#!/usr/bin/env python3

import os
import sys
import unittest

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPT_DIR)

import permission_core  # type: ignore


class ClassifyDecisionTest(unittest.TestCase):
    """Exhaustive coverage for classify_decision and normalize_decision_text."""

    # -- allow texts --
    def test_allow_y(self):
        self.assertEqual(permission_core.classify_decision("y"), "allow")

    def test_allow_yes(self):
        self.assertEqual(permission_core.classify_decision("yes"), "allow")

    def test_allow_approve(self):
        self.assertEqual(permission_core.classify_decision("approve"), "allow")

    def test_allow_ok(self):
        self.assertEqual(permission_core.classify_decision("ok"), "allow")

    def test_allow_1(self):
        self.assertEqual(permission_core.classify_decision("1"), "allow")

    # -- always texts --
    def test_always_always(self):
        self.assertEqual(permission_core.classify_decision("always"), "always")

    def test_always_yes_always(self):
        self.assertEqual(permission_core.classify_decision("yes always"), "always")

    def test_always_always_allow(self):
        self.assertEqual(permission_core.classify_decision("always allow"), "always")

    # -- deny texts --
    def test_deny_n(self):
        self.assertEqual(permission_core.classify_decision("n"), "deny")

    def test_deny_no(self):
        self.assertEqual(permission_core.classify_decision("no"), "deny")

    def test_deny_deny(self):
        self.assertEqual(permission_core.classify_decision("deny"), "deny")

    def test_deny_reject(self):
        self.assertEqual(permission_core.classify_decision("reject"), "deny")

    def test_deny_0(self):
        self.assertEqual(permission_core.classify_decision("0"), "deny")

    # -- case insensitivity --
    def test_case_upper_yes(self):
        self.assertEqual(permission_core.classify_decision("YES"), "allow")

    def test_case_mixed_approve(self):
        self.assertEqual(permission_core.classify_decision("Approve"), "allow")

    def test_case_upper_always(self):
        self.assertEqual(permission_core.classify_decision("ALWAYS"), "always")

    def test_case_upper_deny(self):
        self.assertEqual(permission_core.classify_decision("DENY"), "deny")

    def test_case_mixed_always_allow(self):
        self.assertEqual(permission_core.classify_decision("Always Allow"), "always")

    # -- whitespace handling --
    def test_whitespace_leading_trailing(self):
        self.assertEqual(permission_core.classify_decision("  yes  "), "allow")

    def test_whitespace_tabs(self):
        self.assertEqual(permission_core.classify_decision("\tno\t"), "deny")

    # -- unrecognized inputs --
    def test_unrecognized_maybe(self):
        self.assertIsNone(permission_core.classify_decision("maybe"))

    def test_unrecognized_empty(self):
        self.assertIsNone(permission_core.classify_decision(""))

    def test_unrecognized_none(self):
        self.assertIsNone(permission_core.classify_decision(None))

    def test_unrecognized_number(self):
        self.assertIsNone(permission_core.classify_decision("2"))

    def test_unrecognized_partial(self):
        self.assertIsNone(permission_core.classify_decision("ye"))

    def test_unrecognized_extra_words(self):
        self.assertIsNone(permission_core.classify_decision("yes please"))

    # -- normalize_decision_text --
    def test_normalize_none(self):
        self.assertEqual(permission_core.normalize_decision_text(None), "")

    def test_normalize_strips_and_lowers(self):
        self.assertEqual(permission_core.normalize_decision_text("  YES  "), "yes")

    def test_normalize_non_string(self):
        self.assertEqual(permission_core.normalize_decision_text(42), "42")

    # -- permission_buttons structure --
    def test_permission_buttons_count(self):
        buttons = permission_core.permission_buttons()
        self.assertEqual(len(buttons), 3)

    def test_permission_buttons_labels(self):
        buttons = permission_core.permission_buttons()
        labels = [b[0] for b in buttons]
        self.assertEqual(labels, ["Approve", "Approve All", "Deny"])

    def test_permission_buttons_values(self):
        buttons = permission_core.permission_buttons()
        values = [b[1] for b in buttons]
        self.assertEqual(values, ["y", "always", "n"])

    # -- build_permission_body --
    def test_build_permission_body(self):
        body = permission_core.build_permission_body("Bash", "run ls")
        self.assertIn("**Tool:** `Bash`", body)
        self.assertIn("run ls", body)

class PollLoopTest(unittest.TestCase):
    """Tests for run_permission_poll_loop edge cases."""

    def test_poll_loop_allow(self):
        polls = [
            {"replies": [], "takeover": False, "error": None},
            {
                "replies": [{"text": "yes", "create_time": "1001", "message_id": "m1"}],
                "takeover": False,
                "error": None,
            },
        ]
        idx = {"i": 0}
        recorded = []
        acked = []
        checked = []

        def poll_fn(chat_id, since):
            i = idx["i"]
            idx["i"] = min(i + 1, len(polls) - 1)
            return polls[i]

        decision, ts = permission_core.run_permission_poll_loop(
            poll_fn=poll_fn,
            ack_fn=lambda chat_id, before: acked.append((chat_id, before)),
            record_received_fn=lambda **kwargs: recorded.append(kwargs),
            set_last_checked_fn=lambda sid, t: checked.append((sid, t)),
            on_deny_fn=lambda: self.fail("deny handler should not run"),
            chat_id="chat-1",
            session_id="s1",
            since="0",
            timeout_seconds=5,
            log_fn=None,
        )

        self.assertEqual(decision, "allow")
        self.assertEqual(ts, "1001")
        self.assertEqual(len(recorded), 1)
        self.assertEqual(acked, [("chat-1", "1001")])
        self.assertEqual(checked, [("s1", "1001")])

    def test_poll_loop_deny_with_callback(self):
        called = {"deny": 0}

        def poll_fn(chat_id, since):
            return {
                "replies": [{"text": "no", "create_time": "2002", "message_id": "m2"}],
                "takeover": False,
                "error": None,
            }

        decision, ts = permission_core.run_permission_poll_loop(
            poll_fn=poll_fn,
            ack_fn=lambda chat_id, before: None,
            record_received_fn=lambda **kwargs: None,
            set_last_checked_fn=lambda sid, t: None,
            on_deny_fn=lambda: called.__setitem__("deny", called["deny"] + 1),
            chat_id="chat-2",
            session_id="s2",
            since="0",
            timeout_seconds=5,
            log_fn=None,
        )

        self.assertEqual(decision, "deny")
        self.assertEqual(ts, "2002")
        self.assertEqual(called["deny"], 1)

    def test_poll_loop_non_decision_advances_cursor(self):
        seen_since = []
        polls = [
            {
                "replies": [
                    {"text": "hello", "create_time": "3003", "message_id": "m3"}
                ],
                "takeover": False,
                "error": None,
            },
            {
                "replies": [
                    {"text": "always", "create_time": "3004", "message_id": "m4"}
                ],
                "takeover": False,
                "error": None,
            },
        ]
        idx = {"i": 0}

        def poll_fn(chat_id, since):
            seen_since.append(since)
            i = idx["i"]
            idx["i"] = min(i + 1, len(polls) - 1)
            return polls[i]

        decision, ts = permission_core.run_permission_poll_loop(
            poll_fn=poll_fn,
            ack_fn=lambda chat_id, before: None,
            record_received_fn=lambda **kwargs: None,
            set_last_checked_fn=lambda sid, t: None,
            on_deny_fn=lambda: None,
            chat_id="chat-3",
            session_id="s3",
            since="0",
            timeout_seconds=5,
            log_fn=None,
        )

        self.assertEqual(decision, "always")
        self.assertEqual(ts, "3004")
        self.assertEqual(seen_since[0], "0")
        self.assertEqual(seen_since[1], "3003")

    def test_poll_loop_takeover_returns_deny(self):
        def poll_fn(chat_id, since):
            return {"replies": [], "takeover": True, "error": None}

        decision, ts = permission_core.run_permission_poll_loop(
            poll_fn=poll_fn,
            ack_fn=lambda chat_id, before: None,
            record_received_fn=lambda **kwargs: None,
            set_last_checked_fn=lambda sid, t: None,
            on_deny_fn=lambda: None,
            chat_id="chat-tk",
            session_id="s-tk",
            since="0",
            timeout_seconds=5,
            log_fn=None,
        )
        self.assertEqual(decision, "deny")

    def test_poll_loop_callback_exception_does_not_crash(self):
        """record_received, set_last_checked, ack exceptions are swallowed."""

        def poll_fn(chat_id, since):
            return {
                "replies": [{"text": "yes", "create_time": "5005", "message_id": "m5"}],
                "takeover": False,
                "error": None,
            }

        decision, ts = permission_core.run_permission_poll_loop(
            poll_fn=poll_fn,
            ack_fn=lambda chat_id, before: (_ for _ in ()).throw(RuntimeError("ack")),
            record_received_fn=lambda **kwargs: (_ for _ in ()).throw(
                RuntimeError("record")
            ),
            set_last_checked_fn=lambda sid, t: (_ for _ in ()).throw(
                RuntimeError("checked")
            ),
            on_deny_fn=lambda: None,
            chat_id="chat-ex",
            session_id="s-ex",
            since="0",
            timeout_seconds=5,
            log_fn=lambda msg: None,
        )
        self.assertEqual(decision, "allow")
        self.assertEqual(ts, "5005")

    def test_poll_loop_timeout_returns_deny(self):
        """When deadline passes without a decision, returns deny."""

        def poll_fn(chat_id, since):
            return {"replies": [], "takeover": False, "error": None}

        decision, ts = permission_core.run_permission_poll_loop(
            poll_fn=poll_fn,
            ack_fn=lambda chat_id, before: None,
            record_received_fn=lambda **kwargs: None,
            set_last_checked_fn=lambda sid, t: None,
            on_deny_fn=lambda: None,
            chat_id="chat-to",
            session_id="s-to",
            since="0",
            timeout_seconds=0.01,
            log_fn=None,
        )
        self.assertEqual(decision, "deny")

    def test_poll_loop_approver_ids_accepts_coowner(self):
        """Coowner in approver_ids can approve permission requests."""
        def poll_fn(chat_id, since):
            return {
                "replies": [{"text": "yes", "create_time": "7007",
                             "message_id": "m7", "sender_id": "co1"}],
                "takeover": False,
                "error": None,
            }

        decision, ts = permission_core.run_permission_poll_loop(
            poll_fn=poll_fn,
            ack_fn=lambda chat_id, before: None,
            record_received_fn=lambda **kwargs: None,
            set_last_checked_fn=lambda sid, t: None,
            on_deny_fn=lambda: None,
            chat_id="chat-co",
            session_id="s-co",
            since="0",
            timeout_seconds=5,
            log_fn=None,
            approver_ids={"op1", "co1"},
        )
        self.assertEqual(decision, "allow")

    def test_poll_loop_approver_ids_rejects_guest(self):
        """Guest NOT in approver_ids cannot approve permission requests."""
        call_count = {"n": 0}

        def poll_fn(chat_id, since):
            call_count["n"] += 1
            if call_count["n"] <= 1:
                # Guest tries to approve
                return {
                    "replies": [{"text": "yes", "create_time": "8008",
                                 "message_id": "m8", "sender_id": "guest1"}],
                    "takeover": False,
                    "error": None,
                }
            # Then operator approves
            return {
                "replies": [{"text": "yes", "create_time": "8009",
                             "message_id": "m9", "sender_id": "op1"}],
                "takeover": False,
                "error": None,
            }

        decision, ts = permission_core.run_permission_poll_loop(
            poll_fn=poll_fn,
            ack_fn=lambda chat_id, before: None,
            record_received_fn=lambda **kwargs: None,
            set_last_checked_fn=lambda sid, t: None,
            on_deny_fn=lambda: None,
            chat_id="chat-g",
            session_id="s-g",
            since="0",
            timeout_seconds=5,
            log_fn=None,
            approver_ids={"op1"},
        )
        # Guest's "yes" was ignored, operator's "yes" was accepted
        self.assertEqual(decision, "allow")
        self.assertEqual(ts, "8009")
        self.assertEqual(call_count["n"], 2)

    def test_poll_loop_approver_ids_backward_compat(self):
        """When approver_ids is None, falls back to operator_open_id."""
        def poll_fn(chat_id, since):
            return {
                "replies": [{"text": "yes", "create_time": "9009",
                             "message_id": "m10", "sender_id": "op1"}],
                "takeover": False,
                "error": None,
            }

        decision, ts = permission_core.run_permission_poll_loop(
            poll_fn=poll_fn,
            ack_fn=lambda chat_id, before: None,
            record_received_fn=lambda **kwargs: None,
            set_last_checked_fn=lambda sid, t: None,
            on_deny_fn=lambda: None,
            chat_id="chat-bc",
            session_id="s-bc",
            since="0",
            timeout_seconds=5,
            log_fn=None,
            operator_open_id="op1",
            # approver_ids not passed (defaults to None)
        )
        self.assertEqual(decision, "allow")

    def test_poll_loop_error_uses_backoff(self):
        """Poll errors trigger exponential backoff then recover."""
        logs = []
        call_count = {"n": 0}

        def poll_fn(chat_id, since):
            call_count["n"] += 1
            if call_count["n"] <= 1:
                return {"replies": [], "takeover": False, "error": "transient"}
            return {
                "replies": [{"text": "ok", "create_time": "6006", "message_id": "m6"}],
                "takeover": False,
                "error": None,
            }

        decision, ts = permission_core.run_permission_poll_loop(
            poll_fn=poll_fn,
            ack_fn=lambda chat_id, before: None,
            record_received_fn=lambda **kwargs: None,
            set_last_checked_fn=lambda sid, t: None,
            on_deny_fn=lambda: None,
            chat_id="chat-bo",
            session_id="s-bo",
            since="0",
            timeout_seconds=10,
            log_fn=lambda msg: logs.append(msg),
        )
        self.assertEqual(decision, "allow")
        self.assertTrue(any("backoff" in l for l in logs))


class PermissionCardsTest(unittest.TestCase):
    def test_send_permission_cards(self):
        calls = {"build": [], "send": []}

        class FakeLark:
            @staticmethod
            def build_card(title, body=None, color=None, buttons=None, chat_id=None, nonce=None,
                           extra_value=None):
                calls["build"].append(
                    {
                        "title": title,
                        "body": body,
                        "color": color,
                        "buttons": buttons,
                        "chat_id": chat_id,
                        "nonce": nonce,
                        "extra_value": extra_value,
                    }
                )
                return {"title": title, "body": body}

            @staticmethod
            def send_message(token, chat_id, card):
                calls["send"].append({"token": token, "chat_id": chat_id, "card": card})

        permission_core.send_permission_request_card(
            FakeLark, "tok", "chat-9", "Bash", "do work"
        )
        permission_core.send_permission_denied_card(FakeLark, "tok", "chat-9", "Bash")

        self.assertEqual(len(calls["build"]), 2)
        self.assertEqual(calls["build"][0]["title"], "Permission Request")
        self.assertEqual(calls["build"][1]["title"], "Permission Denied")
        self.assertEqual(len(calls["send"]), 2)

class ResolvePermissionContextTest(unittest.TestCase):
    """Tests for resolve_permission_context.

    The function now imports get_session from handoff_db and
    is_valid_chat_id/load_credentials/load_worker_url from handoff_config
    directly, so we patch those modules on permission_core. Only
    get_tenant_token still goes through the lark_im_mod parameter.
    """

    def _make_fake(self, *, token="tok", token_error=None):
        """Build a minimal FakeLark that only provides get_tenant_token."""
        class FakeLark:
            @staticmethod
            def get_tenant_token(app_id, app_secret):
                if token_error:
                    raise RuntimeError(token_error)
                return token
        return FakeLark

    def _patch_deps(self, *, session=None, creds=None,
                    worker_url="https://w.example"):
        """Patch handoff_db/handoff_config functions on permission_core."""
        import unittest.mock as _m

        patches = [
            _m.patch.object(permission_core.handoff_db, "get_session",
                            return_value=session),
            _m.patch.object(permission_core.handoff_config, "is_valid_chat_id",
                            side_effect=lambda cid: bool(cid) and isinstance(cid, str) and len(cid) <= 128),
            _m.patch.object(permission_core.handoff_config, "load_credentials",
                            return_value=creds),
            _m.patch.object(permission_core.handoff_config, "load_worker_url",
                            return_value=worker_url),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_no_session_id(self):
        fake = self._make_fake()
        self._patch_deps()
        c = permission_core.resolve_permission_context(fake, "")
        self.assertFalse(c["ok"])
        self.assertEqual(c["error"], "no_session_id")

    def test_inactive_session(self):
        fake = self._make_fake()
        self._patch_deps(session=None)
        c = permission_core.resolve_permission_context(fake, "gone")
        self.assertFalse(c["ok"])
        self.assertEqual(c["error"], "inactive")

    def test_no_chat_id(self):
        fake = self._make_fake()
        self._patch_deps(session={"chat_id": ""})
        c = permission_core.resolve_permission_context(fake, "s1")
        self.assertFalse(c["ok"])
        self.assertEqual(c["error"], "no_chat_id")

    def test_invalid_chat_id(self):
        fake = self._make_fake()
        self._patch_deps(session={"chat_id": "x" * 200})
        c = permission_core.resolve_permission_context(fake, "s1")
        self.assertFalse(c["ok"])
        self.assertEqual(c["error"], "invalid_chat_id")

    def test_no_credentials(self):
        fake = self._make_fake()
        self._patch_deps(session={"chat_id": "c1"}, creds=None)
        c = permission_core.resolve_permission_context(fake, "s1")
        self.assertFalse(c["ok"])
        self.assertEqual(c["error"], "no_credentials")
        self.assertEqual(c["chat_id"], "c1")

    def test_token_error(self):
        fake = self._make_fake(token_error="bad creds")
        self._patch_deps(
            session={"chat_id": "c1"},
            creds={"app_id": "a", "app_secret": "b"},
        )
        c = permission_core.resolve_permission_context(fake, "s1")
        self.assertFalse(c["ok"])
        self.assertEqual(c["error"], "token_error")
        self.assertIn("bad creds", c["error_detail"])

    def test_no_worker_url(self):
        fake = self._make_fake()
        self._patch_deps(
            session={"chat_id": "c1"},
            creds={"app_id": "a", "app_secret": "b"},
            worker_url=None,
        )
        c = permission_core.resolve_permission_context(fake, "s1")
        self.assertFalse(c["ok"])
        self.assertEqual(c["error"], "no_worker_url")
        self.assertEqual(c["token"], "tok")

    def test_ok(self):
        fake = self._make_fake()
        self._patch_deps(
            session={"chat_id": "c1"},
            creds={"app_id": "a", "app_secret": "b"},
        )
        c = permission_core.resolve_permission_context(fake, "s1")
        self.assertTrue(c["ok"])
        self.assertEqual(c["chat_id"], "c1")
        self.assertEqual(c["token"], "tok")
        self.assertEqual(c["worker_url"], "https://w.example")


if __name__ == "__main__":
    unittest.main()
