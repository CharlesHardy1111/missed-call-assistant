from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import io
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs
from xml.etree import ElementTree

from test_app import flask_create_app
from test_webhook_security import signature
from signed_client import TEST_TOKEN, TEST_ORIGIN
from businesses import provision_business
from database import get_connection, init_db
from messaging import reply


class BusinessInboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = flask_create_app({
            'TESTING': True, 'DATABASE_PATH': str(Path(self.temp.name) / 'accounts.db'),
            'BUSINESS_PHONE': '+15550000000', 'ENABLE_SMS_FOLLOWUP': False,
            'ENABLE_DEV_ROUTES': False, 'SECRET_KEY': 'test-only-session-key-' * 3,
            'TWILIO_AUTH_TOKEN': TEST_TOKEN, 'TWILIO_WEBHOOK_BASE_URL': TEST_ORIGIN,
            'TWILIO_ACCOUNT_SID': 'AC' + '0' * 32, 'TWILIO_SMS_FROM': '+15559999999',
            'CALL_EVENT_TOKEN': 'test-only-event-token',
        })
        with self.app.app_context():
            self.a = provision_business('Alpha', 'alpha', 'long-test-password',
                                       '+15550000001', '+15550000002', '+15550000003')
            self.b = provision_business('Beta', 'beta', 'long-test-password',
                                       '+15550000004', '+15550000005', '+15550000006', 'America/New_York')
        self.client = self.app.test_client()
        self.network = patch('sms._open', side_effect=AssertionError('Real network forbidden'))
        self.open = self.network.start()
        self.addCleanup(self.network.stop)
        self.sent = []

    def rows(self, table):
        with self.app.app_context(), closing(get_connection()) as conn:
            return [dict(r) for r in conn.execute('SELECT * FROM ' + table + ' ORDER BY id')]

    def signed(self, path, data, client=None):
        return (client or self.client).post(path, data=data, headers={
            'X-Twilio-Signature': signature(TEST_ORIGIN + path, data),
        }, base_url='http://internal:10000')

    def incoming(self, sid='parent', to='+15550000002', caller='+15551111111'):
        return self.signed('/voice/incoming', {'CallSid': sid, 'To': to, 'From': caller})

    def action(self, sid='parent', status='no-answer', client=None):
        return self.signed('/voice/dial-result', {'CallSid': sid, 'DialCallStatus': status,
                           'From': '+15551111111', 'CallStatus': 'in-progress'}, client)

    def child(self, sid='parent', status='no-answer', client=None):
        # Child From/To can describe the forwarded leg, not the customer/business mapping.
        return self.signed('/voice/dial-result', {'CallSid': 'child-' + sid, 'ParentCallSid': sid,
                           'CallStatus': status, 'From': '+15550000002', 'To': '+15550000003',
                           'Timestamp': 'Thu, 01 Oct 2026 19:22:30 GMT'}, client)

    def inbound(self, n=1, to='+15550000002', body='Can you help?', client=None):
        return self.signed('/sms/incoming', {'MessageSid': 'SM' + f'{n:032x}',
                           'From': '+15551111111', 'To': to, 'Body': body}, client)

    def csrf(self, page):
        return re.search(rb'name="csrf" value="([^"]+)"', page.data).group(1).decode()

    def login(self, login='alpha', client=None):
        client = client or self.client
        token = self.csrf(client.get('/login'))
        response = client.post('/login', data={'csrf': token, 'login': login, 'password': 'long-test-password'})
        self.assertEqual(response.status_code, 302)

    def post_reply(self, conversation_id=1, body='Happy to help', key=None):
        page = self.client.get(f'/inbox/{conversation_id}')
        key = key or re.search(rb'name="request_key" value="([^"]+)"', page.data).group(1).decode()
        return self.client.post(f'/inbox/{conversation_id}', data={
            'csrf': self.csrf(page), 'action': 'reply', 'body': body, 'request_key': key})

    def enable_mock_sends(self):
        self.app.config['ENABLE_SMS_FOLLOWUP'] = True
        def accepted(req):
            self.sent.append(parse_qs(req.data.decode()))
            return io.BytesIO(json.dumps({'sid': 'SM' + f'{1000 + len(self.sent):032x}', 'status': 'queued'}).encode())
        self.open.side_effect = accepted

    def test_answered_call_keeps_forwarding_and_creates_nothing(self):
        xml = ElementTree.fromstring(self.incoming().data)
        self.assertEqual(xml.find('Dial').get('timeout'), '20')
        self.assertEqual(xml.find('Dial/Number').text, '+15550000003')
        self.assertEqual(self.action(status='completed').status_code, 200)
        self.assertEqual(self.child(status='completed').status_code, 200)
        self.assertEqual(self.rows('missed_calls'), [])
        self.assertEqual(self.rows('conversations'), [])
        self.open.assert_not_called()

    def test_missed_statuses_callback_orders_and_retries_use_parent_and_caller(self):
        for i, status in enumerate(('no-answer', 'busy', 'failed', 'canceled')):
            sid = f'parent-{i}'
            self.incoming(sid)
            callbacks = (self.action, self.child) if i % 2 else (self.child, self.action)
            for callback in callbacks * 2:
                self.assertEqual(callback(sid, status).status_code, 200)
        calls = self.rows('missed_calls')
        self.assertEqual(len(calls), 4)
        self.assertEqual(len(self.rows('conversations')), 1)
        self.assertTrue(all(c['phone_number'] == '+15551111111' and c['business_id'] == self.a for c in calls))
        self.assertTrue(all(c['call_event_at'] == '2026-10-01T19:22:30+00:00' for c in calls))
        self.assertTrue(all(c['follow_up_status'] == 'disabled' for c in calls))
        self.assertEqual(len(self.rows('messages')), 4)
        self.open.assert_not_called()

    def test_recovery_customer_reply_business_reply_share_sender_and_history(self):
        self.enable_mock_sends()
        self.incoming()
        self.child()
        self.action()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0]['From'], ['+15550000002'])
        self.assertEqual(self.inbound().status_code, 200)
        self.login()
        self.assertEqual(self.post_reply().status_code, 302)
        self.assertEqual([s['From'] for s in self.sent], [['+15550000002'], ['+15550000002']])
        self.assertEqual([s['To'] for s in self.sent], [['+15551111111'], ['+15551111111']])
        self.assertEqual([m['status'] for m in self.rows('messages')], ['sent','received','sent'])
        page = self.client.get('/inbox/1').data
        self.assertIn(b'Oct 01, 2026 12:22 PM MST (America/Phoenix)', page)
        self.assertIn(b'Can you help?', page)
        self.assertIn(b'Happy to help', page)

    def test_same_customer_is_isolated_across_two_businesses(self):
        self.enable_mock_sends()
        for sid, number in (('a', '+15550000002'), ('b', '+15550000005')):
            self.incoming(sid, number)
            self.action(sid)
        self.inbound(1, body='Alpha private')
        self.inbound(2, to='+15550000005', body='Beta private')
        self.assertEqual(len(self.rows('conversations')), 2)
        self.assertEqual([s['From'] for s in self.sent], [['+15550000002'], ['+15550000005']])
        self.login('beta')
        self.assertNotIn(b'Alpha private', self.client.get('/inbox').data)
        self.assertIn(b'Beta private', self.client.get('/inbox/2').data)
        self.assertEqual(self.client.get('/inbox/1').status_code, 404)
        token = self.csrf(self.client.get('/inbox/2'))
        self.assertEqual(self.client.post('/inbox/1', data={'csrf':token,'action':'close'}).status_code, 404)
        self.post_reply(2)
        self.assertEqual(self.sent[-1]['From'], ['+15550000005'])

    def test_disabled_flag_blocks_recovery_and_manual_reply(self):
        self.incoming()
        self.action()
        self.login()
        self.post_reply()
        self.assertEqual([m['status'] for m in self.rows('messages')], ['disabled','disabled'])
        self.assertIn(b'Text sending is disabled', self.client.get('/inbox/1').data)
        self.open.assert_not_called()

    def test_inbound_retry_deduplicates_without_resetting_read_state(self):
        self.inbound()
        self.login()
        token = self.csrf(self.client.get('/inbox/1'))
        self.client.post('/inbox/1', data={'csrf':token,'action':'read'})
        self.inbound()
        self.assertEqual(len(self.rows('messages')), 1)
        self.assertEqual(self.rows('conversations')[0]['unread'], 0)
        self.assertEqual(self.rows('conversations')[0]['status'], 'replied')

    def test_reply_retry_never_sends_twice(self):
        self.inbound()
        self.enable_mock_sends()
        self.login()
        for _ in range(2):
            self.assertEqual(self.post_reply(key='a'*32).status_code, 302)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(len(self.rows('messages')), 2)

    def test_failed_reply_records_safe_failure_and_is_not_retried(self):
        self.inbound()
        self.app.config['ENABLE_SMS_FOLLOWUP'] = True
        self.open.side_effect = TimeoutError('SECRET MUST NOT APPEAR')
        self.login()
        for _ in range(2):
            self.post_reply(key='a'*32)
        self.assertEqual(self.open.call_count, 1)
        message = self.rows('messages')[-1]
        self.assertEqual(message['status'], 'failed')
        self.assertEqual(message['error'], 'unknown_outcome')
        self.assertNotIn(b'SECRET MUST NOT APPEAR', self.client.get('/inbox/1').data)

    def test_unknown_numbers_and_parent_bindings_fail_closed(self):
        self.assertEqual(self.incoming(to='+15557777777').status_code, 404)
        self.assertEqual(self.action().status_code, 404)
        self.assertEqual(self.inbound(to='+15557777777').status_code, 404)
        self.incoming()
        self.assertEqual(self.incoming(to='+15550000005').status_code, 404)
        self.assertEqual(self.rows('conversations'), [])

    def test_sms_signature_required_and_query_and_body_tampering_rejected(self):
        data = {'MessageSid':'SM'+'1'*32,'From':'+15551111111','To':'+15550000002','Body':'Hello'}
        self.assertEqual(self.client.post('/sms/incoming', data=data).status_code, 403)
        path = '/sms/incoming?source=a%2Bb'
        sig = signature(TEST_ORIGIN + path, data)
        self.assertEqual(self.client.post(path, data={**data,'To':'+15550000005'}, headers={'X-Twilio-Signature':sig}).status_code, 403)
        self.assertEqual(self.client.post(path+'&extra=1', data=data, headers={'X-Twilio-Signature':sig}).status_code, 403)
        self.assertEqual(self.signed(path,data).status_code, 200)
        self.assertEqual(len(self.rows('messages')), 1)

    def test_login_csrf_logout_and_public_dashboard_protection(self):
        self.inbound()
        self.assertEqual(self.client.get('/').status_code, 302)
        self.assertEqual(self.client.get('/inbox').status_code, 302)
        self.assertEqual(self.client.post('/login', data={'login':'alpha','password':'long-test-password'}).status_code, 403)
        self.login()
        self.assertEqual(self.client.post('/inbox/1', data={'action':'reply','body':'unsafe'}).status_code, 403)
        token = self.csrf(self.client.get('/inbox'))
        response = self.client.post('/logout', data={'csrf':token})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.get('/inbox/1').status_code, 302)

    def test_stop_blocks_automated_and_manual_texts_until_start(self):
        self.inbound(body='STOP')
        self.enable_mock_sends()
        self.incoming()
        self.action()
        self.login()
        self.post_reply(key='a'*32)
        self.open.assert_not_called()
        self.assertEqual(self.rows('missed_calls')[0]['follow_up_error'], 'recipient_opted_out')
        self.inbound(2,body='START')
        self.post_reply(key='b'*32)
        self.assertEqual(len(self.sent), 1)

    def test_number_assignment_rejects_duplicates_and_direct_loops(self):
        with self.app.app_context():
            for numbers in (('+15550000007','+15550000002','+15550000009'),
                            ('+15550000007','+15550000008','+15550000007')):
                with self.assertRaises(ValueError):
                    provision_business('Other','other','long-test-password',*numbers)

    def test_simultaneous_callbacks_and_inbound_retries(self):
        self.incoming()
        def deliver(i):
            client = self.app.test_client()
            if i % 2:
                return self.child(client=client).status_code
            return self.action(client=client).status_code
        with ThreadPoolExecutor(max_workers=4) as pool:
            self.assertEqual(list(pool.map(deliver, range(4))), [200]*4)
        self.assertEqual(len(self.rows('missed_calls')), 1)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.inbound(client=self.app.test_client()).status_code, range(4)))
        self.assertEqual(results, [200]*4)
        self.assertEqual(len(self.rows('messages')), 2)

    def test_migration_restart_and_receipt_fallback(self):
        self.incoming()
        self.action()
        with self.app.app_context():
            init_db()
            init_db()
        self.assertEqual(len(self.rows('missed_calls')), 1)
        self.assertIsNone(self.rows('missed_calls')[0]['call_event_at'])
        self.login()
        self.assertIn(b'America/Phoenix', self.client.get('/inbox/1').data)
        self.assertEqual(len(self.rows('messages')), 1)

    def test_account_lockout_after_five_failed_attempts(self):
        token = self.csrf(self.client.get('/login'))
        for _ in range(5):
            self.client.post('/login', data={'csrf':token,'login':'alpha','password':'wrong'})
        response = self.client.post('/login', data={'csrf':token,'login':'alpha','password':'long-test-password'})
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Sign-in failed', response.data)

    def test_missing_session_key_fails_closed_and_message_html_is_escaped(self):
        self.inbound(body='<script>alert(1)</script>')
        self.login()
        page = self.client.get('/inbox/1')
        self.assertNotIn(b'<script>', page.data)
        self.assertIn(b'&lt;script&gt;', page.data)
        self.assertEqual(page.headers['Cache-Control'], 'no-store')
        self.app.secret_key = None
        self.assertEqual(self.client.get('/inbox').status_code, 503)

    def test_generic_event_cannot_create_unowned_leads_after_onboarding(self):
        response = self.client.post('/call-event', json={'phone_number':'123','call_status':'no-answer'},
                                    headers={'Authorization':'Bearer test-only-event-token'})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.rows('missed_calls'), [])

    def test_concurrent_callbacks_make_one_recovery_send(self):
        self.enable_mock_sends()
        self.incoming()
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.action(client=self.app.test_client()).status_code, range(4)))
        self.assertEqual(results, [200]*4)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(len(self.rows('messages')), 1)
        self.assertEqual(self.rows('messages')[0]['status'], 'sent')

    def test_concurrent_reply_claims_send_once_and_restart_does_not_replay(self):
        self.inbound()
        self.enable_mock_sends()
        def deliver(_):
            with self.app.app_context():
                return reply(self.a, 1, 'One reply', 'b'*32)
        with ThreadPoolExecutor(max_workers=4) as pool:
            self.assertEqual(len(set(pool.map(deliver, range(4)))), 1)
        with self.app.app_context():
            init_db()
            reply(self.a, 1, 'One reply', 'b'*32)
        self.assertEqual(len(self.sent), 1)

    def test_provider_failure_updates_call_and_recovery_message_consistently(self):
        self.app.config['ENABLE_SMS_FOLLOWUP'] = True
        self.open.side_effect = TimeoutError('do not leak provider detail')
        self.incoming()
        self.action()
        self.child()
        self.assertEqual(self.open.call_count, 1)
        self.assertEqual(self.rows('missed_calls')[0]['follow_up_status'], 'failed')
        self.assertEqual(self.rows('messages')[0]['status'], 'failed')
        self.assertEqual(self.rows('messages')[0]['error'], 'unknown_outcome')

    def test_cli_provisions_without_sending_and_hashes_password(self):
        runner = self.app.test_cli_runner()
        result = runner.invoke(args=['create-business','--name','Gamma','--login','gamma',
            '--public-phone','+15550000007','--mca-phone','+15550000008',
            '--forwarding-phone','+15550000009'], input='another-long-password\nanother-long-password\n')
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(len(self.rows('businesses')), 3)
        self.assertNotIn('another-long-password', self.rows('businesses')[-1]['password_hash'])
        self.assertNotIn('another-long-password', result.output)
        self.open.assert_not_called()

    def test_unassigned_historical_rows_stay_hidden_after_provisioning(self):
        with self.app.app_context(), closing(get_connection()) as conn, conn:
            conn.execute('''INSERT INTO missed_calls
                (phone_number,caller_name,time_received,status,follow_up_status,follow_up_message)
                VALUES ('+15553333333','Private legacy','Oct 01, 2026 12:43 AM','missed','disabled','old')''')
        self.login()
        self.assertNotIn(b'Private legacy', self.client.get('/', follow_redirects=True).data)
        self.assertNotIn(b'+15553333333', self.client.get('/inbox').data)
        self.assertEqual(len(self.rows('missed_calls')), 1)

    def test_rejected_media_and_bad_input_have_no_side_effects(self):
        data = {'MessageSid':'SM'+'3'*32,'From':'+15551111111','To':'+15550000002','Body':'Photo','NumMedia':'1'}
        self.assertEqual(self.signed('/sms/incoming',data).status_code, 400)
        self.assertEqual(self.inbound(body='').status_code, 400)
        self.assertEqual(self.rows('messages'), [])
