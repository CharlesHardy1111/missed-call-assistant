"""Offline agent orchestration. No network, public routes, or production activation.

Callers are trusted application code: resolve business identity before invoking this
module. Model decisions are untrusted proposals; tools never accept a tenant ID.
"""
from contextlib import closing
from dataclasses import dataclass, field
import json
import re
from typing import Protocol

from businesses import ensure_conversation, valid_phone
from database import get_connection
from lead_capture import FIELDS, QUESTIONS, extract, load_details, save_details
from timestamps import utc_now

TOOLS = frozenset({'get_business_information', 'check_calendar_availability',
                   'create_appointment', 'transfer_call', 'create_customer_record',
                   'notify_business_owner'})
INTENTS = {'inquiry', 'information', 'availability', 'appointment', 'human'}


@dataclass(frozen=True)
class Decision:
    intent: str = 'inquiry'
    facts: dict = field(default_factory=dict)


class Interpreter(Protocol):
    def interpret(self, text: str, pending: str | None) -> Decision:
        """Return intent and verbatim facts, never executable code or promises."""


class OfflineInterpreter:
    """Deterministic demo/test substitute, NOT an LLM or a voice implementation."""
    def interpret(self, text, pending):
        intent = 'inquiry'
        for pattern, candidate in [
            (r'\b(hours|open|information)\b', 'information'),
            (r'\b(available|availability)\b', 'availability'),
            (r'\b(book|appointment|schedule)\b', 'appointment'),
            (r'\b(human|person|transfer)\b', 'human'),
        ]:
            if re.search(pattern, text, re.I):
                intent = candidate
        facts = {k: v for k, v in extract(text, pending).items()
                 if k in FIELDS and isinstance(v, str) and v in text}
        return Decision(intent, facts)


def init_agent_store():
    """Explicit opt-in migration for offline use; not called by Flask startup."""
    with closing(get_connection()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('''CREATE TABLE IF NOT EXISTS agent_profiles (
            business_id INTEGER PRIMARY KEY REFERENCES businesses(id), config TEXT NOT NULL)''')
        db.execute('''CREATE TABLE IF NOT EXISTS agent_interactions (
            id INTEGER PRIMARY KEY, business_id INTEGER NOT NULL REFERENCES businesses(id),
            conversation_id INTEGER NOT NULL REFERENCES conversations(id), external_id TEXT NOT NULL,
            config TEXT NOT NULL, state TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(business_id, external_id))''')
        db.execute('''CREATE TABLE IF NOT EXISTS agent_events (
            interaction_id INTEGER NOT NULL REFERENCES agent_interactions(id), event_id TEXT NOT NULL,
            payload TEXT NOT NULL, result TEXT NOT NULL, created_at TEXT NOT NULL,
            PRIMARY KEY(interaction_id,event_id))''')
        db.execute('''CREATE TABLE IF NOT EXISTS agent_actions (
            interaction_id INTEGER NOT NULL REFERENCES agent_interactions(id), action_id TEXT NOT NULL,
            name TEXT NOT NULL, result TEXT NOT NULL, created_at TEXT NOT NULL,
            PRIMARY KEY(interaction_id,action_id))''')


def configure_business(business_id, *, knowledge=None, questions=('service', 'urgency', 'callback'),
                       permissions=('get_business_information', 'create_customer_record',
                                    'notify_business_owner'), regulated_medical=False):
    """Operator-only configuration, snapshotted at interaction start."""
    if regulated_medical:
        raise ValueError('Regulated medical workflows are not supported.')
    knowledge = knowledge or {}
    if (not isinstance(knowledge, dict) or len(knowledge) > 20
            or any(not isinstance(k, str) or not isinstance(v, str)
                   or not 1 <= len(k) <= 80 or not 1 <= len(v) <= 500 for k, v in knowledge.items())
            or len(questions) > 4 or len(set(questions)) != len(questions)
            or any(q not in QUESTIONS for q in questions)
            or not set(permissions) <= TOOLS):
        raise ValueError('Invalid business agent configuration.')
    config = json.dumps(dict(knowledge=knowledge, questions=list(questions), permissions=list(permissions)))
    with closing(get_connection()) as db, db:
        if not db.execute('SELECT 1 FROM businesses WHERE id=?', (business_id,)).fetchone():
            raise LookupError('Unknown business.')
        db.execute('INSERT INTO agent_profiles VALUES (?,?) ON CONFLICT(business_id) DO UPDATE SET config=excluded.config',
                   (business_id, config))


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9:_-]{1,100}', value):
        raise ValueError('Invalid event identifier.')


class AgentEngine:
    def __init__(self, business_id, interpreter=None):
        self.business_id = business_id
        self.interpreter = interpreter  # Missing model fails safely, never silently calls a provider.

    def _owned(self, db, interaction_id):
        row = db.execute('SELECT * FROM agent_interactions WHERE id=? AND business_id=?',
                         (interaction_id, self.business_id)).fetchone()
        if row is None:
            raise LookupError('Unknown interaction.')
        return row

    def start(self, external_id, phone):
        _identifier(external_id)
        if not valid_phone(phone):
            raise ValueError('A valid candidate contact number is required.')
        with closing(get_connection()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            prior = db.execute('SELECT * FROM agent_interactions WHERE business_id=? AND external_id=?',
                               (self.business_id, external_id)).fetchone()
            if prior:
                contact = db.execute('SELECT phone_number FROM conversations WHERE id=?',
                                     (prior['conversation_id'],)).fetchone()[0]
                if contact != phone:
                    raise ValueError('Interaction identity conflict.')
                return prior['id']
            profile = db.execute('SELECT config FROM agent_profiles WHERE business_id=?',
                                 (self.business_id,)).fetchone()
            if not profile:
                raise ValueError('Agent configuration is missing.')
            config = json.loads(profile['config'])
            if 'create_customer_record' not in config['permissions']:
                raise PermissionError('Lead capture is not permitted.')
            lead = ensure_conversation(db, self.business_id, phone)
            state = dict(status='active', intent='unknown', asked=[], pending=None, turns=0)
            now = utc_now()
            return db.execute('''INSERT INTO agent_interactions
                (business_id,conversation_id,external_id,config,state,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?)''', (self.business_id, lead['id'], external_id,
                profile['config'], json.dumps(state), now, now)).lastrowid

    def _tool(self, db, row, action_id, name):
        prior = db.execute('SELECT name,result FROM agent_actions WHERE interaction_id=? AND action_id=?',
                           (row['id'], action_id)).fetchone()
        if prior:
            if prior['name'] != name:
                raise ValueError('Action identity conflict.')
            return json.loads(prior['result'])
        config = json.loads(row['config'])
        result = {'status': 'denied'}
        if name in TOOLS and name in config['permissions']:
            if name == 'get_business_information':
                result = {'status': 'confirmed', 'information': config['knowledge']}
            elif name == 'create_customer_record':
                result = {'status': 'confirmed', 'conversation_id': row['conversation_id']}
            elif name == 'notify_business_owner':
                db.execute('UPDATE conversations SET unread=1,updated_at=? WHERE id=? AND business_id=?',
                           (utc_now(), row['conversation_id'], self.business_id))
                result = {'status': 'confirmed', 'channel': 'inbox', 'delivery': 'unread_activity_only'}
            else:
                # No calendar/telephony adapters installed. Never claim booked/transferred.
                result = {'status': 'not_configured'}
        db.execute('INSERT INTO agent_actions VALUES (?,?,?,?,?)',
                   (row['id'], action_id, name, json.dumps(result), utc_now()))
        return result

    def execute(self, interaction_id, action_id, name):
        """Restricted internal tool boundary; no destinations, URLs, SQL, or tenant arguments."""
        _identifier(action_id)
        if name not in TOOLS:
            raise ValueError('Unknown tool.')
        with closing(get_connection()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            return self._tool(db, self._owned(db, interaction_id), action_id, name)

    def handle(self, interaction_id, event_id, text='', *, kind='utterance'):
        """Only finalized utterances; interrupted audio is not treated as a full answer.

        Calls are serialized for the offline interpreter. A future network model
        requires bounded async execution and cancellation outside this transaction.
        """
        _identifier(event_id)
        if kind not in {'utterance', 'interrupted', 'disconnected', 'ended'} or not isinstance(text, str) or len(text) > 1600:
            raise ValueError('Unsupported event.')
        if kind == 'utterance' and not text.strip():
            raise ValueError('Empty utterance.')
        payload = json.dumps({'kind': kind, 'text': text}, sort_keys=True)
        with closing(get_connection()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = self._owned(db, interaction_id)
            prior = db.execute('SELECT payload,result FROM agent_events WHERE interaction_id=? AND event_id=?',
                               (interaction_id, event_id)).fetchone()
            if prior:
                if prior['payload'] != payload:
                    raise ValueError('Event identity conflict.')
                return json.loads(prior['result'])
            state, config = json.loads(row['state']), json.loads(row['config'])
            lead = db.execute('SELECT * FROM conversations WHERE id=? AND business_id=?',
                              (row['conversation_id'], self.business_id)).fetchone()
            details = load_details(lead)
            reply, actions = None, []
            if kind in {'ended', 'disconnected'}:
                # Transport shutdown must not erase a successfully completed intake.
                if state['status'] != 'complete':
                    state['status'] = 'ended' if kind == 'ended' else 'needs_human'
                state['connection_ended'] = True
                state['pending'] = None
            elif kind == 'utterance' and state['status'] == 'active':
                if lead['opted_out'] or lead['status'] == 'closed' or state['turns'] >= 8:
                    state['status'] = 'needs_human'
                    reply = 'I cannot continue automated intake. Please contact the business directly.'
                else:
                    try:
                        decision = self.interpreter.interpret(text, state['pending']) if self.interpreter else None
                        if (not isinstance(decision, Decision) or decision.intent not in INTENTS
                                or not isinstance(decision.facts, dict) or any(
                                    k not in FIELDS or not isinstance(v, str) or not v.strip()
                                    or len(v) > 240 or v not in text for k, v in decision.facts.items())):
                            raise ValueError('Invalid model decision.')
                    except Exception:
                        # Never store/log exception text: it may contain keys or customer data.
                        state['status'] = 'needs_human'
                        state['failure'] = 'interpreter_unavailable_or_invalid'
                        reply = 'I cannot continue automated intake right now. Your partial request is saved for review.'
                    else:
                        state['intent'] = decision.intent
                        state['turns'] += 1
                        for key, value in decision.facts.items():
                            if key == 'callback' and not valid_phone(value):
                                continue
                            details['fields'][key] = dict(value=value, state='customer_reported',
                                                         message_id=None, agent_event=f'{interaction_id}:{event_id}')
                        if state['pending'] == 'callback' and text.strip().lower() in {'yes', 'yes.'}:
                            details['fields']['callback'] = dict(value=lead['phone_number'], state='confirmed',
                                                                message_id=None, agent_event=f'{interaction_id}:{event_id}')
                        if decision.intent != 'inquiry':
                            name = {'information': 'get_business_information', 'availability': 'check_calendar_availability',
                                    'appointment': 'create_appointment', 'human': 'transfer_call'}[decision.intent]
                            result = self._tool(db, row, event_id + ':tool', name)
                            actions.append({'tool': name, **result})
                            if name == 'get_business_information' and result['status'] == 'confirmed' and result['information']:
                                reply = ' '.join(f'{k}: {v}' for k, v in result['information'].items())
                            else:
                                state['status'] = 'needs_human'
                                reply = 'I cannot confirm availability, book an appointment, or transfer this call here. The business needs to review your request.'
                        else:
                            missing = [q for q in config['questions'] if details['fields'][q]['state'] in {'unknown', 'candidate', 'declined'}]
                            unanswered = state['pending'] in missing
                            next_field = next((q for q in missing if q not in state['asked']), None)
                            if unanswered or (missing and next_field is None) or state['turns'] >= 8:
                                state['status'] = 'needs_human'
                                reply = 'Your partial request is saved. The business needs to review the remaining details.'
                            elif next_field:
                                state['asked'].append(next_field)
                                state['pending'] = next_field
                                reply = QUESTIONS[next_field]
                            else:
                                state['status'] = 'complete'
                                reply = 'Your request is saved for review. No appointment or quote has been confirmed.'
            if state['status'] != 'active':
                state['pending'] = None
            save_details(db, lead['id'], details)
            db.execute('UPDATE conversations SET updated_at=? WHERE id=? AND business_id=?',
                       (utc_now(), lead['id'], self.business_id))
            # The supported notification path is unread activity, not an email/SMS promise.
            actions.append({'tool': 'notify_business_owner', **self._tool(db, row, event_id + ':notify', 'notify_business_owner')})
            result = dict(status=state['status'], intent=state['intent'], reply=reply,
                          fields=details['fields'], actions=actions, conversation_id=lead['id'])
            now = utc_now()
            db.execute('UPDATE agent_interactions SET state=?,updated_at=? WHERE id=?',
                       (json.dumps(state), now, interaction_id))
            db.execute('INSERT INTO agent_events VALUES (?,?,?,?,?)',
                       (interaction_id, event_id, payload, json.dumps(result), now))
            return result

    def outcome(self, interaction_id):
        with closing(get_connection()) as db:
            row = self._owned(db, interaction_id)
            lead = db.execute('SELECT * FROM conversations WHERE id=? AND business_id=?',
                              (row['conversation_id'], self.business_id)).fetchone()
            fields = load_details(lead)['fields']
            missing = [q for q in json.loads(row['config'])['questions']
                       if fields[q]['state'] in {'unknown', 'candidate', 'declined'}]
            return dict(interaction_id=row['id'], conversation_id=lead['id'],
                        state=json.loads(row['state']), fields=fields, missing=missing,
                        summary=fields['service']['value'] or 'Request not yet provided.',
                        next_action='Owner review required; no booking or transfer confirmed.',
                        created_at=row['created_at'], updated_at=row['updated_at'])
