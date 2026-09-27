import io
import base64
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs

# Reuse the test module's isolated import, never the local working database.
from test_app import create_app
from database import get_calls
from sms import DEFAULT_MESSAGE, SendResult, send_sms


class FollowUpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = create_app({
            "TESTING": True,
            "DATABASE_PATH": str(Path(self.temp.name) / "calls.db"),
            "BUSINESS_PHONE": "+15555550100",
            "ENABLE_DEV_ROUTES": False,
            "ENABLE_SMS_FOLLOWUP": False,
            "TWILIO_ACCOUNT_SID": "AC" + "0" * 32,
            "TWILIO_AUTH_TOKEN": "fake-test-token-not-a-secret",
            "TWILIO_SMS_FROM": "+15555550102",
        })
        self.client = self.app.test_client()
        # Every test blocks the real transport, including tests enabling the flag.
        self.network = patch("sms._open", side_effect=AssertionError("Real network forbidden in tests"))
        self.open = self.network.start()
        self.addCleanup(self.network.stop)

    def calls(self):
        with self.app.app_context():
            return get_calls()

    def post(self, sid="parent"):
        return self.client.post("/voice/dial-result", data={
            "From": "+15555550101", "CallSid": sid,
            "CallStatus": "completed", "DialCallStatus": "no-answer",
        })

    def accepted_response(self, status="queued"):
        self.open.side_effect = None
        self.open.return_value = io.BytesIO(json.dumps({
            "sid": "SM" + "1" * 32, "status": status,
        }).encode())

    def test_flag_defaults_false_and_requires_explicit_true(self):
        for value in (None, "false", "0", "yes", ""):
            with patch.dict(os.environ, {}, clear=False), patch("app.load_dotenv"):
                if value is None:
                    os.environ.pop("ENABLE_SMS_FOLLOWUP", None)
                else:
                    os.environ["ENABLE_SMS_FOLLOWUP"] = value
                app = create_app({"DATABASE_PATH": self.app.config["DATABASE_PATH"]})
                self.assertIs(app.config["ENABLE_SMS_FOLLOWUP"], False)
        self.open.assert_not_called()

    def test_disabled_creates_lead_without_invoking_send(self):
        with patch("followup.send_sms") as send:
            self.assertEqual(self.post().status_code, 200)
            send.assert_not_called()
        row = self.calls()[0]
        self.assertEqual(row["follow_up_status"], "disabled")
        self.assertIsNone(row["follow_up_attempted_at"])
        self.assertEqual(row["follow_up_message"], DEFAULT_MESSAGE)
        self.assertIn(b"Not attempted / disabled", self.client.get("/").data)
        self.open.assert_not_called()

    def test_transport_itself_obeys_disabled_flag(self):
        with self.app.app_context():
            self.assertEqual(send_sms("+15555550101", DEFAULT_MESSAGE).status, "disabled")
        self.open.assert_not_called()

    def test_success_stores_sent_sid_and_provider_state(self):
        self.app.config["ENABLE_SMS_FOLLOWUP"] = True
        self.accepted_response()
        self.assertEqual(self.post().status_code, 200)
        row = self.calls()[0]
        self.assertEqual(row["follow_up_status"], "sent")
        self.assertEqual(row["follow_up_provider_status"], "queued")
        self.assertEqual(row["follow_up_message_sid"], "SM" + "1" * 32)
        self.assertIsNotNone(row["follow_up_attempted_at"])
        self.assertIsNotNone(row["follow_up_completed_at"])
        self.assertIsNone(row["follow_up_error"])
        request = self.open.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        expected_auth = base64.b64encode((self.app.config["TWILIO_ACCOUNT_SID"] + ":" + self.app.config["TWILIO_AUTH_TOKEN"]).encode()).decode()
        self.assertEqual(request.get_header("Authorization"), "Basic " + expected_auth)
        self.assertTrue(request.full_url.startswith("https://api.twilio.com/2010-04-01/Accounts/"))
        self.assertEqual(parse_qs(request.data.decode()), {
            "To": ["+15555550101"], "From": ["+15555550102"], "Body": [DEFAULT_MESSAGE],
        })
        self.assertIn(b"Sent (accepted by Twilio; delivery unconfirmed)", self.client.get("/").data)

    def test_provider_error_stores_only_safe_failure_information(self):
        self.app.config["ENABLE_SMS_FOLLOWUP"] = True
        private = "fake-token-private-provider-text"
        self.open.side_effect = HTTPError("https://api.twilio.com", 400, private, {},
                                         io.BytesIO(json.dumps({"code": 21610, "message": private}).encode()))
        self.assertEqual(self.post().status_code, 200)
        row = self.calls()[0]
        self.assertEqual(row["follow_up_status"], "failed")
        self.assertEqual(row["follow_up_error"], "provider_rejected")
        self.assertEqual(row["follow_up_error_code"], "21610")
        self.assertNotIn(private, str(dict(row)))
        page = self.client.get("/").data
        self.assertIn(b"Failed", page)
        self.assertNotIn(private.encode(), page)
        self.post()
        self.assertEqual(self.open.call_count, 1)

    def test_timeout_and_unexpected_adapter_errors_do_not_break_voice_or_retry(self):
        self.app.config["ENABLE_SMS_FOLLOWUP"] = True
        for index, error in enumerate((TimeoutError("fake-private-token"), RuntimeError("fake-private-token"))):
            with patch("followup.send_sms", side_effect=error) as send:
                response = self.post(f"parent-{index}")
                self.assertEqual(response.status_code, 200)
                self.assertIn(b"Sorry we missed", response.data)
                self.post(f"parent-{index}")
                send.assert_called_once()
        for row in self.calls():
            self.assertEqual(row["follow_up_status"], "failed")
            self.assertEqual(row["follow_up_error"], "unknown_outcome")
            self.assertNotIn("fake-private-token", str(dict(row)))

    def test_missing_configuration_and_invalid_recipient_fail_without_network(self):
        self.app.config["ENABLE_SMS_FOLLOWUP"] = True
        self.app.config["TWILIO_SMS_FROM"] = ""
        self.post()
        self.assertEqual(self.calls()[0]["follow_up_error"], "configuration_missing")
        self.app.config["TWILIO_SMS_FROM"] = "+15555550102"
        self.client.post("/voice/dial-result", data={"From": "anonymous", "CallSid": "other", "DialCallStatus": "busy"})
        self.assertEqual(self.calls()[0]["follow_up_error"], "invalid_recipient")
        self.open.assert_not_called()

    def test_all_missed_statuses_use_workflow_and_answered_calls_do_not(self):
        self.app.config["ENABLE_SMS_FOLLOWUP"] = True
        with patch("followup.send_sms", return_value=SendResult("sent")) as send:
            for status in ("no-answer", "busy", "failed", "canceled", "completed", "answered", "ringing"):
                self.client.post("/voice/dial-result", data={
                    "From": "+15555550101", "CallSid": status, "DialCallStatus": status,
                })
            self.assertEqual(send.call_count, 4)
        self.assertEqual(len(self.calls()), 4)

    def test_all_production_entry_points_share_one_lead_and_attempt(self):
        self.app.config["ENABLE_SMS_FOLLOWUP"] = True
        with patch("followup.send_sms", return_value=SendResult("sent")) as send:
            self.client.post("/call-event", json={"phone_number": "+15555550101", "external_call_id": "parent", "call_status": "no-answer"})
            self.post()
            self.client.post("/provider/call-status", data={
                "From": "+15555550101", "CallSid": "child", "ParentCallSid": "parent", "CallStatus": "no-answer",
            })
            send.assert_called_once()
        self.assertEqual(len(self.calls()), 1)

    def test_simultaneous_action_and_number_callbacks_send_once(self):
        self.app.config["ENABLE_SMS_FOLLOWUP"] = True
        barrier = Barrier(2)
        payloads = [
            {"From": "+15555550101", "CallSid": "parent", "DialCallStatus": "no-answer"},
            {"From": "+15555550101", "CallSid": "child", "ParentCallSid": "parent", "CallStatus": "no-answer"},
        ]
        def post(payload):
            with self.app.test_client() as client:
                barrier.wait(timeout=5)
                return client.post("/voice/dial-result", data=payload).status_code
        with patch("followup.send_sms", return_value=SendResult("sent")) as send:
            with ThreadPoolExecutor(max_workers=2) as pool:
                self.assertEqual(list(pool.map(post, payloads)), [200, 200])
            self.post()
            send.assert_called_once()
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(self.calls()[0]["follow_up_status"], "sent")

    def test_disabled_records_are_not_sent_after_flag_is_enabled(self):
        self.post()
        self.app.config["ENABLE_SMS_FOLLOWUP"] = True
        with patch("followup.send_sms") as send:
            self.post()
            send.assert_not_called()
        self.assertEqual(self.calls()[0]["follow_up_status"], "disabled")

    def test_missing_id_and_development_simulator_cannot_send(self):
        self.app.config["ENABLE_SMS_FOLLOWUP"] = True
        self.post(sid="")
        self.assertEqual(self.calls()[0]["follow_up_status"], "not_attempted")
        self.assertEqual(self.calls()[0]["follow_up_error"], "missing_call_id")
        app = create_app({**self.app.config, "ENABLE_DEV_ROUTES": True})
        app.test_client().post("/missed-call", json={"phone_number": "+15555550101"})
        self.assertEqual(self.calls()[0]["follow_up_error"], "development_simulator")
        self.open.assert_not_called()

    def test_restart_preserves_sent_state_and_does_not_send_again(self):
        self.app.config["ENABLE_SMS_FOLLOWUP"] = True
        with patch("followup.send_sms", return_value=SendResult("sent")) as send:
            self.post()
            self.app = create_app(dict(self.app.config))
            self.client = self.app.test_client()
            self.post()
            send.assert_called_once()

    def test_database_failure_after_send_does_not_repeat_attempt(self):
        self.app.config["ENABLE_SMS_FOLLOWUP"] = True
        with patch("followup.send_sms", return_value=SendResult("sent")) as send:
            with patch("followup.finish_follow_up", side_effect=sqlite3.OperationalError("unavailable")):
                with self.assertRaises(sqlite3.OperationalError):
                    self.post()
            self.assertEqual(self.calls()[0]["follow_up_status"], "sending")
            self.post()
            send.assert_called_once()

    def test_legacy_pending_rows_never_become_sms_backlog(self):
        legacy = str(Path(self.temp.name) / "legacy.db")
        with sqlite3.connect(legacy) as conn:
            conn.execute("CREATE TABLE missed_calls (id INTEGER PRIMARY KEY, phone_number TEXT, caller_name TEXT, time_received TEXT, status TEXT, follow_up_status TEXT, follow_up_message TEXT, call_status TEXT, external_call_id TEXT)")
            conn.execute("INSERT INTO missed_calls VALUES (1, '+15555550101', 'Caller', 'old', 'missed', 'pending', ?, 'no-answer', 'parent')", (
                "Hi! Sorry we missed your call. We received your message and someone will get back to you shortly. How can we help?",
            ))
        conn.close()
        self.app = create_app({**self.app.config, "DATABASE_PATH": legacy, "ENABLE_SMS_FOLLOWUP": True})
        self.client = self.app.test_client()
        self.post()
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(self.calls()[0]["follow_up_status"], "not_attempted")
        self.assertEqual(self.calls()[0]["follow_up_message"], DEFAULT_MESSAGE)
        self.open.assert_not_called()

    def test_transport_timeout_and_malformed_response_are_safe(self):
        self.app.config["ENABLE_SMS_FOLLOWUP"] = True
        self.open.side_effect = TimeoutError("private-token")
        self.post("timeout")
        self.open.side_effect = None
        self.open.return_value = io.BytesIO(b'{"sid": "invalid-private-value", "status": "queued"}')
        self.post("malformed")
        for row in self.calls():
            self.assertEqual(row["follow_up_error"], "unknown_outcome")
            self.assertIsNone(row["follow_up_message_sid"])

    def test_provider_terminal_failure_is_not_marked_sent(self):
        self.app.config["ENABLE_SMS_FOLLOWUP"] = True
        self.accepted_response("failed")
        self.post()
        self.assertEqual(self.calls()[0]["follow_up_status"], "failed")
        self.assertEqual(self.calls()[0]["follow_up_provider_status"], "failed")
