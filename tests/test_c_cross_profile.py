"""(c) Cross-profile round trip through native registry dispatch in separate Hermes processes."""
from __future__ import annotations

import unittest

import _support as s

KEY = "ci-roundtrip-1"


def send_args(body="hello from alpha", key=KEY):
    return {"sender_profile": s.PROFILE, "sender_session": s.SID_PROFILE, "recipient_profile": "default",
            "recipient_session": s.SID_DEFAULT, "body": body, "key": key}


class CrossProfileRoundTripTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        s.ensure_installed()

    def dispatch(self, home, calls):
        state = s.dispatch_in(home, calls)
        self.assertTrue(state["loaded"] and state["enabled"] and not state["error"],
                        f"plugin not loaded natively in {home.name}: {state}")
        return state["results"]

    def test_send_inbox_ack_status(self):
        sent, retry, conflict = self.dispatch(s.PROFILE_HOME, [
            ("session_mailbox_send", send_args()),
            ("session_mailbox_send", send_args()),
            ("session_mailbox_send", send_args(body="different payload")),
        ])
        self.assertFalse(sent.get("deduplicated"), sent)
        self.assertIsInstance(sent.get("wake"), dict, sent)
        message_id = sent["id"]
        self.assertEqual((retry.get("id"), retry.get("deduplicated")), (message_id, True), "idempotent retry")
        self.assertIn("error", conflict, "key reuse with another payload must fail")
        self.assertIn("conflict", conflict["error"].lower(), conflict)

        wrong_ack, status = self.dispatch(s.PROFILE_HOME, [
            ("session_mailbox_ack", {"profile": s.PROFILE, "session": s.SID_PROFILE, "message_ids": [message_id]}),
            ("session_mailbox_message_status", {"message_id": message_id}),
        ])
        self.assertIn("error", wrong_ack, "ACK by a non-recipient must be rejected")
        self.assertEqual(status.get("status"), "queued", status)

        inbox, ack, again = self.dispatch(s.ROOT, [
            ("session_mailbox_inbox", {"profile": "default", "session": s.SID_DEFAULT}),
            ("session_mailbox_ack", {"profile": "default", "session": s.SID_DEFAULT, "message_ids": [message_id]}),
            ("session_mailbox_ack", {"profile": "default", "session": s.SID_DEFAULT, "message_ids": [message_id]}),
        ])
        bodies = {m["id"]: m.get("body") for m in inbox.get("messages", [])}
        self.assertEqual(bodies.get(message_id), "hello from alpha", inbox)
        self.assertIs(inbox.get("peer_content_untrusted"), True, inbox)
        self.assertEqual(ack.get("acknowledged_count"), 1, ack)
        self.assertEqual(again["messages"][0]["acknowledged_at"], ack["messages"][0]["acknowledged_at"],
                         "repeated ACK must keep the first timestamp")

        (final,) = self.dispatch(s.PROFILE_HOME, [("session_mailbox_message_status", {"message_id": message_id})])
        self.assertEqual(final.get("status"), "acknowledged", final)
        self.assertNotIn("body", final)
        self.assertIn("wake", final)

    def test_invalid_arguments_are_tool_errors(self):
        missing, wrong_type, unknown_session = self.dispatch(s.ROOT, [
            ("session_mailbox_inbox", {"profile": "default"}),
            ("session_mailbox_list_sessions", {"limit": "20"}),
            ("session_mailbox_inbox", {"profile": "default", "session": "no-such-session"}),
        ])
        for label, result in (("missing", missing), ("wrong type", wrong_type), ("unknown", unknown_session)):
            self.assertIn("error", result, f"{label}: {result}")


if __name__ == "__main__":
    unittest.main()
