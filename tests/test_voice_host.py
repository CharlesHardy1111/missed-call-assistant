import asyncio
from dataclasses import replace
import json
import unittest
from unittest.mock import Mock

from twilio.request_validator import RequestValidator

import test_voice_relay as fixtures
from memory_async import run
from voice_host import RelayHost
from voice_relay import RelaySession, authenticate_handshake


class VoiceHostTests(unittest.TestCase):
    def setUp(self):
        fixtures.VoiceRelayTests.setUp(self)
        self.url = 'wss://stage.example.test/relay?x=a%2Bb'
        self.token = 'test-only-websocket-token'
        self.factory = Mock(side_effect=self.new_session)
        self.host = RelayHost(self.url, self.token, self.factory,
                              context_factory=self.app.app_context, tick_seconds=0.001, io_timeout=0.02)

    def new_session(self):
        return RelaySession(self.engine, self.i, self.binding, clock=lambda: self.now)

    def setup_frame(self):
        return fixtures.VoiceRelayTests.setup_frame(self)

    def scope(self):
        signature = RequestValidator(self.token).compute_signature(self.url, {})
        return dict(type='websocket', raw_path=b'/relay', query_string=b'x=a%2Bb',
                    headers=[(b'x-twilio-signature', signature.encode()),
                             (b'host', b'internal:10000'), (b'x-forwarded-proto', b'http')])

    def run_host(self, events, scope=None, fail_send=False, stall_send=False, after_queue=None):
        sent = []
        async def exercise():
            queue = asyncio.Queue()
            for event in events:
                queue.put_nowait(event)
            async def receive():
                if queue.empty() and after_queue:
                    after_queue()
                return await queue.get()
            async def send(message):
                if message['type'] == 'websocket.send' and fail_send:
                    raise OSError('private transport exception')
                if message['type'] == 'websocket.send' and stall_send:
                    await asyncio.sleep(60)
                sent.append(message)
            await asyncio.wait_for(self.host(scope or self.scope(), receive, send), 2)
        run(exercise())
        return sent

    def events(self, *prompts):
        return [{'type': 'websocket.connect'}, {'type': 'websocket.receive', 'text': self.setup_frame()}] + [
            {'type': 'websocket.receive', 'text': json.dumps(dict(type='prompt', voicePrompt=p, last=True))}
            for p in prompts] + [{'type': 'websocket.disconnect'}]

    def test_signed_flow_preserves_completed_outcome_on_disconnect(self):
        sent = self.run_host(self.events('My sink is clogged', 'Phoenix', 'today', 'yes'))
        self.assertEqual(sent[0]['type'], 'websocket.accept')
        self.assertEqual(len([x for x in sent if x['type'] == 'websocket.send']), 4)
        self.assertEqual(self.engine.outcome(self.i)['state']['status'], 'complete')
        self.assertEqual(self.host.active, 0)

    def test_unsigned_missing_raw_target_duplicate_signature_and_changed_query_reject_before_factory(self):
        for change in ['unsigned', 'raw', 'duplicate', 'query']:
            scope = self.scope()
            if change == 'unsigned':
                scope['headers'] = []
            elif change == 'raw':
                scope.pop('raw_path')
            elif change == 'duplicate':
                scope['headers'].append(scope['headers'][0])
            else:
                scope['query_string'] = b'x=a+b'
            sent = self.run_host([{'type': 'websocket.connect'}], scope)
            self.assertEqual(sent, [{'type': 'websocket.close', 'code': 1008}])
        self.factory.assert_not_called()

    def test_silence_runs_tick_without_incoming_frames(self):
        def advance():
            self.now += 31
        sent = self.run_host(self.events()[:-1], after_queue=advance)
        self.assertIn('silence_timeout', str(sent))
        self.assertEqual(self.engine.outcome(self.i)['state']['status'], 'needs_human')

    def test_connection_loss_preserves_partial_fields(self):
        self.run_host(self.events('My sink is clogged'))
        self.assertIn('sink', self.engine.outcome(self.i)['summary'])
        self.assertEqual(self.engine.outcome(self.i)['state']['status'], 'needs_human')

    def test_broken_write_and_blocked_write_do_not_leak_or_hang(self):
        sent = self.run_host(self.events('My sink is clogged'), fail_send=True)
        self.assertNotIn('private transport exception', str(sent))
        self.assertEqual(self.host.active, 0)
        self.assertEqual(self.engine.outcome(self.i)['state']['status'], 'needs_human')

    def test_blocked_write_times_out(self):
        sent = self.run_host(self.events('My sink is clogged'), stall_send=True)
        self.assertEqual(sent[-1], {'type': 'websocket.close', 'code': 1011})
        self.assertEqual(self.host.active, 0)

    def test_concurrency_limit_rejects_before_factory(self):
        self.host.active = self.host.max_connections
        sent = self.run_host([{'type': 'websocket.connect'}])
        self.assertEqual(sent, [{'type': 'websocket.close', 'code': 1013}])
        self.factory.assert_not_called()

    def test_frame_budget_ends_without_processing_excess_input(self):
        self.host.max_frames = 1
        sent = self.run_host(self.events('My sink is clogged'))
        self.assertIn('frame_limit_or_invalid_event', str(sent))
        self.assertIsNone(self.engine.outcome(self.i)['fields']['service']['value'])

    def test_storage_failure_before_accept_rejects_and_releases_slot(self):
        self.factory.side_effect = RuntimeError('private database path')
        sent = self.run_host([{'type': 'websocket.connect'}])
        self.assertEqual(sent, [{'type': 'websocket.close', 'code': 1011}])
        self.assertEqual(self.host.active, 0)

    def test_binary_and_oversized_frames_terminate(self):
        events = self.events()[:-1] + [{'type': 'websocket.receive', 'bytes': b'audio'}]
        sent = self.run_host(events)
        self.assertIn('invalid_frame', str(sent))

    def test_no_http_surface(self):
        sent = self.run_host([], {'type': 'http'})
        self.assertEqual(sent[0]['status'], 404)

    def test_binding_must_match_lead_and_business_number(self):
        for binding in [replace(self.binding, caller='+15552222222'),
                        replace(self.binding, destination='+15559999999')]:
            with self.assertRaises(ValueError):
                RelaySession(self.engine, self.i, binding)

    def test_invalid_url_credentials_port_and_unicode_frame(self):
        for url in ['wss://@example.test/relay', 'wss://example.test:bad/relay']:
            self.assertFalse(authenticate_handshake(url, '/relay', 'a' * 27 + '=', self.token))
        self.assertEqual(self.relay.receive('\ud800')[0]['type'], 'end')

    def test_cancellation_cleans_up_and_propagates(self):
        async def exercise():
            queue = asyncio.Queue()
            for event in self.events('My sink is clogged')[:-1]:
                queue.put_nowait(event)
            wrote = asyncio.Event()
            async def send(message):
                if message['type'] == 'websocket.send':
                    wrote.set()
            task = asyncio.create_task(self.host(self.scope(), queue.get, send))
            await asyncio.wait_for(wrote.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        run(exercise())
        self.assertEqual(self.host.active, 0)
        self.assertIn('sink', self.engine.outcome(self.i)['summary'])
