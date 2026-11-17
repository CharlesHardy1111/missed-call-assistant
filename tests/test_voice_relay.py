import json
from contextlib import closing
import unittest

from twilio.request_validator import RequestValidator

import test_agent_engine as fixtures
from database import get_connection
from voice_relay import CallBinding, RelaySession, authenticate_handshake, init_relay_store


class VoiceRelayTests(unittest.TestCase):
    def setUp(self):
        fixtures.AgentFoundationTests.setUp(self)
        init_relay_store()
        self.now = 100.0
        self.binding = CallBinding('CA' + '1' * 32, 'AC' + '2' * 32,
                                   '+15551111111', '+15550000002', 120.0)
        self.relay = self.new_session()

    def new_session(self):
        return RelaySession(self.engine, self.i, self.binding, clock=lambda: self.now)

    def frame(self, **data):
        return self.relay.receive(json.dumps(data))

    def setup_frame(self, **overrides):
        data = dict(type='setup', sessionId='VX' + '3' * 32,
                    callSid=self.binding.call_sid, accountSid=self.binding.account_sid,
                    **{'from': self.binding.caller, 'to': self.binding.destination})
        data.update(overrides)
        return json.dumps(data)

    def start(self):
        self.assertEqual(self.relay.receive(self.setup_frame()), [])
        self.assertFalse(self.relay.closed)

    def reason(self, frames):
        return json.loads(frames[0]['handoffData'])['reason']

    def test_valid_signature_exact_public_url_including_query(self):
        for scheme in ['https', 'wss']:
            url = scheme + '://stage.example.test/relay?x=a%2Bb&n=2'
            token = 'test-only-token'
            signature = RequestValidator(token).compute_signature(url, {})
            self.assertTrue(authenticate_handshake(url, '/relay?x=a%2Bb&n=2', signature, token))
            self.assertFalse(authenticate_handshake(url, '/relay?x=a+b&n=2', signature, token))
            self.assertFalse(authenticate_handshake(url, '/relay?n=2&x=a%2Bb', signature, token))
            self.assertFalse(authenticate_handshake(url.replace('stage.', 'other.'), '/relay?x=a%2Bb&n=2', signature, token))

    def test_missing_bad_signature_and_configuration_fail_closed(self):
        for signature in ['', 'bad', None]:
            self.assertFalse(authenticate_handshake('wss://stage.example.test/relay', '/relay', signature, 'test'))
        for url in ['', 'http://stage.example.test/relay', 'wss://user:password@stage.example.test/relay']:
            self.assertFalse(authenticate_handshake(url, '/relay', 'a' * 27 + '=', 'test'))
        self.assertFalse(authenticate_handshake('wss://stage.example.test/relay', '/relay', 'a' * 27 + '=', ''))

    def test_setup_binds_account_call_and_numbers(self):
        for key in ['accountSid', 'callSid', 'from', 'to', 'sessionId']:
            relay = self.new_session()
            self.assertEqual(self.reason(relay.receive(self.setup_frame(**{key: 'wrong'}))), 'binding_rejected')

    def test_expired_and_reconnect_bindings_rejected(self):
        self.now = 121
        self.assertEqual(self.reason(self.relay.receive(self.setup_frame())), 'binding_rejected')
        self.relay = self.new_session()
        self.now = 100
        self.assertEqual(self.reason(self.relay.receive(self.setup_frame(connectionType='reconnect'))), 'binding_rejected')

    def test_setup_claim_survives_new_session_object(self):
        self.start()
        other = self.new_session()
        self.assertEqual(self.reason(other.receive(self.setup_frame())), 'replay_rejected')
        self.assertEqual(self.engine.outcome(self.i)['state']['status'], 'active')

    def test_prompt_requires_setup(self):
        result = self.frame(type='prompt', voicePrompt='hello', last=True)
        self.assertEqual(self.reason(result), 'setup_required')

    def test_four_turn_conversation_reaches_existing_lead(self):
        self.start()
        for text in ['My kitchen sink is clogged', 'Phoenix', 'today', 'yes']:
            result = self.frame(type='prompt', voicePrompt=text, last=True)
            self.assertEqual(result[0]['type'], 'text')
            self.assertTrue(result[0]['interruptible'])
        outcome = self.engine.outcome(self.i)
        self.assertEqual(outcome['state']['status'], 'complete')
        self.assertEqual(outcome['fields']['callback']['state'], 'confirmed')
        self.assertEqual(len(self.relay.metrics), 4)
        with closing(get_connection()) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM messages').fetchone()[0], 0)

    def test_partial_transcripts_never_populate_fields(self):
        self.start()
        self.assertEqual(self.frame(type='prompt', voicePrompt='My sink is', last=False), [])
        self.assertIsNone(self.engine.outcome(self.i)['fields']['service']['value'])

    def test_interrupt_stops_output_and_prevents_false_confirmation(self):
        self.start()
        self.frame(type='prompt', voicePrompt='My sink is clogged', last=True)
        result = self.frame(type='interrupt', utteranceUntilInterrupt='Where', durationUntilInterruptMs=100)
        self.assertEqual(self.reason(result), 'speech_interrupted')
        self.assertEqual(self.frame(type='prompt', voicePrompt='yes', last=True), [])
        outcome = self.engine.outcome(self.i)
        self.assertEqual(outcome['state']['status'], 'needs_human')
        self.assertEqual(outcome['fields']['callback']['state'], 'candidate')

    def test_disconnect_and_provider_error_keep_partial_lead(self):
        self.start()
        self.frame(type='prompt', voicePrompt='My sink is clogged', last=True)
        result = self.frame(type='error', description='secret-provider-value')
        self.assertNotIn('secret-provider-value', str(result))
        self.relay.disconnect()  # harmless after already closed
        self.assertIn('sink', self.engine.outcome(self.i)['summary'])

    def test_silence_and_session_duration_are_bounded(self):
        self.start()
        self.now += 30
        self.assertEqual(self.reason(self.relay.tick()), 'silence_timeout')
        self.relay = RelaySession(self.engine, self.i, self.binding, clock=lambda: self.now,
                                  max_seconds=10, idle_seconds=20)
        self.now += 10
        self.assertEqual(self.reason(self.relay.tick()), 'duration_limit')

    def test_stop_persists_optout_without_send(self):
        self.start()
        self.assertEqual(self.reason(self.frame(type='prompt', voicePrompt='STOP', last=True)), 'consent_withdrawn')
        with closing(get_connection()) as db:
            self.assertEqual(db.execute('SELECT opted_out FROM conversations').fetchone()[0], 1)

    def test_human_request_does_not_claim_transfer(self):
        self.start()
        result = self.frame(type='prompt', voicePrompt='I want a human', last=True)
        self.assertEqual(self.reason(result), 'owner_review_required')
        self.assertNotIn('transferred', str(result))

    def test_malformed_oversize_and_binary_frames_end_safely(self):
        for frame in ['[', '[]', 'x' * 8193, b'binary', json.dumps({'type': 'setup', 'sessionId': []})]:
            relay = self.new_session()
            self.assertEqual(relay.receive(frame)[0]['type'], 'end')

    def test_duplicate_setup_does_not_restart_capture(self):
        self.start()
        self.assertEqual(self.reason(self.relay.receive(self.setup_frame())), 'duplicate_setup')

    def test_no_raw_ssml_from_business_information(self):
        # Mutate only disposable fixture snapshot to exercise speech escaping.
        with closing(get_connection()) as db, db:
            row = db.execute('SELECT config FROM agent_interactions WHERE id=?', (self.i,)).fetchone()
            config = json.loads(row[0])
            config['knowledge'] = {'Hours': '<audio src="https://bad.example" />'}
            db.execute('UPDATE agent_interactions SET config=? WHERE id=?', (json.dumps(config), self.i))
        self.start()
        result = self.frame(type='prompt', voicePrompt='hours', last=True)
        self.assertNotIn('<audio', result[0]['token'])
        self.assertIn('&lt;audio', result[0]['token'])
