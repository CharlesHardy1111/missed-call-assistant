from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from test_app import flask_create_app
from businesses import provision_business
from database import get_connection
from agent_engine import (AgentEngine, Decision, OfflineInterpreter, TOOLS,
                          configure_business, init_agent_store)


class AgentFoundationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.app = flask_create_app({'TESTING': True, 'DATABASE_PATH': str(Path(temp.name) / 'agent.db'),
                                    'ENABLE_SMS_FOLLOWUP': False, 'ENABLE_PROGRESSIVE_CAPTURE': False})
        ctx = self.app.app_context()
        ctx.push()
        self.addCleanup(ctx.pop)
        self.a = provision_business('Service', 'a', 'test-only-password', '+15550000001', '+15550000002', '+15550000003')
        self.b = provision_business('Office', 'b', 'test-only-password', '+15550000004', '+15550000005', '+15550000006')
        init_agent_store()
        configure_business(self.a, questions=('service', 'location', 'urgency', 'callback'),
                           knowledge={'Hours': 'Monday to Friday, 9 to 5'}, permissions=TOOLS)
        configure_business(self.b, questions=('service', 'callback'), knowledge={'Hours': 'By appointment'})
        self.engine = AgentEngine(self.a, OfflineInterpreter())
        self.i = self.engine.start('demo-1', '+15551111111')

    def count(self, table):
        with closing(get_connection()) as db:
            return db.execute('SELECT count(*) FROM ' + table).fetchone()[0]

    def test_multi_turn_capture_reuses_lead_and_notifies_inbox(self):
        for n, text in enumerate(['My kitchen sink is clogged', 'Phoenix', 'today', 'yes']):
            result = self.engine.handle(self.i, str(n), text)
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['fields']['callback']['state'], 'confirmed')
        self.assertEqual(result['fields']['service']['value'], 'My kitchen sink is clogged')
        self.assertEqual(result['fields']['location']['value'], 'Phoenix')
        self.assertIn('No appointment', result['reply'])
        with closing(get_connection()) as db:
            self.assertEqual(db.execute('SELECT unread FROM conversations').fetchone()[0], 1)
        self.assertEqual(self.count('messages'), 0)  # no outbound side effects
        self.assertEqual(self.count('missed_calls'), 0)  # no fabricated real calls

    def test_office_uses_same_engine_different_flow_and_knowledge(self):
        engine = AgentEngine(self.b, OfflineInterpreter())
        interaction = engine.start('office', '+15551111111')
        info = engine.handle(interaction, 'info', 'What are your hours?')
        self.assertIn('By appointment', info['reply'])
        result = engine.handle(interaction, 'request', 'I need an account review')
        self.assertIn('best number', result['reply'])
        self.assertNotIn('Monday', info['reply'])
        self.assertEqual(self.count('conversations'), 2)

    def test_retry_event_and_action_return_original_result(self):
        first = self.engine.handle(self.i, '1', 'My kitchen sink is clogged')
        self.assertEqual(first, self.engine.handle(self.i, '1', 'My kitchen sink is clogged'))
        self.assertEqual(self.count('agent_events'), 1)
        result = self.engine.execute(self.i, 'record', 'create_customer_record')
        self.assertEqual(result, self.engine.execute(self.i, 'record', 'create_customer_record'))
        self.assertEqual(self.count('conversations'), 1)

    def test_reused_event_or_action_key_with_changed_payload_rejected(self):
        self.engine.handle(self.i, '1', 'hello')
        with self.assertRaises(ValueError):
            self.engine.handle(self.i, '1', 'different')
        self.engine.execute(self.i, 'action', 'create_customer_record')
        with self.assertRaises(ValueError):
            self.engine.execute(self.i, 'action', 'transfer_call')

    def test_start_is_idempotent_and_rebinding_rejected(self):
        self.assertEqual(self.i, self.engine.start('demo-1', '+15551111111'))
        with self.assertRaises(ValueError):
            self.engine.start('demo-1', '+15552222222')

    def test_cross_business_reads_writes_and_tools_rejected(self):
        other = AgentEngine(self.b, OfflineInterpreter())
        for action in [lambda: other.outcome(self.i), lambda: other.handle(self.i, 'x', 'hello'),
                       lambda: other.execute(self.i, 'x', 'create_customer_record')]:
            with self.assertRaises(LookupError):
                action()

    def test_unconfigured_tools_never_claim_success(self):
        for tool in ['check_calendar_availability', 'create_appointment', 'transfer_call']:
            self.assertEqual(self.engine.execute(self.i, tool, tool)['status'], 'not_configured')
        result = self.engine.handle(self.i, 'book', 'Book an appointment')
        self.assertEqual(result['status'], 'needs_human')
        self.assertIn('cannot', result['reply'])

    def test_disabled_tools_and_arbitrary_tools(self):
        other = AgentEngine(self.b, OfflineInterpreter())
        interaction = other.start('office', '+15551111111')
        self.assertEqual(other.execute(interaction, 'x', 'create_appointment')['status'], 'denied')
        with self.assertRaises(ValueError):
            other.execute(interaction, 'y', 'run_shell')

    def test_missing_or_failed_model_saves_partial_record_safely(self):
        for model in [None, Mock(interpret=Mock(side_effect=TimeoutError('secret must not persist')))]:
            engine = AgentEngine(self.a, model)
            interaction = engine.start('failure-' + str(model is None), '+15551111111')
            result = engine.handle(interaction, '1', 'I need help')
            self.assertEqual(result['status'], 'needs_human')
            self.assertNotIn('secret', str(result))
            self.assertEqual(engine.outcome(interaction)['state']['failure'], 'interpreter_unavailable_or_invalid')

    def test_unsubstantiated_model_fact_and_unknown_intent_rejected(self):
        for decision in [Decision('inquiry', {'service': 'invented'}), Decision('run_shell'),
                         Decision('inquiry', {'business_id': '2'})]:
            engine = AgentEngine(self.a, Mock(interpret=Mock(return_value=decision)))
            interaction = engine.start('invalid-' + str(self.count('agent_interactions')), '+15551111111')
            result = engine.handle(interaction, '1', 'hello')
            self.assertEqual(result['status'], 'needs_human')
            self.assertIsNone(result['fields']['service']['value'])

    def test_abandonment_preserves_partial_facts_after_restart(self):
        self.engine.handle(self.i, '1', 'My kitchen sink is clogged')
        self.engine.handle(self.i, 'end', kind='disconnected')
        result = AgentEngine(self.a).outcome(self.i)
        self.assertEqual(result['state']['status'], 'needs_human')
        self.assertIn('sink', result['fields']['service']['value'])
        self.assertTrue(result['updated_at'].endswith('+00:00'))

    def test_interrupted_utterance_is_not_answer_or_output(self):
        self.engine.handle(self.i, '1', 'My kitchen sink is clogged')
        result = self.engine.handle(self.i, 'interrupt', 'not finished', kind='interrupted')
        self.assertIsNone(result['reply'])
        self.assertIsNone(result['fields']['location']['value'])
        result = self.engine.handle(self.i, '2', 'Phoenix')
        self.assertEqual(result['fields']['location']['value'], 'Phoenix')

    def test_unclear_answer_hands_off_without_question_loop(self):
        self.engine.handle(self.i, '1', 'hello')
        result = self.engine.handle(self.i, '2', 'not sure')
        self.assertEqual(result['status'], 'needs_human')
        self.assertIsNone(self.engine.handle(self.i, '3', 'hello')['reply'])

    def test_opted_out_or_closed_lead_prevents_intake(self):
        with closing(get_connection()) as db, db:
            db.execute('UPDATE conversations SET opted_out=1')
        result = self.engine.handle(self.i, '1', 'My sink is clogged')
        self.assertEqual(result['status'], 'needs_human')
        self.assertIsNone(result['fields']['service']['value'])

    def test_medical_and_invalid_config_rejected(self):
        with self.assertRaises(ValueError):
            configure_business(self.a, regulated_medical=True)
        with self.assertRaises(ValueError):
            configure_business(self.a, permissions=['run_shell'])
        with self.assertRaises(ValueError):
            configure_business(self.a, questions=['diagnosis'])

    def test_configuration_is_snapshotted(self):
        configure_business(self.a, knowledge={'Hours': 'New hours'})
        self.assertIn('Monday', self.engine.handle(self.i, '1', 'hours')['reply'])

    def test_missing_profile_or_record_permission_rejected(self):
        with closing(get_connection()) as db, db:
            db.execute('DELETE FROM agent_profiles WHERE business_id=?', (self.b,))
        with self.assertRaises(ValueError):
            AgentEngine(self.b).start('x', '+15551111111')
        configure_business(self.b, permissions=[])
        with self.assertRaises(PermissionError):
            AgentEngine(self.b).start('x', '+15551111111')

    def test_migration_is_repeatable_and_no_http_activation(self):
        init_agent_store()
        self.assertEqual(self.count('agent_interactions'), 1)
        self.assertFalse(any('agent' in rule.rule for rule in self.app.url_map.iter_rules()))

    def test_concurrent_duplicate_events_commit_once(self):
        def receive(_):
            with self.app.app_context():
                return AgentEngine(self.a, OfflineInterpreter()).handle(self.i, 'same', 'My sink is clogged')
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(receive, range(2)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(self.count('agent_events'), 1)
        self.assertEqual(self.count('agent_actions'), 1)

    def test_information_questions_cannot_loop_indefinitely(self):
        for index in range(9):
            result = self.engine.handle(self.i, str(index), 'What are your hours?')
        self.assertEqual(result['status'], 'needs_human')

