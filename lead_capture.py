"""Small, deterministic SMS intake. Customer statements are evidence, not inference."""
import json
import re

FIELDS = ('customer_name', 'service', 'location', 'urgency', 'callback')
QUESTIONS = {
    'service': 'What do you need help with?',
    'location': 'Where is the service needed? A city or area is enough for now.',
    'urgency': 'How soon do you need help?',
    'callback': 'Is this the best number to reach you? Reply yes, or share your preferred number with country code.',
}
CONTROL_WORDS = {'STOP', 'STOPALL', 'UNSUBSCRIBE', 'CANCEL', 'END', 'QUIT',
                 'REVOKE', 'OPTOUT', 'START', 'UNSTOP', 'HELP'}
ONSITE = re.compile(r'\b(sink|drain|clogged|leak|plumb\w*|toilet|roof|heater|furnace|air condition\w*|hvac|electri\w*|landscap\w*|pest)\b', re.I)
UNCLEAR = re.compile(r"^(?:hi|hello|hey|thanks?|thank you|ok(?:ay)?|yes|no|maybe|not sure|unknown|unsure|not provided|n/a|i don'?t know)[.! ]*$", re.I)
DECLINE = re.compile(r"\b(skip|prefer not|rather not|don'?t want to share|do not want to share)\b", re.I)


def load_details(row):
    details = json.loads(row['lead_details'] or '{}')
    details.setdefault('version', 1)
    details.setdefault('fields', {})
    for field in FIELDS:
        details['fields'].setdefault(field, {'value': None, 'state': 'unknown', 'message_id': None})
    if details['fields']['callback']['value'] is None:
        details['fields']['callback'] = {'value': row['phone_number'], 'state': 'candidate', 'message_id': None}
    details.setdefault('asked', [])
    details.setdefault('pending', None)
    details.setdefault('paused', False)
    return details


def save_details(conn, conversation_id, details):
    conn.execute('UPDATE conversations SET lead_details=? WHERE id=?',
                 (json.dumps(details, ensure_ascii=False), conversation_id))


def note_source(conn, conversation_id, source):
    conn.execute('''UPDATE conversations SET lead_source=CASE
        WHEN lead_source='unknown' THEN ? WHEN lead_source=? THEN lead_source
        ELSE 'call+sms' END WHERE id=?''', (source, source, conversation_id))


def missing_fields(details):
    fields = details['fields']
    relevant = ['service']
    if ONSITE.search(fields['service']['value'] or ''):
        relevant.append('location')
    relevant += ['urgency', 'callback']
    return [f for f in relevant if fields[f]['state'] in {'unknown', 'candidate', 'declined'}]


def extract(body, pending):
    """Only explicit labels/phrases or an answer to the single last-sent question."""
    values = {}
    patterns = {
        'customer_name': r"\b(?:my name is|name:)\s*([^;,.!\n]+)",
        'service': r'\b(?:service|problem|issue):\s*([^;.!?\n]+)',
        'location': r"(?:\b(?:location|address|city|area):\s*|\b(?:I am|I'm|we are|we're) (?:in|at)\s+|^(?:in|at)\s+)([^;.!?\n]+)",
        'callback': r'\b(?:call|reach|phone|callback)(?: me)?(?: at| on|:)?\s*(\+?[\d() .-]{7,25}\d)',
    }
    for field, pattern in patterns.items():
        match = re.search(pattern, body, re.I)
        if match:
            value = re.split(r'\s+and\s+', match.group(1), maxsplit=1, flags=re.I)[0].strip()
            if value:
                values[field] = value[:240]
    if re.search(r'\b(no rush|not urgent|not an emergency|not an urgency|not today)\b', body, re.I):
        match = re.search(r'\b(no rush|not urgent|not an emergency|not an urgency|not today)\b', body, re.I)
        values['urgency'] = match.group(0)
    else:
        match = re.search(r'\b(urgent|emergency|asap|today|tomorrow|this week|next week)\b', body, re.I)
        if match:
            values['urgency'] = match.group(0)
    if 'service' not in values:
        for clause in re.split(r'[;.!?\n]', body):
            clause = clause.strip()
            if ONSITE.search(clause) or re.search(r'\b(?:I|we) (?:need|want|would like)\b', clause, re.I):
                if not re.match(r'^(?:location|address|urgency|name|callback):', clause, re.I):
                    values['service'] = clause[:240]
                    break
    if pending == 'callback':
        if re.fullmatch(r'yes[.! ]*', body, re.I):
            values['callback_confirmed'] = True
        elif re.fullmatch(r'\+[1-9]\d{7,14}', body.strip()):
            values['callback'] = body.strip()
    elif pending and pending not in values and not values and not UNCLEAR.fullmatch(body.strip()) and not DECLINE.search(body):
        if '?' not in body:
            values[pending] = body.strip()[:240]
    return {field: value for field, value in values.items()
            if not isinstance(value, str) or not UNCLEAR.fullmatch(value)}


def capture_message(conn, conversation_id, message_id, body, opt_out_type=None):
    row = conn.execute('SELECT * FROM conversations WHERE id=?', (conversation_id,)).fetchone()
    details = load_details(row)
    callback_yes = (body.strip().upper() == 'YES' and details['pending']
                    and details['pending']['field'] == 'callback')
    if (row['opted_out'] or opt_out_type in {'STOP', 'HELP'}
            or (opt_out_type == 'START' and not callback_yes)
            or body.strip().upper() in CONTROL_WORDS):
        details['pending'] = None
        save_details(conn, conversation_id, details)
        return None
    pending = details['pending']
    pending_field = None
    if pending:
        message = conn.execute('SELECT status FROM messages WHERE id=?', (pending['message_id'],)).fetchone()
        if message and message['status'] == 'sent':
            pending_field = pending['field']
            details['pending'] = None
        elif message and message['status'] in {'failed', 'not_attempted', 'disabled'}:
            details['paused'] = True
            details['pending'] = None
    values = extract(body, pending_field)
    for field in FIELDS:
        if field in values:
            details['fields'][field] = {'value': values[field], 'state': 'customer_reported', 'message_id': message_id}
    if values.get('callback_confirmed'):
        details['fields']['callback'] = {'value': row['phone_number'], 'state': 'confirmed', 'message_id': message_id}
    if pending_field and pending_field not in values and not values.get('callback_confirmed'):
        if DECLINE.search(body):
            details['fields'][pending_field]['state'] = 'declined'
        else:
            # Never loop on an unclear answer. Keep partial facts and hand off.
            details['paused'] = True
    save_details(conn, conversation_id, details)
    if details['paused'] or details['pending'] or row['status'] == 'closed':
        return None
    # One question per missing field, maximum four per conversation. No name form.
    for field in missing_fields(details):
        if field not in details['asked']:
            return field
    return None


def queue_question(conn, conversation_id, field):
    from timestamps import utc_now
    row = conn.execute('SELECT * FROM conversations WHERE id=?', (conversation_id,)).fetchone()
    details = load_details(row)
    if field in details['asked']:
        return None
    key = f'capture:{conversation_id}:{field}'
    message_id = conn.execute('''INSERT INTO messages
        (business_id,conversation_id,direction,kind,body,status,created_at,request_key)
        VALUES (?,?,'outbound','clarification',?,'pending',?,?)''',
        (row['business_id'], conversation_id, QUESTIONS[field], utc_now(), key)).lastrowid
    details['asked'].append(field)
    details['pending'] = {'field': field, 'message_id': message_id}
    save_details(conn, conversation_id, details)
    return message_id


def pause_capture(conn, conversation_id):
    row = conn.execute('SELECT * FROM conversations WHERE id=?', (conversation_id,)).fetchone()
    details = load_details(row)
    details['paused'] = True
    save_details(conn, conversation_id, details)


def lead_card(conn, row):
    details = load_details(row)
    fields = details['fields']
    missing = missing_fields(details)
    summary = fields['service']['value'] or 'Service need not yet provided.'
    if row['opted_out']:
        action = 'Review lead — customer opted out; do not text.'
    elif row['status'] == 'closed':
        action = 'Closed — review history before further contact.'
    elif details['paused']:
        action = 'Owner review needed; automatic questions are paused.'
    elif details['pending']:
        message = conn.execute('SELECT status FROM messages WHERE id=?', (details['pending']['message_id'],)).fetchone()
        if message and message['status'] == 'sent':
            action = 'Await customer clarification; partial lead is saved.'
        else:
            action = 'Review clarification send status; do not retry blindly.'
    elif missing:
        action = 'Review partial lead; clarify ' + ', '.join(missing) + ' when appropriate.'
    else:
        action = 'Review the request and contact the customer. No appointment or quote has been promised.'
    return {'fields': fields, 'summary': summary, 'missing': missing, 'next_action': action,
            'source': row['lead_source'], 'completeness': 'partial' if missing else 'complete',
            'consent': 'Opted out' if row['opted_out'] else 'Not opted out; not blanket marketing consent'}
