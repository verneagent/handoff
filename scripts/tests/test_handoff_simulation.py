#!/usr/bin/env python3

import os
import tempfile
import threading
import unittest

import sys

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPT_DIR)

import handoff_db


class HandoffSimulationTest(unittest.TestCase):
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

        self.db_path = handoff_db._db_path()
        handoff_db._db_initialized.discard(self.db_path)
        conn = handoff_db._get_db()
        conn.close()

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._old_home

        if self._old_project is None:
            os.environ.pop("HANDOFF_PROJECT_DIR", None)
        else:
            os.environ["HANDOFF_PROJECT_DIR"] = self._old_project

        if self._old_tool is None:
            os.environ.pop("HANDOFF_SESSION_TOOL", None)
        else:
            os.environ["HANDOFF_SESSION_TOOL"] = self._old_tool

        self.tmp.cleanup()

    def test_chat_claim_race(self):
        barrier = threading.Barrier(2)
        results = {}

        def _claim(session_id):
            barrier.wait()
            ok, owner = handoff_db.try_claim_chat(session_id, "chat-race", "opus")
            results[session_id] = (ok, owner)

        t1 = threading.Thread(target=_claim, args=("s1",))
        t2 = threading.Thread(target=_claim, args=("s2",))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        self.assertEqual(len(results), 2)
        winners = [sid for sid, (ok, _) in results.items() if ok]
        losers = [sid for sid, (ok, _) in results.items() if not ok]
        self.assertEqual(len(winners), 1)
        self.assertEqual(len(losers), 1)

        winner = winners[0]
        loser = losers[0]
        self.assertEqual(results[loser][1], winner)

        active = handoff_db.get_active_sessions()
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["chat_id"], "chat-race")
        self.assertEqual(active[0]["session_id"], winner)

    def test_concurrent_takeover_only_one_wins(self):
        handoff_db.register_session("old", "chat-take", "opus")

        barrier = threading.Barrier(2)
        results = {}

        def _take(session_id):
            barrier.wait()
            ok, owner, replaced = handoff_db.takeover_chat(
                session_id,
                "chat-take",
                "sonnet",
                expected_owner_session_id="old",
            )
            results[session_id] = (ok, owner, replaced)

        t1 = threading.Thread(target=_take, args=("A",))
        t2 = threading.Thread(target=_take, args=("B",))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        winners = [sid for sid, (ok, _, _) in results.items() if ok]
        losers = [sid for sid, (ok, _, _) in results.items() if not ok]
        self.assertEqual(len(winners), 1)
        self.assertEqual(len(losers), 1)

        winner = winners[0]
        loser = losers[0]
        self.assertEqual(results[winner][2], "old")
        self.assertEqual(results[loser][1], winner)

        active = handoff_db.get_active_sessions()
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["chat_id"], "chat-take")
        self.assertEqual(active[0]["session_id"], winner)

    def test_end_and_takeover_concurrent(self):
        handoff_db.register_session("old", "chat-end", "opus")

        barrier = threading.Barrier(2)
        takeover_result = {}

        def _end_old():
            barrier.wait()
            handoff_db.deactivate_handoff("old")

        def _take_new():
            barrier.wait()
            ok, owner, _ = handoff_db.takeover_chat(
                "new",
                "chat-end",
                "sonnet",
                expected_owner_session_id="old",
            )
            takeover_result["ok"] = ok
            takeover_result["owner"] = owner

        t1 = threading.Thread(target=_end_old)
        t2 = threading.Thread(target=_take_new)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        self.assertTrue(takeover_result.get("ok"))
        self.assertEqual(takeover_result.get("owner"), "new")
        sess = handoff_db.get_session("new")
        if sess is None:
            self.fail("expected new owner session after concurrent end/takeover")
        self.assertEqual(sess["chat_id"], "chat-end")

    def test_concurrent_takeover_without_expected_owner(self):
        barrier = threading.Barrier(2)
        results = {}

        def _take(session_id):
            barrier.wait()
            ok, owner, replaced = handoff_db.takeover_chat(
                session_id,
                "chat-noexp",
                "sonnet",
                expected_owner_session_id=None,
            )
            results[session_id] = (ok, owner, replaced)

        t1 = threading.Thread(target=_take, args=("A",))
        t2 = threading.Thread(target=_take, args=("B",))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        winners = [sid for sid, (ok, _, _) in results.items() if ok]
        losers = [sid for sid, (ok, _, _) in results.items() if not ok]
        self.assertEqual(len(winners), 1)
        self.assertEqual(len(losers), 1)

        winner = winners[0]
        loser = losers[0]
        self.assertEqual(results[loser][1], winner)

        active = handoff_db.get_active_sessions()
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["chat_id"], "chat-noexp")
        self.assertEqual(active[0]["session_id"], winner)


if __name__ == "__main__":
    unittest.main()
