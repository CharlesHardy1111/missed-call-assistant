import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from xml.etree import ElementTree

# app:app initializes on import; isolate that database too.
_import_db = tempfile.TemporaryDirectory()
os.environ["DATABASE_PATH"] = str(Path(_import_db.name) / "import.db")
os.environ["ENABLE_DEV_ROUTES"] = "false"
from app import app as deployed_app, create_app
from database import get_calls, init_db


class AppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = create_app({
            "TESTING": True,
            "DATABASE_PATH": str(Path(self.temp.name) / "calls.db"),
            "BUSINESS_PHONE": "+15555550100",
            "ENABLE_DEV_ROUTES": False,
        })
        self.client = self.app.test_client()

    def calls(self):
        with self.app.app_context():
            return get_calls()

    def test_production_routes_and_templates(self):
        self.assertFalse(deployed_app.debug)
        for route in ("/missed-call", "/voice/trial-test"):
            self.assertEqual(self.client.post(route).status_code, 404)
        self.assertNotIn(b"simulateCall", self.client.get("/").data)
        for route, title in (("/privacy", b"Privacy Policy"), ("/terms", b"Terms & Conditions")):
            response = self.client.get(route)
            self.assertEqual(response.status_code, 200)
            self.assertIn(title, response.data)
            self.assertIn(b"September 25, 2026", response.data)

    def test_opt_in_development_routes(self):
        app = create_app({**self.app.config, "ENABLE_DEV_ROUTES": True})
        client = app.test_client()
        self.assertIn(b"simulateCall", client.get("/").data)
        self.assertEqual(client.post("/missed-call", json={}).status_code, 400)
        self.assertEqual(client.post("/missed-call", json={"phone_number": "+15555550101"}).status_code, 201)
        self.assertEqual(client.post("/voice/trial-test").status_code, 200)

    def test_missed_statuses_and_duplicate_events(self):
        for status in ("no-answer", "busy", "failed", "canceled"):
            payload = {"phone_number": "+15555550101", "call_status": status.upper(), "external_call_id": status}
            response = self.client.post("/call-event", json=payload)
            self.assertEqual(response.status_code, 201)
            self.assertTrue(self.client.post("/call-event", data=payload).json["duplicate"])
        self.assertEqual(len(self.calls()), 4)
        self.assertTrue(all(row["follow_up_status"] == "pending" for row in self.calls()))

    def test_answered_and_intermediate_events_do_not_create_leads(self):
        for status in ("completed", "in-progress", "ringing", "answered", "unknown"):
            response = self.client.post("/call-event", json={"phone_number": "123", "call_status": status})
            self.assertFalse(response.json["recorded"])
            response = self.client.post("/provider/call-status", data={"From": "123", "CallStatus": status})
            self.assertFalse(response.json["recorded"])
        self.assertEqual(self.calls(), [])

    def test_validation_and_missing_ids(self):
        for payload in ({}, [], ["bad"], {"phone_number": "123"}):
            self.assertEqual(self.client.post("/call-event", json=payload).status_code, 400)
        self.assertEqual(self.client.post("/provider/call-status", data={"CallStatus": "busy"}).status_code, 400)
        self.assertEqual(self.client.post("/voice/dial-result", data={"DialCallStatus": "busy"}).status_code, 400)
        for _ in range(2):
            self.assertEqual(self.client.post("/call-event", json={"phone_number": "123", "call_status": "busy"}).status_code, 201)
        self.assertEqual(len(self.calls()), 2)

    def test_voice_forwarding(self):
        response = self.client.post("/voice/incoming")
        self.assertEqual(response.mimetype, "text/xml")
        dial = ElementTree.fromstring(response.data).find("Dial")
        self.assertEqual(dial.attrib, {"action": "/voice/dial-result", "method": "POST", "timeout": "20"})
        self.assertEqual(dial.find("Number").text, "+15555550100")
        self.assertEqual(dial.find("Number").attrib, {
            "statusCallback": "/voice/dial-result",
            "statusCallbackMethod": "POST",
            "statusCallbackEvent": "completed",
        })
        self.app.config["BUSINESS_PHONE"] = "123&456"
        self.assertEqual(ElementTree.fromstring(self.client.post("/voice/incoming").data).find("Dial/Number").text, "123&456")
        self.app.config["BUSINESS_PHONE"] = None
        self.assertEqual(self.client.post("/voice/incoming").status_code, 503)

    def test_voice_results_and_cross_endpoint_deduplication(self):
        for status in ("no-answer", "busy", "failed", "canceled"):
            payload = {"From": "123", "DialCallStatus": status, "CallSid": status}
            for _ in range(2):
                response = self.client.post("/voice/dial-result", data=payload)
                self.assertEqual(response.status_code, 200)
                self.assertIn("Sorry we missed", ElementTree.fromstring(response.data).find("Say").text)
            response = self.client.post("/provider/call-status", data={"From": "123", "CallStatus": status, "CallSid": status})
            self.assertTrue(response.json["duplicate"])
        for status in ("completed", "answered", ""):
            root = ElementTree.fromstring(self.client.post("/voice/dial-result", data={"DialCallStatus": status}).data)
            self.assertEqual(list(root), [])
        self.assertEqual(len(self.calls()), 4)

    def test_provider_records_and_maps_answered(self):
        response = self.client.post("/provider/call-status", data={"From": "123", "CallStatus": "BUSY", "CallSid": "provider"})
        self.assertEqual(response.status_code, 201)
        response = self.client.post("/provider/call-status", data={"From": "123", "CallStatus": "completed"})
        self.assertEqual(response.json["call_status"], "answered")
        self.assertEqual(self.calls()[0]["caller_name"], "Incoming Caller")

    def test_number_no_answer_callback_alone_reaches_dashboard(self):
        # Follow the callback URL from the actual incoming TwiML. No Dial action
        # is delivered in this scenario; the child leg must still create a lead.
        incoming = self.client.post("/voice/incoming", data={
            "From": "+15555550101", "CallSid": "parent",
        })
        number = ElementTree.fromstring(incoming.data).find("Dial/Number")
        self.assertEqual(self.calls(), [])
        with self.assertLogs(self.app.logger, level="INFO") as logs:
            response = self.client.post(number.attrib["statusCallback"], data={
                "From": "+15555550101", "To": "+15555550100",
                "CallSid": "child-private-id", "ParentCallSid": "parent-private-id",
                "CallStatus": "no-answer", "Direction": "outbound-dial",
            })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(ElementTree.fromstring(response.data)), [])
        row = self.calls()[0]
        self.assertEqual(row["phone_number"], "+15555550101")
        self.assertEqual(row["external_call_id"], "parent-private-id")
        self.assertEqual(row["call_status"], "no-answer")
        self.assertEqual(row["follow_up_status"], "pending")
        page = self.client.get("/").data
        self.assertIn(b"+15555550101", page)
        self.assertNotIn(b"No missed calls yet", page)
        output = " ".join(logs.output)
        self.assertIn("outcome=recorded", output)
        for private in ("+15555550101", "+15555550100", "parent-private-id", "child-private-id"):
            self.assertNotIn(private, output)

    def test_action_and_number_callback_deduplicate_in_either_order(self):
        for reverse in (False, True):
            parent = f"parent-{reverse}"
            payloads = [
                {"From": "123", "CallSid": parent, "DialCallSid": "child",
                 "CallStatus": "in-progress", "DialCallStatus": "no-answer"},
                {"From": "123", "CallSid": "child", "ParentCallSid": parent,
                 "CallStatus": "no-answer"},
            ]
            if reverse:
                payloads.reverse()
            for payload in payloads * 2:
                self.assertEqual(self.client.post("/voice/dial-result", data=payload).status_code, 200)
        self.assertEqual(len(self.calls()), 2)

    def test_number_callback_statuses_and_parent_validation(self):
        for status in ("no-answer", "busy", "failed", "canceled"):
            payload = {"From": "123", "CallSid": f"child-{status}", "CallStatus": status}
            self.assertEqual(self.client.post("/voice/dial-result", data=payload).status_code, 400)
            payload["ParentCallSid"] = status
            self.assertEqual(self.client.post("/voice/dial-result", data=payload).status_code, 200)
        for status in ("completed", "answered", "in-progress", "ringing", ""):
            response = self.client.post("/voice/dial-result", data={
                "From": "123", "CallSid": "child", "ParentCallSid": "answered-parent",
                "CallStatus": status,
            })
            self.assertEqual(response.status_code, 200)
            self.assertEqual(list(ElementTree.fromstring(response.data)), [])
        self.assertEqual(len(self.calls()), 4)

    def test_action_no_answer_uses_dial_status_not_parent_status(self):
        incoming = self.client.post("/voice/incoming")
        dial = ElementTree.fromstring(incoming.data).find("Dial")
        response = self.client.post(dial.attrib["action"], data={
            "From": "+15555550101", "CallSid": "parent", "DialCallSid": "child",
            "CallStatus": "completed", "DialCallStatus": "no-answer",
        })
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(ElementTree.fromstring(response.data).find("Say"))
        self.assertEqual(self.calls()[0]["external_call_id"], "parent")
        self.assertIn(b"+15555550101", self.client.get("/").data)

    def test_simultaneous_voice_callbacks_create_one_lead(self):
        barrier = Barrier(2)
        payloads = [
            {"From": "123", "CallSid": "parent", "DialCallStatus": "no-answer"},
            {"From": "123", "CallSid": "child", "ParentCallSid": "parent", "CallStatus": "no-answer"},
        ]

        def post(payload):
            with self.app.test_client() as client:
                barrier.wait(timeout=5)
                return client.post("/voice/dial-result", data=payload).status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(list(pool.map(post, payloads)), [200, 200])
        self.assertEqual(len(self.calls()), 1)

    def test_legacy_database_migration_preserves_rows(self):
        legacy = str(Path(self.temp.name) / "legacy.db")
        with sqlite3.connect(legacy) as conn:
            conn.execute("CREATE TABLE missed_calls (id INTEGER PRIMARY KEY AUTOINCREMENT, phone_number TEXT NOT NULL, caller_name TEXT, time_received TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'missed', follow_up_status TEXT NOT NULL DEFAULT 'pending', follow_up_message TEXT NOT NULL)")
            conn.execute("INSERT INTO missed_calls (phone_number, time_received, follow_up_message) VALUES ('123', 'old date', 'draft')")
        conn.close()
        self.app.config["DATABASE_PATH"] = legacy
        with self.app.app_context():
            init_db()
            init_db()
        rows = self.calls()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["phone_number"], "123")
        self.assertIsNone(rows[0]["external_call_id"])


if __name__ == "__main__":
    unittest.main()
