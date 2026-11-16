"""Transactional message intake and at-most-once outbound claims."""
from contextlib import closing
import re

from flask import current_app

from businesses import ensure_conversation, valid_phone
from database import get_connection
from sms import send_sms, SendResult
from timestamps import utc_now
from lead_capture import capture_message, queue_question, note_source, pause_capture, load_details, missing_fields


def receive_sms(data):
    sid, caller, body = data.get('MessageSid', ''), data.get('From', ''), data.get('Body', '')
    if (not re.fullmatch(r'SM[0-9a-fA-F]{32}', sid) or not valid_phone(caller)
            or not body.strip() or len(body) > 1600 or data.get('NumMedia', '0') != '0'):
        raise ValueError('Unsupported SMS payload.')
    with closing(get_connection()) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        business = conn.execute('SELECT id FROM businesses WHERE mca_phone=?', (data.get('To'),)).fetchone()
        if business is None:
            raise LookupError('Unknown destination.')
        existing = conn.execute('SELECT * FROM messages WHERE provider_sid=?', (sid,)).fetchone()
        if existing:
            if existing['business_id'] != business['id'] or existing['direction'] != 'inbound':
                raise ValueError('Message ownership mismatch.')
            return existing['id']
        lead = ensure_conversation(conn, business['id'], caller)
        now = utc_now()
        message_id = conn.execute('''INSERT INTO messages
            (business_id,conversation_id,direction,kind,body,status,created_at,provider_sid)
            VALUES (?,?,'inbound','customer',?,'received',?,?)''',
            (business['id'], lead['id'], body, now, sid)).lastrowid
        opted_out = lead['opted_out']
        keyword = body.strip().upper()
        if (data.get('OptOutType') == 'STOP'
                or keyword in {'STOP','STOPALL','UNSUBSCRIBE','CANCEL','END','QUIT','REVOKE','OPTOUT'}
                or re.search(r"\b(?:stop|don't|do not) (?:texting|messaging|contacting)(?: me)?\b|\bremove me\b", body, re.I)):
            opted_out = 1
        elif data.get('OptOutType') == 'START' or keyword in {'START', 'UNSTOP', 'YES'}:
            opted_out = 0
        conn.execute("""UPDATE conversations SET status='replied',unread=1,updated_at=?,opted_out=?
            WHERE id=?""", (now, opted_out, lead['id']))
        note_source(conn, lead['id'], 'sms')
        if lead['status'] == 'closed':
            pause_capture(conn, lead['id'])
        field = capture_message(conn, lead['id'], message_id, body, data.get('OptOutType'))
        question_id = None
        if (field and not opted_out and current_app.config.get('ENABLE_PROGRESSIVE_CAPTURE') is True
                and current_app.config.get('ENABLE_SMS_FOLLOWUP') is True):
            question_id = queue_question(conn, lead['id'], field)
    # Persist customer facts and inbox notification BEFORE the bounded send.
    if question_id is not None:
        dispatch_question(question_id)
    return message_id


def reply(business_id, conversation_id, body, request_key):
    if not body.strip() or len(body) > 1600 or not re.fullmatch(r'[a-f0-9]{32}', request_key):
        raise ValueError('Enter a message of 1–1600 characters.')
    with closing(get_connection()) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        lead = conn.execute('SELECT * FROM conversations WHERE id=? AND business_id=?',
                            (conversation_id, business_id)).fetchone()
        if lead is None:
            raise LookupError('Conversation not found.')
        prior = conn.execute('SELECT * FROM messages WHERE business_id=? AND request_key=?',
                             (business_id, request_key)).fetchone()
        if prior:
            if prior['conversation_id'] != conversation_id or prior['body'] != body:
                raise ValueError('This submission was already used. Reload the conversation.')
            return prior['id']
        business = conn.execute('SELECT * FROM businesses WHERE id=?', (business_id,)).fetchone()
        enabled = current_app.config.get('ENABLE_SMS_FOLLOWUP') is True
        status = 'disabled' if not enabled else ('not_attempted' if lead['opted_out'] else 'sending')
        error = 'recipient_opted_out' if lead['opted_out'] else None
        message_id = conn.execute('''INSERT INTO messages
            (business_id,conversation_id,direction,kind,body,status,created_at,request_key,error)
            VALUES (?,?,'outbound','reply',?,?,?,?,?)''',
            (business_id, conversation_id, body, status, utc_now(), request_key, error)).lastrowid
        conn.execute('UPDATE conversations SET updated_at=? WHERE id=?', (utc_now(), conversation_id))
        pause_capture(conn, conversation_id)
    # Claim is committed before any network call. Retries never resend a claim.
    if status != 'sending':
        return message_id
    return send_claimed_message(message_id, lead, business, body)


def send_claimed_message(message_id, lead, business, body):
    """Shared transport/result handling for business replies and clarifications."""
    try:
        result = send_sms(lead['phone_number'], body, sender=business['mca_phone'])
    except Exception:
        result = SendResult('failed', error='unknown_outcome')
    with closing(get_connection()) as conn, conn:
        conn.execute('''UPDATE messages SET status=?,provider_sid=?,error=?,completed_at=?
            WHERE id=? AND status='sending' ''',
            (result.status, result.message_sid, result.error, utc_now(), message_id))
        message = conn.execute('SELECT kind FROM messages WHERE id=?', (message_id,)).fetchone()
        if result.status == 'sent' and message['kind'] == 'reply':
            conn.execute("UPDATE conversations SET status='contacted' WHERE id=? AND status!='closed'",
                         (lead['id'],))
    return message_id


def dispatch_question(message_id):
    """Claim once, rechecking opt-out, ownership and whether the question is still needed."""
    with closing(get_connection()) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        message = conn.execute("SELECT * FROM messages WHERE id=? AND kind='clarification'", (message_id,)).fetchone()
        if message is None or message['status'] != 'pending':
            return
        lead = conn.execute('SELECT * FROM conversations WHERE id=? AND business_id=?',
                            (message['conversation_id'], message['business_id'])).fetchone()
        details = load_details(lead)
        pending = details['pending']
        allowed = (current_app.config.get('ENABLE_SMS_FOLLOWUP') is True
                   and current_app.config.get('ENABLE_PROGRESSIVE_CAPTURE') is True
                   and not lead['opted_out'] and lead['status'] != 'closed' and not details['paused']
                   and pending and pending['message_id'] == message_id
                   and pending['field'] in missing_fields(details))
        if not allowed:
            conn.execute("UPDATE messages SET status='not_attempted',completed_at=? WHERE id=?", (utc_now(), message_id))
            return
        business = conn.execute('SELECT * FROM businesses WHERE id=?', (message['business_id'],)).fetchone()
        conn.execute("UPDATE messages SET status='sending' WHERE id=?", (message_id,))
    send_claimed_message(message_id, lead, business, message['body'])
