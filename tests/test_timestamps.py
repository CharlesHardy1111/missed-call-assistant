from datetime import datetime, timezone
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfoNotFoundError

from test_app import create_app
from database import get_calls, init_db, get_connection
from timestamps import display_timestamp, twilio_event_time, utc_now


class TimestampTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = create_app({
            'TESTING': True, 'DATABASE_PATH': str(Path(self.temp.name) / 'calls.db'),
            'BUSINESS_PHONE': '+15555550100', 'BUSINESS_TIMEZONE': 'America/Phoenix',
            'ENABLE_SMS_FOLLOWUP': False, 'ENABLE_DEV_ROUTES': False,
        })
        self.client = self.app.test_client()
        network = patch('sms._open', side_effect=AssertionError('Network forbidden'))
        self.network = network.start()
        self.addCleanup(network.stop)

    def calls(self):
        with self.app.app_context():
            return get_calls()

    def action(self):
        return self.client.post('/voice/dial-result', data={
            'CallSid': 'parent', 'From': '+15555550101', 'DialCallStatus': 'no-answer',
        })

    def number(self, timestamp='Thu, 01 Oct 2026 19:22:30 +0000'):
        return self.client.post('/voice/dial-result', data={
            'CallSid': 'child', 'ParentCallSid': 'parent', 'From': '+15555550101',
            'CallStatus': 'no-answer', 'Timestamp': timestamp,
        })

    def test_utc_clock_requests_aware_time_and_serializes_offset(self):
        expected = datetime(2026, 10, 1, 19, 22, 30, 123456, tzinfo=timezone.utc)
        with patch('timestamps.datetime') as clock:
            clock.now.return_value = expected
            self.assertEqual(utc_now(), '2026-10-01T19:22:30.123456+00:00')
            clock.now.assert_called_once_with(timezone.utc)

    def test_receipt_storage_and_html_serialization_are_separate_from_display(self):
        utc = '2026-10-01T19:22:30.123456+00:00'
        with patch('database.utc_now', return_value=utc):
            self.assertEqual(self.action().status_code, 200)
        row = self.calls()[0]
        self.assertEqual(row['time_received'], utc)
        self.assertIsNone(row['call_event_at'])
        with self.app.app_context():
            conn = get_connection()
            self.addCleanup(conn.close)
            columns = {r['name']: r['type'] for r in conn.execute('PRAGMA table_info(missed_calls)')}
            self.assertEqual(columns['time_received'], 'TEXT')
            self.assertEqual(columns['call_event_at'], 'TEXT')
        page = self.client.get('/').get_data(as_text=True)
        self.assertIn(f'datetime="{utc}"', page)
        self.assertIn('Oct 01, 2026 12:22 PM MST (America/Phoenix)', page)
        self.assertIn('Recorded:', page)
        self.assertEqual(self.calls()[0]['time_received'], utc)
        self.network.assert_not_called()

    def test_provider_event_time_is_used_instead_of_delayed_receipt(self):
        with patch('database.utc_now', return_value='2026-10-02T03:43:00+00:00'):
            response = self.client.post('/provider/call-status', data={
                'CallSid': 'parent', 'From': '+15555550101', 'CallStatus': 'no-answer',
                'Timestamp': 'Thu, 01 Oct 2026 19:22:30 GMT',
            })
        self.assertEqual(response.status_code, 201)
        row = self.calls()[0]
        self.assertEqual(row['time_received'], '2026-10-02T03:43:00+00:00')
        self.assertEqual(row['call_event_at'], '2026-10-01T19:22:30+00:00')
        page = self.client.get('/').get_data(as_text=True)
        self.assertIn('Call ended:', page)
        self.assertIn('Oct 01, 2026 12:22 PM', page)

    def test_action_then_number_enriches_once_without_duplicate_followup(self):
        self.assertEqual(self.action().status_code, 200)
        receipt = self.calls()[0]['time_received']
        for stamp in ('Thu, 01 Oct 2026 19:22:30 GMT',
                      'Thu, 01 Oct 2026 19:22:30 GMT',
                      'Thu, 01 Oct 2026 23:43:00 GMT'):
            self.assertEqual(self.number(stamp).status_code, 200)
        rows = self.calls()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['time_received'], receipt)
        self.assertEqual(rows[0]['call_event_at'], '2026-10-01T19:22:30+00:00')
        self.assertEqual(rows[0]['follow_up_status'], 'disabled')
        self.network.assert_not_called()

    def test_number_then_action_and_restart_preserve_event_time(self):
        self.assertEqual(self.number().status_code, 200)
        self.assertEqual(self.action().status_code, 200)
        with self.app.app_context():
            init_db()
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(self.calls()[0]['call_event_at'], '2026-10-01T19:22:30+00:00')

    def test_missing_malformed_and_naive_provider_time_fall_back_safely(self):
        for value in (None, '', 'garbage', 'Thu, 01 Oct 2026 19:22:30',
                      'Thu, 01 Oct 2026 19:22:30 -0000'):
            with self.subTest(value=value):
                self.assertIsNone(twilio_event_time(value))
        self.assertEqual(self.number('garbage').status_code, 200)
        row = self.calls()[0]
        self.assertIsNone(row['call_event_at'])
        self.assertEqual(datetime.fromisoformat(row['time_received']).utcoffset().total_seconds(), 0)
        self.assertIn(b'Recorded:', self.client.get('/').data)

    def test_rfc2822_offset_normalizes_to_utc(self):
        self.assertEqual(twilio_event_time('Thu, 01 Oct 2026 12:22:30 -0700'),
                         '2026-10-01T19:22:30+00:00')

    def test_phoenix_noon_midnight_minutes_and_date_rollover(self):
        cases = (
            ('2026-10-01T06:59:00+00:00', 'Sep 30, 2026 11:59 PM'),
            ('2026-10-01T07:00:00+00:00', 'Oct 01, 2026 12:00 AM'),
            ('2026-10-01T07:01:00+00:00', 'Oct 01, 2026 12:01 AM'),
            ('2026-10-01T18:59:00+00:00', 'Oct 01, 2026 11:59 AM'),
            ('2026-10-01T19:00:00+00:00', 'Oct 01, 2026 12:00 PM'),
            ('2026-10-01T19:01:00+00:00', 'Oct 01, 2026 12:01 PM'),
        )
        for utc, expected in cases:
            with self.subTest(utc=utc):
                self.assertEqual(display_timestamp(utc, 'America/Phoenix')['text'],
                                 expected + ' MST (America/Phoenix)')

    def test_iana_dst_transitions_and_phoenix_no_dst(self):
        cases = (
            ('2026-03-08T06:59:00+00:00', '01:59 AM EST'),
            ('2026-03-08T07:00:00+00:00', '03:00 AM EDT'),
            ('2026-11-01T05:30:00+00:00', '01:30 AM EDT'),
            ('2026-11-01T06:30:00+00:00', '01:30 AM EST'),
        )
        for utc, expected in cases:
            self.assertIn(expected, display_timestamp(utc, 'America/New_York')['text'])
        for month in ('01', '07'):
            self.assertIn('12:00 PM MST', display_timestamp(
                f'2026-{month}-01T19:00:00+00:00', 'America/Phoenix')['text'])

    def test_configured_business_timezone_changes_only_presentation(self):
        self.number()
        before = dict(self.calls()[0])
        self.app.config['BUSINESS_TIMEZONE'] = 'Asia/Kolkata'
        page = self.client.get('/').get_data(as_text=True)
        self.assertIn('Oct 02, 2026 12:52 AM IST (Asia/Kolkata)', page)
        self.assertEqual(dict(self.calls()[0]), before)

    def test_legacy_migration_preserves_ambiguous_timestamp(self):
        path = str(Path(self.temp.name) / 'legacy.db')
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute('CREATE TABLE missed_calls (id INTEGER PRIMARY KEY, phone_number TEXT, '
                         'caller_name TEXT, time_received TEXT, status TEXT, '
                         'follow_up_status TEXT, follow_up_message TEXT)')
            conn.execute("INSERT INTO missed_calls VALUES (1, '123', 'Legacy', ?, 'missed', 'pending', 'old')",
                         ('Oct 01, 2026 12:43 AM',))
        legacy = create_app({**self.app.config, 'DATABASE_PATH': path})
        with legacy.app_context():
            init_db()
            row = get_calls()[0]
            self.assertEqual(row['time_received'], 'Oct 01, 2026 12:43 AM')
            self.assertIsNone(row['call_event_at'])
        page = legacy.test_client().get('/').get_data(as_text=True)
        self.assertIn('Oct 01, 2026 12:43 AM (legacy timestamp; timezone unknown)', page)
        self.assertNotIn('<time datetime=', page)

    def test_invalid_business_timezone_is_rejected_at_startup(self):
        with self.assertRaises(ZoneInfoNotFoundError):
            create_app({**self.app.config, 'BUSINESS_TIMEZONE': 'Invalid/Timezone'})
