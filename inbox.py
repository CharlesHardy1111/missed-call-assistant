"""Authenticated business inbox; account identity comes only from the session."""
from contextlib import closing
from functools import wraps
import hmac
import secrets
import time

import click
from flask import Blueprint, Response, abort, current_app, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash

from businesses import business_by_id, conversation, provision_business
from database import get_connection
from messaging import receive_sms, reply
from sms import SAFE_ERRORS
from timestamps import display_timestamp
from lead_capture import lead_card, pause_capture

inbox = Blueprint('inbox', __name__)


def csrf_token():
    if 'csrf' not in session:
        session['csrf'] = secrets.token_hex(32)
    return session['csrf']


def check_csrf():
    token = request.form.get('csrf', '')
    if not token or not hmac.compare_digest(token.encode(), session.get('csrf', '').encode()):
        abort(403)


@inbox.before_request
def browser_security():
    if request.endpoint == 'inbox.inbound_sms':
        return None
    if not current_app.secret_key or len(current_app.secret_key) < 32:
        return Response('Account access is not configured.', status=503)
    if request.method == 'POST':
        check_csrf()


@inbox.after_request
def private_response(response):
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Content-Security-Policy'] = "default-src 'self'; style-src 'self' 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'"
    return response


def require_business(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        business = business_by_id(session.get('business_id'))
        if business is None:
            return redirect(url_for('inbox.login'))
        return view(business, *args, **kwargs)
    return wrapped


@inbox.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        with closing(get_connection()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            business = conn.execute('SELECT * FROM businesses WHERE login=?',
                                    (request.form.get('login', '').strip().lower(),)).fetchone()
            now = time.time()
            allowed = business and business['locked_until'] <= now
            if allowed and check_password_hash(business['password_hash'], request.form.get('password', '')):
                conn.execute('UPDATE businesses SET login_failures=0,locked_until=0 WHERE id=?', (business['id'],))
                session.clear()
                session['business_id'] = business['id']
                session.permanent = True
                return redirect(url_for('inbox.home'))
            if allowed:
                failures = business['login_failures'] + 1
                conn.execute('UPDATE businesses SET login_failures=?,locked_until=? WHERE id=?',
                             (0 if failures >= 5 else failures, now + 900 if failures >= 5 else 0, business['id']))
            error = 'Sign-in failed. Check your credentials or try again later.'
    return render_template('login.html', csrf=csrf_token(), error=error)


@inbox.post('/logout')
def logout():
    session.clear()
    return redirect(url_for('inbox.login'))


@inbox.get('/inbox')
@require_business
def home(business):
    with closing(get_connection()) as conn:
        leads = [dict(r) for r in conn.execute('''SELECT c.*,
            (SELECT body FROM messages m WHERE m.conversation_id=c.id ORDER BY m.id DESC LIMIT 1) latest,
            (SELECT COALESCE(call_event_at,time_received) FROM missed_calls mc
                WHERE mc.conversation_id=c.id ORDER BY mc.id DESC LIMIT 1) missed_at
            FROM conversations c WHERE business_id=? ORDER BY updated_at DESC,id DESC''', (business['id'],))]
        for lead in leads:
            lead['card'] = lead_card(conn, lead)
    for lead in leads:
        lead['time'] = display_timestamp(lead['missed_at'] or lead['created_at'], business['timezone'])['text']
    return render_template('inbox.html', business=business, leads=leads, csrf=csrf_token())


@inbox.route('/inbox/<int:conversation_id>', methods=['GET', 'POST'])
@require_business
def detail(business, conversation_id):
    lead = conversation(business['id'], conversation_id)
    if lead is None:
        abort(404)
    error = None
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'reply':
            try:
                reply(business['id'], conversation_id, request.form.get('body', ''), request.form.get('request_key', ''))
            except ValueError as exc:
                error = str(exc)
        elif action in {'read', 'close', 'reopen'}:
            with closing(get_connection()) as conn, conn:
                if action == 'read':
                    conn.execute('UPDATE conversations SET unread=0 WHERE business_id=? AND id=?', (business['id'], conversation_id))
                else:
                    if action == 'close':
                        pause_capture(conn, conversation_id)
                    conn.execute('UPDATE conversations SET status=? WHERE business_id=? AND id=?',
                                 ('closed' if action == 'close' else 'new', business['id'], conversation_id))
        else:
            abort(400)
        if not error:
            return redirect(url_for('inbox.detail', conversation_id=conversation_id))
    with closing(get_connection()) as conn:
        messages = [dict(r) for r in conn.execute('SELECT * FROM messages WHERE business_id=? AND conversation_id=? ORDER BY id',
                    (business['id'], conversation_id))]
        calls = [dict(r) for r in conn.execute('SELECT * FROM missed_calls WHERE business_id=? AND conversation_id=? ORDER BY id',
                 (business['id'], conversation_id))]
        card = lead_card(conn, lead)
    for message in messages:
        message['time'] = display_timestamp(message['created_at'], business['timezone'])['text']
    for call in calls:
        call['time'] = display_timestamp(call['call_event_at'] or call['time_received'], business['timezone'])['text']
    return render_template('conversation.html', business=business, lead=lead, messages=messages,
                           calls=calls, csrf=csrf_token(), request_key=secrets.token_hex(16),
                           error=error, errors=SAFE_ERRORS, card=card)


@inbox.post('/sms/incoming')
def inbound_sms():
    try:
        receive_sms(request.form)
    except LookupError:
        return Response('Unknown destination.', status=404)
    except ValueError:
        return Response('Invalid message.', status=400)
    return Response('<?xml version="1.0" encoding="UTF-8"?><Response/>', mimetype='text/xml')


def register_account_cli(app):
    @app.cli.command('create-business')
    @click.option('--name', required=True)
    @click.option('--login', required=True)
    @click.option('--public-phone', required=True)
    @click.option('--mca-phone', required=True)
    @click.option('--forwarding-phone', required=True)
    @click.option('--timezone', default='America/Phoenix')
    @click.password_option(confirmation_prompt=True)
    def create_business_command(**options):
        """Provision an operator-verified number assignment and private account."""
        if not app.secret_key or len(app.secret_key) < 32:
            raise click.ClickException('Set a stable SECRET_KEY of at least 32 characters first.')
        try:
            provision_business(**options)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from None
        click.echo('Business account created. No calls or messages were sent.')
