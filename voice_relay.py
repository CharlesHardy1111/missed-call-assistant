"""Offline ConversationRelay protocol adapter; no listener or provider connection.

The future WSS host must authenticate before constructing a session and resolve
the binding from its own signed intake. Neither setup nor customParameters selects
the business. This adapter is deliberately not imported by app.py.
"""
from contextlib import closing
from dataclasses import dataclass
from html import escape
import json
import re
import sqlite3
import time
from urllib.parse import urlsplit

from twilio.request_validator import RequestValidator

from database import get_connection
from timestamps import utc_now


def authenticate_handshake(public_url, raw_path_query, signature, auth_token):
    """Validate one configured external URL; never use Host/proxy headers.

    public_url is operator configuration, raw_path_query is the unmodified request
    target. The staging smoke test must confirm Twilio's exact signed URL scheme.
    No alternate schemes/hosts are tried on failure.
    """
    try:
        url = urlsplit(public_url)
        url.port  # Reject malformed ports.
        if (url.scheme not in {'https', 'wss'} or not url.hostname or url.username is not None
                or url.password is not None or url.fragment or any(c.isspace() for c in public_url)
                or not auth_token or not re.fullmatch(r'[A-Za-z0-9+/]{27}=', signature or '')):
            return False
        target = (url.path or '/') + ('?' + url.query if url.query else '')
        return raw_path_query == target and RequestValidator(auth_token).validate(public_url, {}, signature)
    except (TypeError, ValueError):
        return False


def init_relay_store():
    """Explicit offline migration; durable single-use call claim prevents reconnect replay."""
    with closing(get_connection()) as db, db:
        db.execute('''CREATE TABLE IF NOT EXISTS agent_voice_claims (
            call_sid TEXT PRIMARY KEY, interaction_id INTEGER NOT NULL UNIQUE
            REFERENCES agent_interactions(id), session_id TEXT NOT NULL,
            created_at TEXT NOT NULL)''')


@dataclass(frozen=True)
class CallBinding:
    call_sid: str
    account_sid: str
    caller: str
    destination: str
    expires_at: float  # monotonic deadline, owned by this process


class RelaySession:
    """One authenticated, ordered socket, with the existing offline AgentEngine.

    Transport must call tick during silence and disconnect on socket closure.
    There is no async/network interpreter here and no automatic reconnection.
    """
    def __init__(self, engine, interaction_id, binding, *, clock=time.monotonic,
                 idle_seconds=30, max_seconds=300):
        if idle_seconds <= 0 or max_seconds <= 0:
            raise ValueError('Positive session limits required.')
        engine.outcome(interaction_id)  # tenant scope checked before accepting any frame
        with closing(get_connection()) as db:
            identity = db.execute('''SELECT c.phone_number,b.mca_phone
                FROM agent_interactions i JOIN conversations c ON c.id=i.conversation_id
                JOIN businesses b ON b.id=i.business_id WHERE i.id=? AND i.business_id=?''',
                (interaction_id, engine.business_id)).fetchone()
        if identity is None or (identity['phone_number'], identity['mca_phone']) != (binding.caller, binding.destination):
            raise ValueError('Call binding does not match the interaction.')
        self.engine, self.interaction_id, self.binding = engine, interaction_id, binding
        self.clock, self.started = clock, clock()
        self.last_activity = self.started
        self.idle_seconds, self.max_seconds = idle_seconds, max_seconds
        self.session_id = None
        self.closed = False
        self.counter = 0
        self.metrics = []  # bounded, numeric controller latency only, no transcript/IDs

    def _event(self):
        self.counter += 1
        return f'relay:{self.counter}'

    def _end(self, reason):
        if self.closed:
            return []
        self.closed = True
        if self.session_id:
            self.engine.handle(self.interaction_id, self._event(), kind='disconnected')
        # Connect action must consume this disposition. It is NOT a completed transfer.
        return [{'type': 'end', 'handoffData': json.dumps({'disposition': 'needs_human', 'reason': reason})}]

    def tick(self):
        if self.closed:
            return []
        now = self.clock()
        if now - self.started >= self.max_seconds:
            return self._end('duration_limit')
        if now - self.last_activity >= self.idle_seconds:
            return self._end('silence_timeout')
        return []

    def disconnect(self):
        self._end('connection_lost')

    def end(self, reason):
        """Host-level limit; only trusted host code supplies safe reason codes."""
        return self._end(reason)

    def receive(self, frame):
        """Consume JSON text; return protocol messages to send, never write a socket."""
        expired = self.tick()
        if expired or self.closed:
            return expired
        try:
            valid_frame = isinstance(frame, str) and len(frame.encode('utf-8')) <= 8192
        except UnicodeError:
            valid_frame = False
        if not valid_frame:
            return self._end('invalid_frame')
        try:
            data = json.loads(frame)
        except (ValueError, RecursionError):
            return self._end('invalid_frame')
        if not isinstance(data, dict):
            return self._end('invalid_frame')
        kind = data.get('type')
        if kind == 'setup':
            # Repeated setup even with the same IDs is not a new interaction.
            if self.session_id:
                return self._end('duplicate_setup')
            b = self.binding
            if (self.clock() >= b.expires_at or data.get('connectionType', 'initial') != 'initial'
                    or data.get('callSid') != b.call_sid or data.get('accountSid') != b.account_sid
                    or data.get('from') != b.caller or data.get('to') != b.destination
                    or not isinstance(data.get('sessionId'), str)
                    or not re.fullmatch(r'VX[0-9a-fA-F]{32}', data['sessionId'])):
                return self._end('binding_rejected')
            try:
                with closing(get_connection()) as db, db:
                    db.execute('INSERT INTO agent_voice_claims VALUES (?,?,?,?)',
                               (b.call_sid, self.interaction_id, data['sessionId'], utc_now()))
            except sqlite3.IntegrityError:
                return self._end('replay_rejected')
            self.session_id = data['sessionId']
            self.last_activity = self.clock()
            return []
        if not self.session_id:
            return self._end('setup_required')
        if kind == 'interrupt':
            # Spoken-question completion is uncertain: do not interpret a following
            # "yes" as confirmation of an unheard callback question. Fail to human.
            return self._end('speech_interrupted')
        if kind == 'error':
            return self._end('provider_error')  # never retain raw provider error strings
        if kind == 'dtmf':
            return self._end('human_requested')
        if kind != 'prompt' or type(data.get('last')) is not bool:
            return self._end('unsupported_frame')
        text = data.get('voicePrompt')
        if not isinstance(text, str) or not text.strip() or len(text) > 1600:
            return self._end('invalid_prompt')
        # Do not treat a partial transcript as a confirmed answer or extend the
        # silence timeout indefinitely with interim frames.
        if not data['last']:
            return []
        self.last_activity = self.clock()
        if text.strip().upper() in {'STOP', 'STOPALL', 'UNSUBSCRIBE', 'QUIT'} or re.search(
                r"\b(?:stop|don't|do not) (?:texting|messaging|contacting)(?: me)?\b|\bremove me\b", text, re.I):
            # No SMS resubscription based on spoken YES; preserve existing opt-outs.
            lead_id = self.engine.outcome(self.interaction_id)['conversation_id']
            with closing(get_connection()) as db, db:
                db.execute('UPDATE conversations SET opted_out=1 WHERE id=? AND business_id=?',
                           (lead_id, self.engine.business_id))
            return self._end('consent_withdrawn')
        before = self.clock()
        result = self.engine.handle(self.interaction_id, self._event(), text)
        self.metrics.append(max(0, self.clock() - before))
        self.metrics = self.metrics[-8:]
        if result['status'] == 'needs_human':
            return self._end('owner_review_required')
        messages = []
        if result['reply']:
            # Twilio supports SSML: escape user/operator-derived text before synthesis.
            messages.append({'type': 'text', 'token': escape(result['reply']), 'last': True,
                             'interruptible': True, 'preemptible': True})
        return messages
