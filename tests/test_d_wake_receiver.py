"""(d) Wake receiver against the real tui_gateway.server prompt.submit.

Only the provider/agent run boundary (tui_gateway.server._run_after_agent_ready) and UI emission
(_emit) are stubbed. The session record comes from the native _deferred_session_record factory.
"""
from __future__ import annotations

from contextlib import ExitStack, closing
import importlib.util
import json
import sqlite3
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import _support as s

SERVER_SYMBOLS = ("_sessions", "_sessions_lock", "_methods", "_session_lookup_key", "_session_pending_kind",
                  "_session_compression_in_flight", "_child_run_active", "_session_profile_runtime_scope",
                  "_emit", "_run_after_agent_ready", "_release_active_session_slot", "_deferred_session_record",
                  "_hermes_home")
# Session fields wake_receiver.Receiver reads from a live tui_gateway session record.
SESSION_FIELDS = ("agent", "agent_ready", "running", "history_lock", "profile_home", "session_key",
                  "attached_images", "lazy")
A = ("default", "session-a")
B = ("lab", "session-b")


def _load(name):
    spec = importlib.util.spec_from_file_location("smb_ci_" + name, s.REPO / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


wake_receiver = _load("wake_receiver")
Mailbox = _load("mailbox_store").Mailbox
WakeStore = _load("wake_store").WakeStore


def send_args(body="UNTRUSTED BODY MUST NOT BE SUBMITTED", key="one"):
    return {"sender_profile": A[0], "sender_session": A[1], "recipient_profile": B[0],
            "recipient_session": B[1], "body": body, "key": key}


class NativeReceiverTests(unittest.TestCase):
    def setUp(self):
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override
        from hermes_state import SessionDB
        from tui_gateway import server
        missing = [name for name in SERVER_SYMBOLS if not hasattr(server, name)]
        self.assertEqual(missing, [], f"tui_gateway.server no longer provides {missing}")
        self.assertIn("prompt.submit", server._methods, "tui_gateway.server._methods['prompt.submit'] is gone")
        self.server = server

        self.base = s.BASE / ("wake-" + self._testMethodName)
        self.root = self.base / "hermes"
        self.home = self.root / "profiles" / B[0]
        self.db = self.base / "mailbox.sqlite3"
        for home, session in ((self.root, A[1]), (self.home, B[1])):
            (home / "runtime").mkdir(parents=True)
            (home / "config.yaml").write_text("# CI fixture profile\n", encoding="utf-8")
            (home / "runtime" / "active_sessions.json").write_text('{"entries": []}', encoding="utf-8")
            with closing(SessionDB(home / "state.db")) as state:
                state.create_session(session, source="tui")
        # No per-test removal: native helpers may still hold this tree briefly; tests/run.py removes
        # the whole fixture tree after the test process exits and asserts it is gone.
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        token = set_hermes_home_override(self.root)
        self.addCleanup(reset_hermes_home_override, token)

        record = server._deferred_session_record(B[1], cols=80, cwd=str(self.home), history=[], lease=None,
                                                 profile_home=self.home)
        missing = [key for key in SESSION_FIELDS if key not in record]
        self.assertEqual(missing, [], f"tui_gateway.server._deferred_session_record no longer has {missing}")
        record["agent"] = SimpleNamespace(session_id=B[1], model="ci-provider-boundary")
        record["agent_ready"].set()
        record["agent_build_started"] = True
        record["transport"] = None
        self.session = record
        self.stack.enter_context(patch.object(server, "_hermes_home", self.root))
        self.stack.enter_context(patch.object(server, "_sessions", {"native-live-b": self.session}))
        self.events = self.stack.enter_context(patch.object(server, "_emit"))
        self.ran = threading.Event()
        self.stack.enter_context(patch.object(server, "_run_after_agent_ready",
                                              side_effect=lambda *a, **k: self.ran.set()))
        self.mailbox = Mailbox(self.db, self.root)
        self.wakes = WakeStore(self.db, self.root)
        self.receiver = wake_receiver.Receiver(server, self.home, self.root, self.db,
                                               mailbox=self.mailbox, wakes=self.wakes)
        self.addCleanup(self.receiver.stop)
        self.addCleanup(server._release_active_session_slot, self.session)

    def message(self, key="one"):
        row = self.mailbox.send(**send_args(key=key))
        self.wakes.request(row)
        return row

    def test_real_prompt_submit_persists_exact_recipient_turn(self):
        row = self.message()
        native = self.server._methods["prompt.submit"]
        responses = []

        def observed(*args, **kwargs):
            responses.append(native(*args, **kwargs))
            return responses[-1]

        with patch.dict(self.server._methods, {"prompt.submit": Mock(wraps=observed)}) as methods:
            self.receiver.tick()
            self.assertTrue(self.ran.wait(10), ("prompt.submit never reached the agent run boundary",
                                                self.wakes.status(row["id"]), responses))
            state = self.wakes.status(row["id"])
            self.assertEqual(state["state"], "submitted",
                             f"tui_gateway prompt.submit refused the receiver's params: {state} {responses}")
            calls = methods["prompt.submit"].call_args_list
            self.assertEqual(len(calls), 1)
            params = calls[0].args[1]
            self.assertEqual(params["session_id"], "native-live-b")
            self.assertTrue(params["queued"])
            self.assertNotIn("UNTRUSTED BODY", params["text"])
            self.assertEqual(json.loads(params["text"].splitlines()[1]),
                             {"profile": B[0], "session": B[1], "message_id": row["id"]})
            notices = [c.args for c in self.events.call_args_list if c.args[0] == "notification.show"]
            self.assertEqual([(n[1], n[2]["id"]) for n in notices], [("native-live-b", row["id"])])
            self.session["running"] = False
            self.receiver.tick()
            self.assertEqual(methods["prompt.submit"].call_count, 1, "a submitted wake must not repeat")
        self.assertEqual(self.mailbox.status(row["id"])["status"], "queued", "a wake is not an ACK")
        with closing(sqlite3.connect(self.home / "state.db")) as db:
            rows = db.execute("SELECT content FROM messages WHERE session_id=? AND role='user'", (B[1],)).fetchall()
        self.assertEqual([r[0] for r in rows], [params["text"]], "native SessionDB messages row for the turn")

    def test_busy_approval_attachment_and_other_profile_defer(self):
        row = self.message()
        with patch.dict(self.server._methods, {"prompt.submit": Mock()}) as methods:
            for flag, value in (("running", True), ("attached_images", ["draft.png"]), ("_closing", True),
                                ("queued_prompt", {"text": "user"}), ("resume_hydrating", True)):
                previous = self.session.get(flag)
                self.session[flag] = value
                self.receiver.tick()
                self.session[flag] = previous
            with patch.object(self.server, "_session_pending_kind", return_value="approval.request"):
                self.receiver.tick()
            self.session["profile_home"] = str(self.root)
            self.receiver.tick()
            self.session["profile_home"] = str(self.home)
            self.assertEqual(methods["prompt.submit"].call_count, 0)
        self.assertEqual(self.wakes.status(row["id"])["state"], "pending")

    def test_ack_before_dispatch_and_ambiguous_result_never_retried(self):
        row = self.message("acked")
        self.mailbox.ack(B[0], B[1], [row["id"]])
        self.receiver.tick()
        self.assertEqual(self.wakes.status(row["id"])["state"], "acknowledged")
        other = self.message("ambiguous")
        with patch.dict(self.server._methods, {"prompt.submit": Mock(side_effect=OSError("after send"))}) as methods, \
                self.assertLogs("session_mailbox.wake", level="ERROR"):
            self.receiver.tick()
            self.receiver.tick()
            self.assertEqual(methods["prompt.submit"].call_count, 1)
        self.assertEqual(self.wakes.status(other["id"])["state"], "unknown")
        self.assertFalse(self.wakes.claim(other["id"], "restart"))

    def test_race_to_busy_uses_native_queue(self):
        row = self.message("busy-race")
        claim = self.wakes.claim

        def busy_after_claim(*args):
            result = claim(*args)
            self.session["running"] = True
            return result

        with patch.object(self.wakes, "claim", side_effect=busy_after_claim):
            self.receiver.tick()
        state = self.wakes.status(row["id"])
        self.assertEqual(state["state"], "queued", f"prompt.submit queued path changed: {state}")
        self.assertFalse(self.ran.is_set())

    def test_start_duplicate_and_unload_leave_no_monitor(self):
        dispose = wake_receiver.start_receiver(self.home, self.root, self.db)
        self.assertIsNotNone(dispose, "wake_receiver.start_receiver found no usable tui_gateway.server")
        self.addCleanup(dispose)
        (first,) = wake_receiver._MONITORS.values()
        self.assertTrue(first.thread.is_alive())
        self.assertIsNone(wake_receiver.start_receiver(self.home, self.root, self.db))
        dispose()
        self.assertEqual(wake_receiver._MONITORS, {})
        self.assertFalse(first.thread.is_alive(), "disposed monitor thread still running")
        again = wake_receiver.start_receiver(self.home, self.root, self.db)
        self.assertIsNotNone(again)
        (second,) = wake_receiver._MONITORS.values()
        again()
        self.assertEqual(wake_receiver._MONITORS, {})
        self.assertFalse(second.thread.is_alive(), "disposed monitor thread still running")


if __name__ == "__main__":
    unittest.main()
