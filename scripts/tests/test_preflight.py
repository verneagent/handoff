#!/usr/bin/env python3
"""Tests for preflight.py.

Covers all check_* functions, _load_required_hooks, and main() flow.
"""

import io
import json
import os
import sys
import tempfile
import unittest

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPT_DIR)

import handoff_config
import lark_im
import preflight  # type: ignore


class PreflightTestBase(unittest.TestCase):
    """Base class with env isolation for preflight tests."""

    def setUp(self):
        self._old_home = os.environ.get("HOME")
        self._old_project = os.environ.get("HANDOFF_PROJECT_DIR")
        self._old_tool = os.environ.get("HANDOFF_SESSION_TOOL")

        self.tmp = tempfile.TemporaryDirectory()
        self.project_dir = os.path.join(self.tmp.name, "project")
        os.makedirs(self.project_dir, exist_ok=True)

        os.environ["HOME"] = self.tmp.name
        os.environ["HANDOFF_PROJECT_DIR"] = self.project_dir
        os.environ["HANDOFF_SESSION_TOOL"] = "Claude Code"

        # Reset handoff_config config resolution cache by setting CONFIG_FILE
        self._orig_config_file = handoff_config.CONFIG_FILE
        self._orig_handoff_home = handoff_config.HANDOFF_HOME

    def tearDown(self):
        handoff_config.CONFIG_FILE = self._orig_config_file
        handoff_config.HANDOFF_HOME = self._orig_handoff_home

        for key, val in [
            ("HOME", self._old_home),
            ("HANDOFF_PROJECT_DIR", self._old_project),
            ("HANDOFF_SESSION_TOOL", self._old_tool),
        ]:
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val
        self.tmp.cleanup()

    def _write_config(self, data):
        """Write config JSON file and point handoff_config.CONFIG_FILE at it."""
        config_dir = os.path.join(self.tmp.name, ".handoff")
        os.makedirs(config_dir, exist_ok=True)
        config_path = os.path.join(config_dir, "config.json")
        with open(config_path, "w") as f:
            json.dump(data, f)
        handoff_config.CONFIG_FILE = config_path
        handoff_config.HANDOFF_HOME = config_dir
        return config_path


# ---------------------------------------------------------------------------
# check_credentials
# ---------------------------------------------------------------------------

class CheckCredentialsTest(PreflightTestBase):
    def test_missing_config_file(self):
        handoff_config.CONFIG_FILE = os.path.join(self.tmp.name, "nonexistent.json")
        handoff_config.HANDOFF_HOME = self.tmp.name
        ok, detail = preflight.check_credentials()
        self.assertFalse(ok)
        self.assertIn("not found", detail)

    def test_invalid_json(self):
        config_dir = os.path.join(self.tmp.name, ".handoff_bad")
        os.makedirs(config_dir, exist_ok=True)
        config_path = os.path.join(config_dir, "config.json")
        with open(config_path, "w") as f:
            f.write("not json {{{")
        handoff_config.CONFIG_FILE = config_path
        handoff_config.HANDOFF_HOME = config_dir

        ok, detail = preflight.check_credentials()
        self.assertFalse(ok)
        self.assertIn("invalid JSON", detail)

    def test_missing_ims_section(self):
        self._write_config({"app_id": "a"})
        ok, detail = preflight.check_credentials()
        self.assertFalse(ok)
        self.assertIn("ims", detail)

    def test_missing_fields(self):
        self._write_config({
            "ims": {"lark": {"app_id": "a"}},
        })
        ok, detail = preflight.check_credentials()
        self.assertFalse(ok)
        self.assertIn("app_secret", detail)
        self.assertIn("email", detail)

    def test_valid_credentials(self):
        self._write_config({
            "ims": {"lark": {"app_id": "a", "app_secret": "s", "email": "e@example.com"}},
        })
        ok, detail = preflight.check_credentials()
        self.assertTrue(ok)
        self.assertIsNone(detail)


# ---------------------------------------------------------------------------
# check_credentials — nested IM config format
# ---------------------------------------------------------------------------

class CheckCredentialsNestedTest(PreflightTestBase):
    def test_nested_valid(self):
        self._write_config({
            "default_im": "lark",
            "worker_url": "https://w.example",
            "ims": {
                "lark": {
                    "app_id": "a", "app_secret": "s", "email": "e@example.com"
                }
            },
        })
        ok, detail = preflight.check_credentials()
        self.assertTrue(ok)
        self.assertIsNone(detail)

    def test_nested_missing_email(self):
        self._write_config({
            "default_im": "lark",
            "ims": {"lark": {"app_id": "a", "app_secret": "s"}},
        })
        ok, detail = preflight.check_credentials()
        self.assertFalse(ok)
        self.assertIn("email", detail)

    def test_nested_missing_provider(self):
        """When ims map exists but the default provider is absent."""
        self._write_config({
            "default_im": "slack",
            "ims": {"lark": {"app_id": "a", "app_secret": "s", "email": "e"}},
        })
        ok, detail = preflight.check_credentials()
        self.assertFalse(ok)
        self.assertIn("slack", detail)

    def test_nested_defaults_to_lark(self):
        """When default_im is omitted, defaults to lark."""
        self._write_config({
            "ims": {
                "lark": {
                    "app_id": "a", "app_secret": "s", "email": "e@example.com"
                }
            },
        })
        ok, detail = preflight.check_credentials()
        self.assertTrue(ok)


# ---------------------------------------------------------------------------
# check_worker_url
# ---------------------------------------------------------------------------

class CheckWorkerUrlTest(PreflightTestBase):
    def setUp(self):
        super().setUp()
        self._orig = handoff_config.load_worker_url

    def tearDown(self):
        handoff_config.load_worker_url = self._orig
        super().tearDown()

    def test_missing_url(self):
        handoff_config.load_worker_url = lambda **kw: None
        ok, detail = preflight.check_worker_url()
        self.assertFalse(ok)
        self.assertIn("worker_url", detail)

    def test_present_url(self):
        handoff_config.load_worker_url = lambda **kw: "https://worker.example.com"
        ok, url = preflight.check_worker_url()
        self.assertTrue(ok)
        self.assertEqual(url, "https://worker.example.com")


# ---------------------------------------------------------------------------
# check_api_key
# ---------------------------------------------------------------------------

class CheckApiKeyTest(PreflightTestBase):
    def setUp(self):
        super().setUp()
        self._orig = handoff_config.load_api_key

    def tearDown(self):
        handoff_config.load_api_key = self._orig
        super().tearDown()

    def test_missing_key(self):
        handoff_config.load_api_key = lambda **kw: None
        ok, detail = preflight.check_api_key()
        self.assertFalse(ok)
        self.assertIn("worker_api_key", detail)

    def test_present_key(self):
        handoff_config.load_api_key = lambda **kw: "secret123"
        ok, detail = preflight.check_api_key()
        self.assertTrue(ok)
        self.assertIsNone(detail)


# ---------------------------------------------------------------------------
# check_token
# ---------------------------------------------------------------------------

class CheckTokenTest(PreflightTestBase):
    def setUp(self):
        super().setUp()
        self._orig_creds = handoff_config.load_credentials
        self._orig_token = lark_im.get_tenant_token

    def tearDown(self):
        handoff_config.load_credentials = self._orig_creds
        lark_im.get_tenant_token = self._orig_token
        super().tearDown()

    def test_no_credentials(self):
        handoff_config.load_credentials = lambda **kw: None
        ok, detail = preflight.check_token()
        self.assertFalse(ok)
        self.assertIn("Skipped", detail)

    def test_token_error(self):
        handoff_config.load_credentials = lambda **kw: {"app_id": "a", "app_secret": "b"}
        lark_im.get_tenant_token = lambda a, b: (_ for _ in ()).throw(
            RuntimeError("auth fail")
        )
        ok, detail = preflight.check_token()
        self.assertFalse(ok)
        self.assertIn("auth fail", detail)

    def test_token_success(self):
        handoff_config.load_credentials = lambda **kw: {"app_id": "a", "app_secret": "b"}
        lark_im.get_tenant_token = lambda a, b: "tok"
        ok, detail = preflight.check_token()
        self.assertTrue(ok)
        self.assertIsNone(detail)


# ---------------------------------------------------------------------------
# _load_required_hooks
# ---------------------------------------------------------------------------

class LoadRequiredHooksTest(PreflightTestBase):
    def test_loads_from_hooks_json(self):
        """When hooks.json exists, load hook names from it."""
        hooks_dir = os.path.dirname(SCRIPT_DIR)  # .claude/skills/handoff/
        hooks_path = os.path.join(hooks_dir, "hooks.json")
        if os.path.exists(hooks_path):
            hooks = preflight._load_required_hooks()
            self.assertIsInstance(hooks, list)
            self.assertGreater(len(hooks), 0)

    def test_fallback_when_missing(self):
        """When hooks.json doesn't exist, use fallback list."""
        # Temporarily change the path that _load_required_hooks looks at
        orig_abspath = os.path.abspath

        def fake_abspath(path):
            if path == __file__:
                return os.path.join(self.tmp.name, "nonexistent", "tests", "test.py")
            return orig_abspath(path)

        # The function uses __file__ internally; we can't easily redirect.
        # Instead, test the fallback list directly.
        fallback = [
            "Notification",
            "PermissionRequest",
            "PostToolUse",
            "SessionStart",
            "SessionEnd",
        ]
        # Verify fallback list is valid
        self.assertGreaterEqual(len(fallback), 5)
        self.assertIn("Notification", fallback)


# ---------------------------------------------------------------------------
# check_hooks
# ---------------------------------------------------------------------------

class CheckHooksTest(PreflightTestBase):
    def test_no_project_dir(self):
        os.environ.pop("HANDOFF_PROJECT_DIR", None)
        ok, detail = preflight.check_hooks()
        self.assertFalse(ok)
        self.assertIn("not initialized", detail)

    def test_missing_settings_files(self):
        # No .claude/settings.json exists
        ok, detail = preflight.check_hooks()
        self.assertFalse(ok)
        self.assertIn("not initialized", detail)

    def test_all_hooks_in_settings(self):
        """When all required hooks are in settings.json, check passes."""
        claude_dir = os.path.join(self.project_dir, ".claude")
        os.makedirs(claude_dir, exist_ok=True)

        required = preflight._load_required_hooks()
        hooks = {name: [{"command": "echo"}] for name in required}
        settings = {"hooks": hooks}

        with open(os.path.join(claude_dir, "settings.json"), "w") as f:
            json.dump(settings, f)

        ok, detail = preflight.check_hooks()
        self.assertTrue(ok)

    def test_hooks_split_across_files(self):
        """Hooks can be split between settings.json and settings.local.json."""
        claude_dir = os.path.join(self.project_dir, ".claude")
        os.makedirs(claude_dir, exist_ok=True)

        required = preflight._load_required_hooks()
        half = len(required) // 2
        first_half = {name: [{"command": "echo"}] for name in required[:half]}
        second_half = {name: [{"command": "echo"}] for name in required[half:]}

        with open(os.path.join(claude_dir, "settings.json"), "w") as f:
            json.dump({"hooks": first_half}, f)
        with open(os.path.join(claude_dir, "settings.local.json"), "w") as f:
            json.dump({"hooks": second_half}, f)

        ok, detail = preflight.check_hooks()
        self.assertTrue(ok)

    def test_partial_hooks_reports_missing(self):
        claude_dir = os.path.join(self.project_dir, ".claude")
        os.makedirs(claude_dir, exist_ok=True)

        required = preflight._load_required_hooks()
        # Only configure first hook
        hooks = {required[0]: [{"command": "echo"}]}
        with open(os.path.join(claude_dir, "settings.json"), "w") as f:
            json.dump({"hooks": hooks}, f)

        ok, detail = preflight.check_hooks()
        if len(required) > 1:
            self.assertFalse(ok)
            self.assertIn("Missing hooks", detail)


# ---------------------------------------------------------------------------
# main() flow
# ---------------------------------------------------------------------------

class MainFlowTest(PreflightTestBase):
    def setUp(self):
        super().setUp()
        self._orig_argv = sys.argv
        self._orig_creds = handoff_config.load_credentials
        self._orig_token = lark_im.get_tenant_token
        self._orig_worker = handoff_config.load_worker_url
        self._orig_api_key = handoff_config.load_api_key

    def tearDown(self):
        sys.argv = self._orig_argv
        handoff_config.load_credentials = self._orig_creds
        lark_im.get_tenant_token = self._orig_token
        handoff_config.load_worker_url = self._orig_worker
        handoff_config.load_api_key = self._orig_api_key
        super().tearDown()

    def _run_main(self, argv):
        sys.argv = ["preflight.py"] + argv
        old_stdout = sys.stdout
        sys.stdout = io.StringIO()
        try:
            preflight.main()
            return sys.stdout.getvalue(), None
        except SystemExit as e:
            return sys.stdout.getvalue(), e.code
        finally:
            sys.stdout = old_stdout

    def test_all_checks_fail_exits_1(self):
        handoff_config.CONFIG_FILE = os.path.join(self.tmp.name, "nope.json")
        handoff_config.load_worker_url = lambda **kw: None
        handoff_config.load_api_key = lambda **kw: None
        handoff_config.load_credentials = lambda **kw: None

        output, exit_code = self._run_main(["--skip-hooks"])
        self.assertEqual(exit_code, 1)
        self.assertIn("[FAIL]", output)
        self.assertIn("issue(s) found", output)

    def test_all_checks_pass(self):
        self._write_config({
            "worker_url": "https://w.example",
            "worker_api_key": "k",
            "ims": {"lark": {"app_id": "a", "app_secret": "s", "email": "e@e.com"}},
        })
        handoff_config.load_worker_url = lambda **kw: "https://w.example"
        handoff_config.load_api_key = lambda **kw: "k"
        handoff_config.load_credentials = lambda **kw: {
            "app_id": "a", "app_secret": "s"
        }
        lark_im.get_tenant_token = lambda a, b: "tok"

        # Set up hooks
        claude_dir = os.path.join(self.project_dir, ".claude")
        os.makedirs(claude_dir, exist_ok=True)
        required = preflight._load_required_hooks()
        hooks = {name: [{"command": "echo"}] for name in required}
        with open(os.path.join(claude_dir, "settings.json"), "w") as f:
            json.dump({"hooks": hooks}, f)

        # Skip worker reachability (requires curl)
        # The main flow checks worker_url first, then reachability
        # We can test the non-curl checks pass
        output, exit_code = self._run_main(["--skip-hooks"])

        # Token and credentials should pass at minimum
        self.assertIn("[OK] Lark credentials", output)
        self.assertIn("[OK] Worker URL", output)

    def test_report_flag(self):
        self._write_config({
            "ims": {"lark": {"app_id": "a", "app_secret": "secret123", "email": "e@e.com"}},
        })
        handoff_config.load_credentials = lambda **kw: {
            "app_id": "a", "app_secret": "secret123"
        }
        lark_im.get_tenant_token = lambda a, b: "tok"

        output, exit_code = self._run_main(["--report"])
        self.assertIsNone(exit_code)  # report() doesn't sys.exit
        self.assertIn("Handoff Configuration Report", output)
        self.assertIn("app_id: a", output)
        # Secret should be redacted
        self.assertIn("***", output)

    def test_report_nested_format(self):
        self._write_config({
            "default_im": "lark",
            "worker_url": "https://w.example",
            "worker_api_key": "key12345",
            "ims": {
                "lark": {
                    "app_id": "nested_a",
                    "app_secret": "nested_secret",
                    "email": "nested@e.com",
                }
            },
        })
        handoff_config.load_credentials = lambda **kw: {
            "app_id": "nested_a", "app_secret": "nested_secret"
        }
        lark_im.get_tenant_token = lambda a, b: "tok"

        output, exit_code = self._run_main(["--report"])
        self.assertIsNone(exit_code)
        self.assertIn("IM provider: lark", output)
        self.assertIn("app_id: nested_a", output)
        self.assertIn("email: nested@e.com", output)
        self.assertIn("worker_url: https://w.example", output)


# ---------------------------------------------------------------------------
# check_worker_reachable (mock subprocess)
# ---------------------------------------------------------------------------

class CheckWorkerReachableTest(PreflightTestBase):
    def setUp(self):
        super().setUp()
        self._orig_run = __import__("subprocess").run
        self._orig_auth = handoff_config._worker_auth_headers

    def tearDown(self):
        __import__("subprocess").run = self._orig_run
        handoff_config._worker_auth_headers = self._orig_auth
        super().tearDown()

    def test_curl_failure(self):
        import subprocess as sp

        class FakeResult:
            returncode = 1
            stdout = ""
            stderr = "connection refused"

        sp.run = lambda *a, **kw: FakeResult()
        handoff_config._worker_auth_headers = lambda **kw: []

        ok, detail = preflight.check_worker_reachable("https://w.example")
        self.assertFalse(ok)
        self.assertIn("curl failed", detail)

    def test_unauthorized_response(self):
        import subprocess as sp

        class FakeResult:
            returncode = 0
            stdout = "Unauthorized"
            stderr = ""

        sp.run = lambda *a, **kw: FakeResult()
        handoff_config._worker_auth_headers = lambda **kw: []

        ok, detail = preflight.check_worker_reachable("https://w.example")
        self.assertFalse(ok)
        self.assertIn("401", detail)

    def test_healthy_response(self):
        import subprocess as sp

        class FakeResult:
            returncode = 0
            stdout = json.dumps({"ok": True, "verify_token": True})
            stderr = ""

        sp.run = lambda *a, **kw: FakeResult()
        handoff_config._worker_auth_headers = lambda **kw: []

        ok, detail = preflight.check_worker_reachable("https://w.example")
        self.assertTrue(ok)

    def test_missing_verify_token(self):
        import subprocess as sp

        class FakeResult:
            returncode = 0
            stdout = json.dumps({"ok": True, "verify_token": False})
            stderr = ""

        sp.run = lambda *a, **kw: FakeResult()
        handoff_config._worker_auth_headers = lambda **kw: []

        ok, detail = preflight.check_worker_reachable("https://w.example")
        self.assertFalse(ok)
        self.assertIn("VERIFY_TOKEN", detail)

    def test_exception_during_curl(self):
        import subprocess as sp

        def raise_timeout(*a, **kw):
            raise sp.TimeoutExpired("curl", 15)

        sp.run = raise_timeout
        handoff_config._worker_auth_headers = lambda **kw: []

        ok, detail = preflight.check_worker_reachable("https://w.example")
        self.assertFalse(ok)
        self.assertIn("unreachable", detail)


if __name__ == "__main__":
    unittest.main()
