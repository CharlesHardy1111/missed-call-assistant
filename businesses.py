"""Tenant mapping and conversation persistence; no provider calls or credentials."""
from contextlib import closing
import re
import sqlite3
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from werkzeug.security import generate_password_hash

from database import get_connection
from timestamps import utc_now
from sms import DEFAULT_MESSAGE


def init_business_tables(cursor):
    # Execute individually to preserve init_db's migration transaction.
    statements = [
        '''CREATE TABLE IF NOT EXISTS businesses (
            id INTEGER PRIMARY KEY, name TEXT NOT NULL, login TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL, public_phone TEXT NOT NULL UNIQUE,
            mca_phone TEXT NOT NULL UNIQUE, forwarding_phone TEXT NOT NULL,
            timezone TEXT NOT NULL, recovery_message TEXT NOT NULL,
            login_failures INTEGER NOT NULL DEFAULT 0, locked_until REAL NOT NULL DEFAULT 0)''',
        '''CREATE TABLE IF NOT EXISTS conversations (
            id INTEGER PRIMARY KEY, business_id INTEGER NOT NULL REFERENCES businesses(id),
            phone_number TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'new',
            unread INTEGER NOT NULL DEFAULT 0, opted_out INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(business_id, phone_number))''',
        '''CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY, business_id INTEGER NOT NULL REFERENCES businesses(id),
            conversation_id INTEGER NOT NULL REFERENCES conversations(id),
            direction TEXT NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL,
            status TEXT NOT NULL, created_at TEXT NOT NULL, completed_at TEXT,
            provider_sid TEXT UNIQUE, error TEXT, request_key TEXT,
            missed_call_id INTEGER UNIQUE REFERENCES missed_calls(id),
            UNIQUE(business_id, request_key))''',
        '''CREATE TABLE IF NOT EXISTS voice_sessions (
            parent_sid TEXT PRIMARY KEY, business_id INTEGER NOT NULL REFERENCES businesses(id),
            caller TEXT NOT NULL, created_at TEXT NOT NULL)''',
        'CREATE INDEX IF NOT EXISTS messages_conversation ON messages(conversation_id, id)',
        'CREATE INDEX IF NOT EXISTS calls_business ON missed_calls(business_id, conversation_id)',
    ]
    for sql in statements:
        cursor.execute(sql)
    columns = {r['name'] for r in cursor.execute('PRAGMA table_info(conversations)')}
    for name, definition in {
        'lead_source': "TEXT NOT NULL DEFAULT 'unknown'",
        'lead_details': "TEXT NOT NULL DEFAULT '{}'",
    }.items():
        if name not in columns:
            cursor.execute(f'ALTER TABLE conversations ADD COLUMN {name} {definition}')


def valid_phone(value):
    return isinstance(value, str) and re.fullmatch(r'\+[1-9][0-9]{7,14}', value) is not None


def provision_business(name, login, password, public_phone, mca_phone, forwarding_phone,
                       timezone='America/Phoenix'):
    """Operator-only provisioning; assignment requires external ownership verification."""
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise ValueError('Use a valid IANA business timezone.') from None
    login = login.strip().lower()
    if not name.strip() or not login or len(password) < 12:
        raise ValueError('Name, login, and a password of at least 12 characters are required.')
    if not all(valid_phone(p) for p in (public_phone, mca_phone, forwarding_phone)):
        raise ValueError('Numbers must be in E.164 format.')
    if len({public_phone, mca_phone, forwarding_phone}) != 3:
        raise ValueError('Use distinct public, MCA, and ringing destinations to avoid forwarding loops.')
    password_hash = generate_password_hash(password)
    with closing(get_connection()) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        # Numbers cannot change roles between accounts: that could create forwarding loops.
        used = {p for row in conn.execute('SELECT public_phone,mca_phone,forwarding_phone FROM businesses') for p in row}
        if used.intersection({public_phone, mca_phone, forwarding_phone}):
            raise ValueError('A number is already assigned to an account.')
        try:
            return conn.execute('''INSERT INTO businesses
                (name,login,password_hash,public_phone,mca_phone,forwarding_phone,timezone,recovery_message)
                VALUES (?,?,?,?,?,?,?,?)''',
                (name.strip(), login, password_hash, public_phone, mca_phone,
                 forwarding_phone, timezone, DEFAULT_MESSAGE)).lastrowid
        except sqlite3.IntegrityError:
            raise ValueError('An account with that login or number already exists.') from None


def has_businesses():
    with closing(get_connection()) as conn:
        return conn.execute('SELECT 1 FROM businesses LIMIT 1').fetchone() is not None


def business_by_id(business_id):
    with closing(get_connection()) as conn:
        return conn.execute('SELECT * FROM businesses WHERE id=?', (business_id,)).fetchone()


def bind_incoming(data):
    """Bind the parent at intake; child From/To fields never select a tenant."""
    parent, caller = data.get('CallSid'), data.get('From')
    if not parent or not caller:
        return None
    with closing(get_connection()) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        business = conn.execute('SELECT * FROM businesses WHERE mca_phone=?', (data.get('To'),)).fetchone()
        if business is None:
            return None
        existing = conn.execute('SELECT * FROM voice_sessions WHERE parent_sid=?', (parent,)).fetchone()
        if existing and (existing['business_id'] != business['id'] or existing['caller'] != caller):
            return None
        conn.execute('INSERT OR IGNORE INTO voice_sessions VALUES (?,?,?,?)',
                     (parent, business['id'], caller, utc_now()))
        return business


def bound_call(parent):
    with closing(get_connection()) as conn:
        return conn.execute('SELECT * FROM voice_sessions WHERE parent_sid=?', (parent,)).fetchone()


def ensure_conversation(cursor, business_id, phone):
    now = utc_now()
    cursor.execute('''INSERT OR IGNORE INTO conversations
        (business_id,phone_number,created_at,updated_at) VALUES (?,?,?,?)''',
        (business_id, phone, now, now))
    return cursor.execute('SELECT * FROM conversations WHERE business_id=? AND phone_number=?',
                          (business_id, phone)).fetchone()


def conversation(business_id, conversation_id):
    with closing(get_connection()) as conn:
        return conn.execute('SELECT * FROM conversations WHERE business_id=? AND id=?',
                            (business_id, conversation_id)).fetchone()


def message_result(cursor, call_id, status, sid=None, error=None):
    cursor.execute('''UPDATE messages SET status=?,provider_sid=?,error=?,completed_at=?
        WHERE missed_call_id=?''', (status, sid, error,
                                  None if status in {'pending', 'sending'} else utc_now(), call_id))
    if status == 'sent':
        cursor.execute("""UPDATE conversations SET status='contacted'
            WHERE status='new' AND id=(SELECT conversation_id FROM messages WHERE missed_call_id=?)""", (call_id,))
