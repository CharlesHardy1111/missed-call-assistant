"""Boundary checks for the production 200-response / missing-lead investigation."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from xml.etree import ElementTree

from test_app import create_app
from database import get_calls


class PersistenceDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = {
            'TESTING': True, 'DATABASE_PATH': str(Path(self.temp.name) / 'calls.db'),
            'BUSINESS_PHONE': '+15555550100', 'BUSINESS_TIMEZONE': 'America/Phoenix',
            'ENABLE_SMS_FOLLOWUP': False, 'ENABLE_DEV_ROUTES': False,
        }
        self.app = create_app(self.config)
        self.client = self.app.test_client()
        sender = patch('followup.send_sms', side_effect=AssertionError('SMS forbidden'))
        self.send = sender.start()
        self.addCleanup(sender.stop)

    def calls(self):
        with self.app.app_context():
            return get_calls()

    def incoming(self, parent, phone):
        response = self.client.post('/voice/incoming', data={
            'CallSid': parent, 'From': phone, 'CallStatus': 'ringing',
        })
        self.assertEqual(response.status_code, 200)
        dial = ElementTree.fromstring(response.data).find('Dial')
        self.assertEqual(dial.get('timeout'), '20')
        self.assertEqual(dial.find('Number').text, self.config['BUSINESS_PHONE'])
        return dial.get('action'), dial.find('Number').get('statusCallback')

    def test_completed_action_and_child_match_empty_live_response_without_lead(self):
        action, number = self.incoming('parent', '+15555550101')
        callbacks = (
            (number, {'CallSid': 'child', 'ParentCallSid': 'parent', 'CallStatus': 'completed'}),
            (action, {'CallSid': 'parent', 'DialCallSid': 'child',
                      'CallStatus': 'in-progress', 'DialCallStatus': 'completed'}),
        )
        for path, data in callbacks:
            response = self.client.post(path, data={**data, 'From': '+15555550101'})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(response.data), 61)
            self.assertIsNone(ElementTree.fromstring(response.data).find('Say'))
        self.assertEqual(self.calls(), [])
        self.assertIn(b'No missed calls yet.', self.client.get('/').data)
        self.send.assert_not_called()

    def test_signed_missed_sequences_commit_and_render_in_both_orders(self):
        count = 0
        for status in ('no-answer', 'busy', 'failed', 'canceled'):
            for reverse in (False, True):
                for event_time in (None, 'Thu, 01 Oct 2026 21:19:47 GMT'):
                    with self.subTest(status=status, reverse=reverse, event_time=event_time):
                        count += 1
                        parent, child = f'parent-{count}', f'child-{count}'
                        phone = f'+1555555{count:04d}'
                        action, number = self.incoming(parent, phone)
                        callbacks = [
                            (action, {'CallSid': parent, 'DialCallSid': child,
                                      'CallStatus': 'in-progress', 'DialCallStatus': status}),
                            (number, {'CallSid': child, 'ParentCallSid': parent, 'CallStatus': status}),
                        ]
                        if event_time:
                            callbacks[1][1]['Timestamp'] = event_time
                        if reverse:
                            callbacks.reverse()
                        for path, data in callbacks * 2:
                            response = self.client.post(path, data={**data, 'From': phone})
                            self.assertEqual(response.status_code, 200)
                            if 'DialCallStatus' in data:
                                self.assertIsNotNone(ElementTree.fromstring(response.data).find('Say'))
                                self.assertNotEqual(len(response.data), 61)
                            else:
                                self.assertEqual(len(response.data), 61)
                        rows = self.calls()
                        self.assertEqual(len(rows), count)
                        self.assertEqual(rows[0]['external_call_id'], parent)
                        self.assertEqual(rows[0]['call_status'], status)
                        self.assertEqual(rows[0]['follow_up_status'], 'disabled')
                        self.assertTrue(rows[0]['time_received'].endswith('+00:00'))
                        page = self.client.get('/')
                        self.assertEqual(page.status_code, 200)
                        self.assertEqual(page.data.count(b'class="call-card"'), count)
                        self.assertIn(phone.encode(), page.data)
                        if event_time:
                            self.assertEqual(rows[0]['call_event_at'], '2026-10-01T21:19:47+00:00')
                            self.assertIn(b'Oct 01, 2026 02:19 PM MST (America/Phoenix)', page.data)
                        else:
                            self.assertIsNone(rows[0]['call_event_at'])
                            self.assertIn(b'Recorded:', page.data)
        self.send.assert_not_called()

    def test_separate_app_instance_reads_committed_callback_row_from_same_file(self):
        self.client.post('/voice/dial-result', data={
            'CallSid': 'parent', 'From': '+15555550101', 'DialCallStatus': 'no-answer',
        })
        other_app = create_app(self.config)
        page = other_app.test_client().get('/')
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.data.count(b'class="call-card"'), 1)
        self.assertIn(b'+15555550101', page.data)
        self.send.assert_not_called()
