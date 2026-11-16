from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import unittest
from unittest.mock import patch

import test_business_inbox as fixtures
from database import get_connection, init_db
from lead_capture import extract, load_details
from messaging import dispatch_question


class ProgressiveCaptureTests(unittest.TestCase):
    setUp = fixtures.BusinessInboxTests.setUp
    rows = fixtures.BusinessInboxTests.rows
    signed = fixtures.BusinessInboxTests.signed
    incoming = fixtures.BusinessInboxTests.incoming
    action = fixtures.BusinessInboxTests.action
    child = fixtures.BusinessInboxTests.child
    inbound = fixtures.BusinessInboxTests.inbound
    csrf = fixtures.BusinessInboxTests.csrf
    login = fixtures.BusinessInboxTests.login
    post_reply = fixtures.BusinessInboxTests.post_reply
    enable_mock_sends = fixtures.BusinessInboxTests.enable_mock_sends

    def details(self, index=0):
        return load_details(self.rows('conversations')[index])

    def enable_capture(self):
        self.enable_mock_sends()
        self.app.config['ENABLE_PROGRESSIVE_CAPTURE'] = True

    def test_acceptance_missed_call_to_complete_service_card(self):
        self.enable_capture()
        self.incoming()
        self.child()
        self.action()
        self.assertEqual(len(self.rows('conversations')), 1)
        self.assertEqual(self.details()['fields']['service']['state'], 'unknown')
        self.assertEqual(self.details()['fields']['callback']['state'], 'candidate')
        self.assertEqual(len(self.sent), 1)
        self.inbound(1, body='My kitchen sink is clogged')
        fields = self.details()['fields']
        self.assertEqual(fields['service']['value'], 'My kitchen sink is clogged')
        self.assertEqual(fields['service']['state'], 'customer_reported')
        self.assertIn('Where', self.sent[-1]['Body'][0])
        self.inbound(2, body='Phoenix')
        self.assertEqual(self.details()['fields']['location']['value'], 'Phoenix')
        self.assertIn('How soon', self.sent[-1]['Body'][0])
        self.inbound(3, body='Today')
        self.assertIn('best number', self.sent[-1]['Body'][0])
        self.inbound(4, body='Yes')
        self.assertEqual(self.details()['fields']['callback']['state'], 'confirmed')
        self.assertEqual(len(self.sent), 4)  # Recovery + three clarifications, no gratuitous acknowledgement.
        self.assertTrue(all(s['From'] == ['+15550000002'] for s in self.sent))
        self.assertEqual(self.rows('conversations')[0]['lead_source'], 'call+sms')
        self.assertEqual(self.rows('conversations')[0]['unread'], 1)
        self.login()
        page = self.client.get('/inbox/1').data
        for value in (b'My kitchen sink is clogged', b'Phoenix', b'Today', b'confirmed by customer',
                      b'Complete', b'No appointment or quote has been promised'):
            self.assertIn(value, page)

    def test_partial_lead_survives_abandonment_restart_and_repeated_call(self):
        self.enable_capture()
        self.incoming()
        self.action(status='canceled')
        self.inbound(body='My kitchen sink is clogged')
        before = self.details()
        with self.app.app_context():
            init_db()
            init_db()
        self.assertEqual(self.details(), before)
        self.incoming('another-call')
        self.action('another-call')
        self.assertEqual(len(self.rows('conversations')), 1)
        self.assertEqual(self.details(), before)
        self.login()
        page = self.client.get('/inbox/1').data
        self.assertIn(b'Partial', page)
        self.assertIn(b'partial lead is saved', page)

    def test_sms_only_lead_skips_details_already_provided(self):
        self.enable_capture()
        self.inbound(body='My name is Alex; My kitchen sink is clogged; location: Phoenix; urgency: today; call me at +15552222222')
        fields = self.details()['fields']
        self.assertEqual(fields['customer_name']['value'], 'Alex')
        self.assertEqual(fields['location']['value'], 'Phoenix')
        self.assertEqual(fields['urgency']['value'], 'today')
        self.assertEqual(fields['callback']['value'], '+15552222222')
        self.assertEqual(fields['callback']['state'], 'customer_reported')
        self.assertEqual(self.rows('conversations')[0]['lead_source'], 'sms')
        self.assertEqual(self.sent, [])
        self.assertEqual(len(self.rows('conversations')), 1)

    def test_disabled_flags_still_capture_facts_without_sending(self):
        self.app.config['ENABLE_PROGRESSIVE_CAPTURE'] = True
        self.inbound(body='My kitchen sink is clogged')
        self.assertEqual(self.details()['fields']['service']['value'], 'My kitchen sink is clogged')
        self.assertEqual(len(self.rows('messages')), 1)
        self.assertIsNone(self.details()['pending'])
        self.open.assert_not_called()
        self.app.config['ENABLE_SMS_FOLLOWUP'] = True
        self.app.config['ENABLE_PROGRESSIVE_CAPTURE'] = False
        self.inbound(2, body='location: Phoenix')
        self.open.assert_not_called()
        self.assertEqual(self.details()['fields']['location']['value'], 'Phoenix')

    def test_duplicate_inbound_does_not_advance_or_repeat_question(self):
        self.enable_capture()
        self.inbound(body='My kitchen sink is clogged')
        before = self.details()
        self.inbound(body='My kitchen sink is clogged')
        self.assertEqual(self.details(), before)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(len(self.rows('messages')), 2)

    def test_concurrent_duplicate_webhooks_capture_and_send_once(self):
        self.enable_capture()
        with ThreadPoolExecutor(max_workers=4) as pool:
            statuses = list(pool.map(lambda _: self.inbound(body='My kitchen sink is clogged', client=self.app.test_client()).status_code, range(4)))
        self.assertEqual(statuses, [200]*4)
        self.assertEqual(len(self.rows('conversations')), 1)
        self.assertEqual(len(self.rows('messages')), 2)
        self.assertEqual(len(self.sent), 1)

    def test_stop_keeps_partial_lead_and_blocks_questions_and_replies(self):
        self.enable_capture()
        self.inbound(body='My kitchen sink is clogged')
        self.inbound(2, body='STOP')
        self.inbound(3, body='location: Phoenix')
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.details()['fields']['service']['value'], 'My kitchen sink is clogged')
        self.assertIsNone(self.details()['fields']['location']['value'])
        self.assertEqual(self.rows('conversations')[0]['opted_out'], 1)
        self.login()
        self.post_reply()
        self.assertEqual(len(self.sent), 1)
        self.assertIn(b'customer opted out; do not text', self.client.get('/inbox/1').data)

    def test_stop_between_queue_and_send_cancels_queued_question(self):
        self.enable_capture()
        with patch('messaging.dispatch_question'):
            self.inbound(body='My kitchen sink is clogged')
        question_id = self.details()['pending']['message_id']
        self.inbound(2, body='STOP')
        with self.app.app_context():
            dispatch_question(question_id)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.rows('messages')[1]['status'], 'not_attempted')

    def test_send_failure_keeps_details_and_hands_off_without_retry(self):
        self.enable_capture()
        self.open.side_effect = TimeoutError('sensitive provider details')
        self.inbound(body='My kitchen sink is clogged')
        self.assertEqual(self.rows('messages')[1]['status'], 'failed')
        self.assertEqual(self.details()['fields']['service']['value'], 'My kitchen sink is clogged')
        self.inbound(body='My kitchen sink is clogged')
        self.assertEqual(self.open.call_count, 1)
        self.inbound(2, body='location: Phoenix')
        self.assertEqual(self.open.call_count, 1)
        self.assertTrue(self.details()['paused'])
        self.login()
        page = self.client.get('/inbox/1').data
        self.assertNotIn(b'sensitive provider details', page)
        self.assertIn(b'Owner review needed', page)

    def test_unclear_answer_does_not_loop_or_become_a_fact(self):
        self.enable_capture()
        self.inbound(body='My kitchen sink is clogged')
        self.inbound(2, body="I don't know")
        self.inbound(3, body='Maybe')
        self.assertIsNone(self.details()['fields']['location']['value'])
        self.assertTrue(self.details()['paused'])
        self.assertEqual(len(self.sent), 1)

    def test_owner_reply_stops_automatic_questions_but_not_passive_capture(self):
        self.enable_capture()
        self.inbound(body='My kitchen sink is clogged')
        self.login()
        self.post_reply()
        self.inbound(2, body='location: Phoenix; urgency: today')
        self.assertEqual(len(self.sent), 2)
        self.assertTrue(self.details()['paused'])
        self.assertEqual(self.details()['fields']['location']['value'], 'Phoenix')

    def test_second_business_cannot_read_captured_lead_and_uses_own_sender(self):
        self.enable_capture()
        self.inbound(body='My kitchen sink is clogged')
        self.inbound(2, to='+15550000005', body='My roof is leaking')
        self.assertEqual([s['From'] for s in self.sent], [['+15550000002'], ['+15550000005']])
        self.login('beta')
        page = self.client.get('/inbox').data
        self.assertIn(b'roof is leaking', page)
        self.assertNotIn(b'kitchen sink', page)
        self.assertEqual(self.client.get('/inbox/1').status_code, 404)

    def test_missing_service_asks_one_question_and_preserves_greeting(self):
        self.enable_capture()
        self.inbound(body='Hello')
        self.assertEqual(self.sent[0]['Body'], ['What do you need help with?'])
        self.assertIsNone(self.details()['fields']['service']['value'])
        self.inbound(2, body='A tax return')
        self.assertEqual(self.details()['fields']['service']['value'], 'A tax return')
        self.assertIn('How soon', self.sent[-1]['Body'][0])  # No irrelevant service location question.

    def test_declined_location_is_not_reasked_and_name_is_optional(self):
        self.enable_capture()
        self.inbound(body='My kitchen sink is clogged')
        self.inbound(2, body='Prefer not to share')
        self.assertEqual(self.details()['fields']['location']['state'], 'declined')
        self.assertIn('How soon', self.sent[-1]['Body'][0])
        self.assertIsNone(self.details()['fields']['customer_name']['value'])

    def test_no_urgency_inference_or_negation_reversal(self):
        self.assertEqual(extract('My sink is clogged. Not urgent.', None)['urgency'], 'Not urgent')
        self.assertNotIn('urgency', extract('My kitchen sink is clogged', None))

    def test_additive_migration_retains_existing_conversation_without_history_replay(self):
        self.inbound(body='Hello')
        with self.app.app_context(), closing(get_connection()) as conn, conn:
            conn.execute('ALTER TABLE conversations DROP COLUMN lead_details')
            conn.execute('ALTER TABLE conversations DROP COLUMN lead_source')
        with self.app.app_context():
            init_db()
        self.assertEqual(len(self.rows('conversations')), 1)
        self.assertEqual(len(self.rows('messages')), 1)
        self.assertEqual(self.rows('conversations')[0]['lead_source'], 'unknown')
        self.assertIsNone(self.details()['fields']['service']['value'])
        self.open.assert_not_called()

    def test_answered_call_does_not_create_false_missed_lead_or_question(self):
        self.enable_capture()
        self.incoming()
        self.action(status='completed')
        self.child(status='completed')
        self.assertEqual(self.rows('conversations'), [])
        self.assertEqual(self.sent, [])

    def test_natural_language_opt_out_never_becomes_service_answer(self):
        self.enable_capture()
        self.inbound(body='Please stop texting me')
        self.assertEqual(self.sent, [])
        self.assertEqual(self.rows('conversations')[0]['opted_out'], 1)
        self.assertIsNone(self.details()['fields']['service']['value'])

    def test_new_facts_before_dispatch_cancel_obsolete_question(self):
        self.enable_capture()
        with patch('messaging.dispatch_question'):
            self.inbound(body='My kitchen sink is clogged')
        question_id = self.details()['pending']['message_id']
        self.inbound(2, body='location: Phoenix')
        with self.app.app_context():
            dispatch_question(question_id)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.rows('messages')[1]['status'], 'not_attempted')
        self.assertEqual(self.details()['fields']['location']['value'], 'Phoenix')

    def test_confirmation_does_not_change_sms_destination(self):
        self.enable_capture()
        self.inbound(body='My kitchen sink is clogged; location: Phoenix; urgency: today')
        self.inbound(2, body='+15552222222')
        self.assertEqual(self.details()['fields']['callback']['value'], '+15552222222')
        self.assertTrue(all(s['To'] == ['+15551111111'] for s in self.sent))

    def test_unsigned_inbound_cannot_change_structured_lead(self):
        self.enable_capture()
        self.inbound(body='My kitchen sink is clogged')
        before = self.details()
        response = self.client.post('/sms/incoming', data={
            'MessageSid':'SM'+'f'*32,'From':'+15551111111','To':'+15550000002','Body':'location: forged'})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.details(), before)

    def test_explicit_unknown_location_is_not_saved_as_confirmed_information(self):
        self.enable_capture()
        self.inbound(body='My kitchen sink is clogged')
        self.inbound(2, body="location: I don't know")
        self.assertIsNone(self.details()['fields']['location']['value'])
        self.assertEqual(self.details()['fields']['location']['state'], 'unknown')
        self.assertEqual(len(self.sent), 1)

    def test_twilio_start_metadata_does_not_hide_requested_yes_confirmation(self):
        self.enable_capture()
        self.inbound(body='My kitchen sink is clogged; location: Phoenix; urgency: today')
        self.assertEqual(self.details()['pending']['field'], 'callback')
        self.signed('/sms/incoming', {'MessageSid':'SM'+'e'*32,
            'From':'+15551111111','To':'+15550000002','Body':'YES','OptOutType':'START'})
        self.assertEqual(self.details()['fields']['callback']['state'], 'confirmed')
        self.assertEqual(len(self.sent), 1)
