#!/usr/bin/env python3
"""Tests for on_post_tool_use.py ANSI rendering and formatting.

Covers _strip_ansi, _render_ansi (cursor movement, carriage return,
erase-to-end, bold tracking, OSC stripping, color conversion),
_format_edit, _format_write, _format_bash, _format_failure, and main().
"""

import io
import json
import os
import sys
import unittest

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPT_DIR)

import on_post_tool_use  # type: ignore


# ---------------------------------------------------------------------------
# _strip_ansi
# ---------------------------------------------------------------------------


class StripAnsiTest(unittest.TestCase):
    def test_plain_text_unchanged(self):
        self.assertEqual(on_post_tool_use._strip_ansi("hello"), "hello")

    def test_strips_csi_sequences(self):
        self.assertEqual(on_post_tool_use._strip_ansi("\x1b[31mred\x1b[0m"), "red")

    def test_strips_osc_bel_terminated(self):
        """OSC hyperlinks (\x1b]8;;url\x07text\x1b]8;;\x07) should be stripped."""
        text = "\x1b]8;;https://example.com\x07click here\x1b]8;;\x07"
        result = on_post_tool_use._strip_ansi(text)
        self.assertEqual(result, "click here")

    def test_strips_osc_st_terminated(self):
        """OSC sequences terminated by ST (\x1b\\) should be stripped."""
        text = "\x1b]0;window title\x1b\\"
        result = on_post_tool_use._strip_ansi(text)
        self.assertEqual(result, "")

    def test_strips_charset_selection(self):
        """Character set selection sequences should be stripped."""
        text = "\x1b(Bhello\x1b)0"
        result = on_post_tool_use._strip_ansi(text)
        self.assertEqual(result, "hello")

    def test_cursor_movement(self):
        self.assertEqual(on_post_tool_use._strip_ansi("\x1b[1A\x1b[2Kline"), "line")

    def test_mixed_codes(self):
        text = "\x1b[32m✓\x1b[0m 1 passed \x1b[90m(10.2s)\x1b[0m"
        self.assertEqual(on_post_tool_use._strip_ansi(text), "✓ 1 passed (10.2s)")


# ---------------------------------------------------------------------------
# _render_ansi — color conversion
# ---------------------------------------------------------------------------


class RenderAnsiColorTest(unittest.TestCase):
    def test_plain_text_no_colors(self):
        text, has_colors = on_post_tool_use._render_ansi("hello world")
        self.assertEqual(text, "hello world")
        self.assertFalse(has_colors)

    def test_red_text(self):
        text, has_colors = on_post_tool_use._render_ansi("\x1b[31merror\x1b[0m")
        self.assertIn('<font color="red">', text)
        self.assertIn("error", text)
        self.assertIn("</font>", text)
        self.assertTrue(has_colors)

    def test_green_text(self):
        text, _ = on_post_tool_use._render_ansi("\x1b[32msuccess\x1b[0m")
        self.assertIn('<font color="green">', text)
        self.assertIn("success", text)

    def test_unclosed_color_auto_closes(self):
        """Color without reset should be auto-closed at end of line."""
        text, _ = on_post_tool_use._render_ansi("\x1b[31mred text")
        self.assertIn("</font>", text)
        # Should not have dangling open tag
        self.assertEqual(text.count('<font color="red">'), text.count("</font>"))

    def test_color_switch(self):
        """Switching from one color to another should close the first."""
        text, _ = on_post_tool_use._render_ansi("\x1b[31mred\x1b[32mgreen\x1b[0m")
        self.assertEqual(text.count("</font>"), 2)  # one for switch, one for reset

    def test_reset_39_closes_color_only(self):
        """Code 39 (default fg) should close color but not bold."""
        text, _ = on_post_tool_use._render_ansi(
            "\x1b[1m\x1b[31mbold red\x1b[39m still bold\x1b[0m"
        )
        # After 39: color closed but bold still open until 0
        self.assertIn("still bold", text)

    def test_yellow_maps_to_orange(self):
        text, _ = on_post_tool_use._render_ansi("\x1b[33mwarning\x1b[0m")
        self.assertIn('<font color="orange">', text)

    def test_dim_maps_to_grey(self):
        text, _ = on_post_tool_use._render_ansi("\x1b[2m(10.2s)\x1b[22m")
        self.assertIn('<font color="grey">', text)

    def test_playwright_progress_output(self):
        # Real Playwright output pattern with cursor movement
        output = (
            "\x1b[2m[WebServer]\x1b[22m \x1b[33mThe CJS build\x1b[0m\n"
            "\x1b[1A\x1b[2K\x1b[32m✓\x1b[0m 1 passed (10.2s)"
        )
        text, has_colors = on_post_tool_use._render_ansi(output)
        self.assertIn("1 passed", text)
        self.assertTrue(has_colors)

    def test_multiline_plain_and_color(self):
        output = "plain line\n\x1b[32mgreen line\x1b[0m\nanother plain"
        text, has_colors = on_post_tool_use._render_ansi(output)
        self.assertIn("plain line", text)
        self.assertIn('<font color="green">', text)
        self.assertIn("another plain", text)
        self.assertTrue(has_colors)


# ---------------------------------------------------------------------------
# _render_ansi — bold tracking
# ---------------------------------------------------------------------------


class RenderAnsiBoldTest(unittest.TestCase):
    def test_bold_opens_and_closes(self):
        """Bold (**) should be properly closed at reset."""
        text, _ = on_post_tool_use._render_ansi("\x1b[1mbold text\x1b[0m")
        self.assertIn("**bold text**", text)

    def test_bold_unclosed_auto_closes(self):
        """Bold without reset should auto-close at end of line."""
        text, _ = on_post_tool_use._render_ansi("\x1b[1mbold text")
        count = text.count("**")
        self.assertEqual(count, 2)  # open + close

    def test_bold_reset_22(self):
        """Code 22 (normal intensity) should close bold only."""
        text, _ = on_post_tool_use._render_ansi(
            "\x1b[1m\x1b[31mbold red\x1b[22m just red\x1b[0m"
        )
        # Bold wraps "bold red", color stays open for "just red"
        self.assertIn("**", text)
        self.assertIn("bold red", text)
        self.assertIn("just red", text)
        # Bold closes before "just red" — find positions
        bold_close = text.index("**", text.index("**") + 2)
        self.assertLess(bold_close, text.index("just red"))
        # Color still active around "just red"
        self.assertIn("</font>", text)

    def test_bold_not_doubled(self):
        """Double bold codes should not produce extra **."""
        text, _ = on_post_tool_use._render_ansi("\x1b[1m\x1b[1mbold\x1b[0m")
        # Only 2 ** markers (open + close), not 4
        self.assertEqual(text.count("**"), 2)

    def test_bold_plus_color(self):
        """Combined bold+color: \x1b[1;31m should produce both."""
        text, _ = on_post_tool_use._render_ansi("\x1b[1;31mbold red\x1b[0m")
        self.assertIn("**", text)
        self.assertIn('<font color="red">', text)


# ---------------------------------------------------------------------------
# _render_ansi — carriage return
# ---------------------------------------------------------------------------


class RenderAnsiCarriageReturnTest(unittest.TestCase):
    def test_simple_cr_overwrites(self):
        """\\r overwrites from the beginning of the line."""
        text, _ = on_post_tool_use._render_ansi("old text\rnew text")
        self.assertIn("new text", text)
        self.assertNotIn("old", text)

    def test_partial_cr_overwrite(self):
        """Short \\r text overwrites only the first N chars."""
        text, _ = on_post_tool_use._render_ansi("12345\r67")
        self.assertEqual(text.strip(), "67345")

    def test_progress_bar_cr(self):
        """Multiple \\r segments (progress bar pattern) shows final state."""
        text, _ = on_post_tool_use._render_ansi(
            "Progress: 25%\rProgress: 50%\rProgress: 100%"
        )
        self.assertIn("100%", text)
        self.assertNotIn("25%", text)

    def test_cr_with_ansi(self):
        """\\r combined with ANSI codes should render final state."""
        text, _ = on_post_tool_use._render_ansi(
            "loading...\r\x1b[32mdone!\x1b[0m      "
        )
        stripped = on_post_tool_use._strip_ansi(text)
        self.assertIn("done!", stripped)


# ---------------------------------------------------------------------------
# _render_ansi — cursor movement
# ---------------------------------------------------------------------------


class RenderAnsiCursorTest(unittest.TestCase):
    def test_cursor_up_overwrites_previous_line(self):
        """Cursor-up + erase-line should overwrite previous output."""
        # Line 1: "old", then cursor up + erase + "new"
        text, _ = on_post_tool_use._render_ansi("old\n\x1b[1A\x1b[2Knew")
        lines = [l for l in text.splitlines() if l.strip()]
        self.assertIn("new", lines[0])
        self.assertNotIn("old", text)

    def test_erase_to_end_of_line(self):
        """\\x1b[K truncates content after the sequence."""
        text, _ = on_post_tool_use._render_ansi("hello world\x1b[K gone")
        # Everything after \x1b[K (including " gone") should be removed
        self.assertNotIn("gone", text)
        self.assertIn("hello world", text)

    def test_erase_to_end_0k(self):
        """\\x1b[0K is equivalent to \\x1b[K."""
        text, _ = on_post_tool_use._render_ansi("keep\x1b[0Kremoved")
        self.assertIn("keep", text)
        self.assertNotIn("removed", text)


# ---------------------------------------------------------------------------
# _render_ansi — OSC stripping
# ---------------------------------------------------------------------------


class RenderAnsiOscTest(unittest.TestCase):
    def test_osc_hyperlink_stripped(self):
        """OSC 8 hyperlinks should be stripped, leaving only the visible text."""
        text, _ = on_post_tool_use._render_ansi(
            "\x1b]8;;https://example.com\x07Link\x1b]8;;\x07"
        )
        self.assertIn("Link", text)
        self.assertNotIn("example.com", text)
        self.assertNotIn("\x1b]", text)

    def test_osc_title_stripped(self):
        """OSC 0 (window title) should be stripped."""
        text, _ = on_post_tool_use._render_ansi("\x1b]0;my title\x07actual output")
        self.assertIn("actual output", text)
        self.assertNotIn("my title", text)


# ---------------------------------------------------------------------------
# _format_edit
# ---------------------------------------------------------------------------


class FormatEditTest(unittest.TestCase):
    def test_basic_edit(self):
        title, body = on_post_tool_use._format_edit(
            {"file_path": "/src/app.py", "old_string": "foo", "new_string": "bar"},
            {},
            "/src",
        )
        self.assertIn("app.py", body)
        self.assertIn("foo", body)
        self.assertIn("bar", body)

    def test_no_changes_returns_none(self):
        title, body = on_post_tool_use._format_edit(
            {"file_path": "/f.py", "old_string": "same", "new_string": "same"},
            {},
            "/",
        )
        self.assertIsNone(body)

    def test_syntax_highlighting(self):
        """Python files should get python code blocks."""
        _, body = on_post_tool_use._format_edit(
            {"file_path": "/x.py", "old_string": "a", "new_string": "b"},
            {},
            "/",
        )
        self.assertIn("```python", body)

    def test_multiline_diff(self):
        tool_input = {
            "file_path": "/proj/file.py",
            "old_string": "line1\nline2\nline3",
            "new_string": "line1\nmodified\nline3\nextra",
        }
        title, body = on_post_tool_use._format_edit(tool_input, {}, "/proj")
        self.assertEqual(title, "")
        self.assertIn("**Edit: file.py**", body)
        self.assertIn("line2", body)
        self.assertIn("modified", body)
        self.assertIn("extra", body)

    def test_pure_addition(self):
        tool_input = {
            "file_path": "/proj/new.py",
            "old_string": "a\nb",
            "new_string": "a\nb\nc",
        }
        title, body = on_post_tool_use._format_edit(tool_input, {}, "/proj")
        self.assertIn('<font color="green">Added:</font>', body)
        # No removed section for pure addition
        self.assertNotIn('<font color="red">Removed:</font>', body)

    def test_pure_deletion(self):
        tool_input = {
            "file_path": "/proj/old.py",
            "old_string": "a\nb\nc",
            "new_string": "a\nc",
        }
        title, body = on_post_tool_use._format_edit(tool_input, {}, "/proj")
        self.assertIn('<font color="red">Removed:</font>', body)
        # No added section for pure deletion
        self.assertNotIn('<font color="green">Added:</font>', body)

    def test_empty_strings_returns_none(self):
        tool_input = {
            "file_path": "/proj/empty.py",
            "old_string": "",
            "new_string": "",
        }
        title, body = on_post_tool_use._format_edit(tool_input, {}, "/proj")
        self.assertIsNone(title)
        self.assertIsNone(body)

    def test_unknown_ext_uses_plain_code_block(self):
        tool_input = {
            "file_path": "/proj/file.xyz",
            "old_string": "old",
            "new_string": "new",
        }
        title, body = on_post_tool_use._format_edit(tool_input, {}, "/proj")
        # Unknown extension: plain code block (no language)
        self.assertIn("```\n", body)
        self.assertNotIn("```python", body)


# ---------------------------------------------------------------------------
# _format_write
# ---------------------------------------------------------------------------


class FormatWriteTest(unittest.TestCase):
    def test_basic_write(self):
        _, body = on_post_tool_use._format_write(
            {"file_path": "/src/new.py", "content": "line1\nline2\n"},
            {},
            "/src",
        )
        self.assertIn("new.py", body)
        self.assertIn("2 lines", body)

    def test_single_line_no_newline(self):
        _, body = on_post_tool_use._format_write(
            {"file_path": "/proj/one.txt", "content": "hello"},
            {},
            "/proj",
        )
        self.assertIn("1 lines", body)

    def test_empty_content(self):
        _, body = on_post_tool_use._format_write(
            {"file_path": "/proj/empty.txt", "content": ""},
            {},
            "/proj",
        )
        self.assertIn("0 lines", body)


# ---------------------------------------------------------------------------
# _format_bash
# ---------------------------------------------------------------------------


class FormatBashTest(unittest.TestCase):
    def test_skip_infrastructure_commands(self):
        for script in on_post_tool_use.SKIP_COMMANDS:
            title, body = on_post_tool_use._format_bash(
                {"command": f"python3 /path/{script} arg"},
                {"stdout": "output", "stderr": "", "exitCode": 0},
                "/",
            )
            self.assertIsNone(body, f"Should skip {script}")

    def test_no_output_no_error_skipped(self):
        title, body = on_post_tool_use._format_bash(
            {"command": "mkdir -p /tmp/foo"},
            {"stdout": "", "stderr": "", "exitCode": 0},
            "/",
        )
        self.assertIsNone(body)

    def test_exit_code_shown(self):
        _, body = on_post_tool_use._format_bash(
            {"command": "false"},
            {"stdout": "", "stderr": "error", "exitCode": 1},
            "/",
        )
        self.assertIn("Exit code: 1", body)

    def test_description_used_as_label(self):
        _, body = on_post_tool_use._format_bash(
            {"command": "git log --oneline -5", "description": "Show recent commits"},
            {"stdout": "abc123 first\ndef456 second", "stderr": "", "exitCode": 0},
            "/",
        )
        self.assertIn("Show recent commits", body)

    def test_ansi_in_output_produces_font_tags(self):
        _, body = on_post_tool_use._format_bash(
            {"command": "ls --color"},
            {"stdout": "\x1b[32mfile.txt\x1b[0m", "stderr": "", "exitCode": 0},
            "/",
        )
        self.assertIn('<font color="green">', body)
        self.assertNotIn("```", body)  # colored output uses plain text, not code block

    def test_diff_output_formatted(self):
        diff = (
            "diff --git a/file.py b/file.py\n"
            "index abc..def 100644\n"
            "--- a/file.py\n"
            "+++ b/file.py\n"
            "@@ -1,3 +1,3 @@\n"
            "-old line\n"
            "+new line\n"
        )
        _, body = on_post_tool_use._format_bash(
            {"command": "git diff"},
            {"stdout": diff, "stderr": "", "exitCode": 0},
            "/",
        )
        self.assertIn("file.py", body)
        self.assertIn("old line", body)
        self.assertIn("new line", body)

    def test_long_label_truncated(self):
        cmd = "x" * 200
        _, body = on_post_tool_use._format_bash(
            {"command": cmd},
            {"stdout": "output", "stderr": "", "exitCode": 0},
            "/",
        )
        self.assertIn("...", body.split("\n")[0])

    def test_uses_command_when_no_description(self):
        _, body = on_post_tool_use._format_bash(
            {"command": "ls -la", "description": ""},
            {"stdout": "file1\nfile2", "stderr": "", "exitCode": 0},
            "/proj",
        )
        self.assertIn("**$ ls -la**", body)

    def test_no_output_with_error_shows_exit_code(self):
        title, body = on_post_tool_use._format_bash(
            {"command": "false", "description": "Fail"},
            {"stdout": "", "stderr": "", "exitCode": 1},
            "/proj",
        )
        self.assertEqual(title, "")
        self.assertIn("Exit code: 1", body)
        self.assertNotIn("```", body)  # No code block when no output

    def test_combined_stdout_stderr(self):
        _, body = on_post_tool_use._format_bash(
            {"command": "cmd", "description": "Test"},
            {"stdout": "out", "stderr": "warn", "exitCode": 0},
            "/proj",
        )
        self.assertIn("out", body)
        self.assertIn("warn", body)

    def test_bold_in_output_properly_closed(self):
        """Bold ANSI codes should produce matched ** pairs."""
        _, body = on_post_tool_use._format_bash(
            {"command": "test"},
            {"stdout": "\x1b[1mBOLD\x1b[0m normal", "stderr": "", "exitCode": 0},
            "/",
        )
        # 4 total: 2 for label (**$ test**) + 2 for ANSI bold (**BOLD**)
        self.assertEqual(body.count("**"), 4)
        self.assertIn("**BOLD**", body)

    def test_osc_hyperlink_in_output_stripped(self):
        """OSC hyperlinks in command output should be stripped."""
        _, body = on_post_tool_use._format_bash(
            {"command": "ls"},
            {
                "stdout": "\x1b]8;;file:///foo\x07foo.txt\x1b]8;;\x07",
                "stderr": "",
                "exitCode": 0,
            },
            "/",
        )
        self.assertIn("foo.txt", body)
        self.assertNotIn("\x1b]", body)
        self.assertNotIn("file:///", body)

    def test_progress_bar_cr_shows_final(self):
        """Progress bar with \\r should show only final state."""
        _, body = on_post_tool_use._format_bash(
            {"command": "build"},
            {
                "stdout": "Building 1/3\rBuilding 2/3\rBuilding 3/3",
                "stderr": "",
                "exitCode": 0,
            },
            "/",
        )
        self.assertIn("3/3", body)
        self.assertNotIn("1/3", body)


# ---------------------------------------------------------------------------
# _format_diff_output
# ---------------------------------------------------------------------------


class FormatDiffOutputTest(unittest.TestCase):
    def test_multi_file_diff(self):
        diff = (
            "diff --git a/a.py b/a.py\n"
            "--- a/a.py\n"
            "+++ b/a.py\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
            "diff --git a/b.js b/b.js\n"
            "--- a/b.js\n"
            "+++ b/b.js\n"
            "@@ -1 +1 @@\n"
            "-foo\n"
            "+bar\n"
        )
        result = on_post_tool_use._format_diff_output(diff)
        self.assertIn("a.py", result)
        self.assertIn("b.js", result)
        self.assertIn("old", result)
        self.assertIn("bar", result)

    def test_no_diff_returns_none(self):
        self.assertIsNone(on_post_tool_use._format_diff_output("not a diff"))

    def test_single_file_diff(self):
        diff = (
            "diff --git a/src/main.py b/src/main.py\n"
            "index abc..def 100644\n"
            "--- a/src/main.py\n"
            "+++ b/src/main.py\n"
            "@@ -1,3 +1,3 @@\n"
            " context\n"
            "-old line\n"
            "+new line\n"
            " more context\n"
        )
        result = on_post_tool_use._format_diff_output(diff)
        self.assertIsNotNone(result)
        self.assertIn("**src/main.py**", result)
        self.assertIn('<font color="red">Removed:</font>', result)
        self.assertIn('<font color="green">Added:</font>', result)
        self.assertIn("old line", result)
        self.assertIn("new line", result)
        self.assertIn("```python", result)

    def test_pure_addition_diff(self):
        diff = (
            "diff --git a/new.py b/new.py\n"
            "--- /dev/null\n"
            "+++ b/new.py\n"
            "@@ -0,0 +1,2 @@\n"
            "+line1\n"
            "+line2\n"
        )
        result = on_post_tool_use._format_diff_output(diff)
        self.assertIn('<font color="green">Added:</font>', result)
        self.assertNotIn('<font color="red">Removed:</font>', result)

    def test_empty_diff_returns_none(self):
        result = on_post_tool_use._format_diff_output("")
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# _format_failure
# ---------------------------------------------------------------------------


class FormatFailureTest(unittest.TestCase):
    def test_bash_failure(self):
        title, body, color = on_post_tool_use._format_failure(
            "Bash",
            {"command": "make build", "description": "Build project"},
            "compilation failed",
            "/",
        )
        self.assertIn("Build project", title)
        self.assertEqual(color, "red")
        self.assertIn("compilation failed", body)

    def test_edit_failure(self):
        title, body, color = on_post_tool_use._format_failure(
            "Edit", {"file_path": "/src/main.py"}, "string not found", "/src"
        )
        self.assertIn("main.py", title)
        self.assertEqual(color, "red")

    def test_skip_infrastructure(self):
        result = on_post_tool_use._format_failure(
            "Bash", {"command": "python3 send_to_group.py hi"}, "error", "/"
        )
        self.assertEqual(result, (None, None, None))

    def test_ansi_stripped_from_error(self):
        _, body, _ = on_post_tool_use._format_failure(
            "Bash", {"command": "test"}, "\x1b[31mred error\x1b[0m", "/"
        )
        self.assertNotIn("\x1b[", body)
        self.assertIn("red error", body)

    def test_bash_failure_uses_command_when_no_description(self):
        title, body, color = on_post_tool_use._format_failure(
            "Bash", {"command": "npm build", "description": ""}, "fail", "/proj"
        )
        self.assertEqual(title, "$ npm build")

    def test_write_failure(self):
        title, body, color = on_post_tool_use._format_failure(
            "Write", {"file_path": "/proj/out.txt", "content": "data"}, "Permission denied", "/proj"
        )
        self.assertEqual(title, "Write: out.txt")
        self.assertIn("Permission denied", body)

    def test_unknown_tool_failure(self):
        title, body, color = on_post_tool_use._format_failure(
            "Glob", {}, "error", "/proj"
        )
        self.assertEqual(title, "Glob failed")
        self.assertEqual(color, "red")

    def test_long_bash_title_truncated(self):
        long_desc = "x" * 100
        title, body, color = on_post_tool_use._format_failure(
            "Bash", {"command": "cmd", "description": long_desc}, "err", "/proj"
        )
        self.assertLessEqual(len(title), 80)
        self.assertTrue(title.endswith("..."))


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------


class MainTest(unittest.TestCase):
    def setUp(self):
        self._orig_get_session = on_post_tool_use.handoff_db.get_session
        self._orig_load_creds = on_post_tool_use.handoff_config.load_credentials
        self._orig_get_token = on_post_tool_use.lark_im.get_tenant_token
        self._orig_send_msg = on_post_tool_use.lark_im.send_message
        self._orig_build_card = on_post_tool_use.lark_im.build_markdown_card
        self._orig_record = on_post_tool_use.handoff_db.record_sent_message

    def tearDown(self):
        on_post_tool_use.handoff_db.get_session = self._orig_get_session
        on_post_tool_use.handoff_config.load_credentials = self._orig_load_creds
        on_post_tool_use.lark_im.get_tenant_token = self._orig_get_token
        on_post_tool_use.lark_im.send_message = self._orig_send_msg
        on_post_tool_use.lark_im.build_markdown_card = self._orig_build_card
        on_post_tool_use.handoff_db.record_sent_message = self._orig_record

    def _run_main(self, hook_input):
        old_stdin, old_stdout, old_stderr = sys.stdin, sys.stdout, sys.stderr
        sys.stdin = io.StringIO(json.dumps(hook_input))
        sys.stdout = io.StringIO()
        sys.stderr = io.StringIO()
        try:
            on_post_tool_use.main()
            return sys.stdout.getvalue(), sys.stderr.getvalue()
        finally:
            sys.stdin = old_stdin
            sys.stdout = old_stdout
            sys.stderr = old_stderr

    def test_no_session_returns_early(self):
        sent = []
        on_post_tool_use.handoff_db.get_session = lambda sid: None
        on_post_tool_use.lark_im.send_message = lambda *a, **kw: sent.append(1)

        self._run_main(
            {
                "session_id": "s1",
                "tool_name": "Bash",
                "tool_input": {"command": "ls"},
                "tool_response": {"stdout": "output", "stderr": "", "exitCode": 0},
            }
        )
        self.assertEqual(len(sent), 0)

    def test_empty_session_id_returns_early(self):
        sent = []
        on_post_tool_use.lark_im.send_message = lambda *a, **kw: sent.append(1)

        self._run_main(
            {
                "session_id": "",
                "tool_name": "Bash",
                "tool_input": {"command": "ls"},
                "tool_response": {"stdout": "output", "stderr": "", "exitCode": 0},
            }
        )
        self.assertEqual(len(sent), 0)

    def test_bash_output_sent(self):
        sent = []
        on_post_tool_use.handoff_db.get_session = lambda sid: {
            "chat_id": "c1",
            "message_filter": "verbose",
        }
        on_post_tool_use.handoff_config.load_credentials = lambda **kw: {
            "app_id": "a",
            "app_secret": "b",
        }
        on_post_tool_use.lark_im.get_tenant_token = lambda a, b: "tok"
        on_post_tool_use.lark_im.build_markdown_card = lambda *a, **kw: {}
        on_post_tool_use.lark_im.send_message = lambda *a, **kw: (
            sent.append(1) or "m1"
        )
        on_post_tool_use.handoff_db.record_sent_message = lambda *a, **kw: None

        self._run_main(
            {
                "session_id": "s1",
                "tool_name": "Bash",
                "tool_input": {"command": "echo hello"},
                "tool_response": {"stdout": "hello", "stderr": "", "exitCode": 0},
            }
        )
        self.assertEqual(len(sent), 1)

    def test_failure_event_sends_red_card(self):
        cards = []
        on_post_tool_use.handoff_db.get_session = lambda sid: {
            "chat_id": "c1",
            "message_filter": "verbose",
        }
        on_post_tool_use.handoff_config.load_credentials = lambda **kw: {
            "app_id": "a",
            "app_secret": "b",
        }
        on_post_tool_use.lark_im.get_tenant_token = lambda a, b: "tok"
        on_post_tool_use.lark_im.build_markdown_card = (
            lambda body, title=None, color=None: cards.append(color) or {}
        )
        on_post_tool_use.lark_im.send_message = lambda *a, **kw: "m1"
        on_post_tool_use.handoff_db.record_sent_message = lambda *a, **kw: None

        self._run_main(
            {
                "session_id": "s1",
                "hook_event_name": "PostToolUseFailure",
                "tool_name": "Bash",
                "tool_input": {"command": "make"},
                "error": "build failed",
            }
        )
        self.assertEqual(cards[0], "red")

    def test_interrupt_skipped(self):
        sent = []
        on_post_tool_use.handoff_db.get_session = lambda sid: {"chat_id": "c1"}
        on_post_tool_use.handoff_config.load_credentials = lambda **kw: None
        on_post_tool_use.lark_im.send_message = lambda *a, **kw: sent.append(1)

        self._run_main(
            {
                "session_id": "s1",
                "hook_event_name": "PostToolUseFailure",
                "tool_name": "Bash",
                "tool_input": {"command": "long-task"},
                "error": "interrupted",
                "is_interrupt": True,
            }
        )
        self.assertEqual(len(sent), 0)

    def test_invalid_json_handled(self):
        on_post_tool_use.handoff_db.get_session = lambda sid: None
        old_stdin, old_stdout, old_stderr = sys.stdin, sys.stdout, sys.stderr
        sys.stdin = io.StringIO("not json")
        sys.stdout = io.StringIO()
        sys.stderr = io.StringIO()
        try:
            on_post_tool_use.main()
            stderr = sys.stderr.getvalue()
        finally:
            sys.stdin = old_stdin
            sys.stdout = old_stdout
            sys.stderr = old_stderr
        self.assertIn("invalid", stderr)


# ---------------------------------------------------------------------------
# _truncate
# ---------------------------------------------------------------------------


class TruncateTest(unittest.TestCase):
    def test_short_text_unchanged(self):
        self.assertEqual(on_post_tool_use._truncate("hi", 100), "hi")

    def test_long_text_truncated(self):
        result = on_post_tool_use._truncate("x" * 200, 50)
        self.assertEqual(len(result.split("\n")[0]), 50)
        self.assertIn("truncated", result)


# ---------------------------------------------------------------------------
# _relative_path
# ---------------------------------------------------------------------------


class RelativePathTest(unittest.TestCase):
    def test_relative(self):
        self.assertEqual(
            on_post_tool_use._relative_path("/home/user/proj/src/main.py", "/home/user/proj"),
            "src/main.py",
        )

    def test_same_dir(self):
        self.assertEqual(
            on_post_tool_use._relative_path("/proj/file.py", "/proj"),
            "file.py",
        )


# ---------------------------------------------------------------------------
# _lang_for_file
# ---------------------------------------------------------------------------


class LangForFileTest(unittest.TestCase):
    def test_python(self):
        self.assertEqual(on_post_tool_use._lang_for_file("/proj/main.py"), "python")

    def test_typescript(self):
        self.assertEqual(on_post_tool_use._lang_for_file("/proj/app.ts"), "typescript")

    def test_tsx(self):
        self.assertEqual(on_post_tool_use._lang_for_file("/proj/Component.tsx"), "tsx")

    def test_json(self):
        self.assertEqual(on_post_tool_use._lang_for_file("/proj/config.json"), "json")

    def test_unknown_extension(self):
        self.assertEqual(on_post_tool_use._lang_for_file("/proj/data.xyz"), "")

    def test_no_extension(self):
        self.assertEqual(on_post_tool_use._lang_for_file("/proj/Makefile"), "")


# ---------------------------------------------------------------------------
# FORMATTERS registry
# ---------------------------------------------------------------------------


class FormattersMapTest(unittest.TestCase):
    def test_edit_registered(self):
        self.assertIn("Edit", on_post_tool_use.FORMATTERS)

    def test_write_registered(self):
        self.assertIn("Write", on_post_tool_use.FORMATTERS)

    def test_bash_registered(self):
        self.assertIn("Bash", on_post_tool_use.FORMATTERS)

    def test_unknown_tool_not_registered(self):
        self.assertNotIn("Read", on_post_tool_use.FORMATTERS)
        self.assertNotIn("Glob", on_post_tool_use.FORMATTERS)


# ---------------------------------------------------------------------------
# Bash diff detection
# ---------------------------------------------------------------------------


class BashDiffDetectionTest(unittest.TestCase):
    """Tests that _format_bash detects and formats git diff output."""

    def test_git_diff_formatted(self):
        diff = (
            "diff --git a/file.py b/file.py\n"
            "--- a/file.py\n"
            "+++ b/file.py\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
        )
        tool_input = {"command": "git diff", "description": "Show changes"}
        tool_response = {"stdout": diff, "stderr": "", "exitCode": 0}
        title, body = on_post_tool_use._format_bash(tool_input, tool_response, "/proj")
        self.assertIn("**file.py**", body)
        self.assertIn('<font color="red">Removed:</font>', body)

    def test_non_diff_output_uses_code_block(self):
        tool_input = {"command": "ls", "description": "List files"}
        tool_response = {"stdout": "file1\nfile2", "stderr": "", "exitCode": 0}
        title, body = on_post_tool_use._format_bash(tool_input, tool_response, "/proj")
        self.assertIn("```\n", body)

    def test_ansi_colored_output_uses_font_tags(self):
        tool_input = {"command": "npx playwright test", "description": "Run tests"}
        tool_response = {
            "stdout": "\x1b[32m✓\x1b[0m 1 passed \x1b[90m(5s)\x1b[0m",
            "stderr": "",
            "exitCode": 0,
        }
        title, body = on_post_tool_use._format_bash(tool_input, tool_response, "/proj")
        self.assertIn('<font color="green">', body)
        self.assertNotIn("```", body)  # Colors → no code block


# ---------------------------------------------------------------------------
# Working card skip patterns
# ---------------------------------------------------------------------------


class SkipPatternsTest(unittest.TestCase):
    """Test that infrastructure commands are properly skipped."""

    def _should_skip(self, command, description=""):
        """Check if a Bash command would be skipped by the main handler."""
        combined = f"{command} {description}"
        for skip in on_post_tool_use.SKIP_COMMANDS:
            if skip in combined:
                return True
        # Check polling description patterns
        desc_lower = description.lower()
        if desc_lower and ("check" in desc_lower or "poll" in desc_lower) and (
            "reply" in desc_lower or "latest" in desc_lower
            or "output" in desc_lower or "status" in desc_lower
            or "log" in desc_lower or "agent" in desc_lower
        ):
            return True
        # Check background task output path
        if "/tasks/" in command and ".output" in command:
            return True
        return False

    def test_skip_wait_for_reply(self):
        self.assertTrue(self._should_skip("python3 scripts/wait_for_reply.py"))

    def test_skip_send_and_wait(self):
        self.assertTrue(self._should_skip("python3 scripts/send_and_wait.py 'hello'"))

    def test_skip_handoff_ops(self):
        self.assertTrue(self._should_skip("python3 scripts/handoff_ops.py download-image"))

    def test_skip_check_for_reply_description(self):
        self.assertTrue(self._should_skip("tail -3 /tmp/output", "Check for reply"))

    def test_skip_check_reply_after_5_min(self):
        self.assertTrue(self._should_skip("tail -3 /tmp/output", "Check reply after 5 min"))

    def test_skip_check_latest_line(self):
        self.assertTrue(self._should_skip("tail -3 /tmp/output", "Check latest line"))

    def test_skip_poll_for_reply(self):
        self.assertTrue(self._should_skip("tail -3 /tmp/output", "Poll for reply"))

    def test_skip_check_agent_status(self):
        self.assertTrue(self._should_skip("tail -3 /tmp/output", "Check agent status"))

    def test_skip_background_task_output(self):
        self.assertTrue(self._should_skip(
            "sleep 600 && cat /private/tmp/claude-501/tasks/abc123.output"
        ))

    def test_skip_skills_handoff_path(self):
        self.assertTrue(self._should_skip("python3 .claude/skills/handoff/scripts/foo.py"))

    def test_no_skip_normal_command(self):
        self.assertFalse(self._should_skip("git status", "Show working tree status"))

    def test_no_skip_normal_bash(self):
        self.assertFalse(self._should_skip("ls -la", "List files"))


if __name__ == "__main__":
    unittest.main()
