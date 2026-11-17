"""Run an offline conversation against a disposable DB, never the deployed DB."""
import json
import argparse
from pathlib import Path
import tempfile

from flask import Flask
from agent_engine import AgentEngine, OfflineInterpreter, configure_business, init_agent_store
from businesses import provision_business
from database import init_db
from database import get_connection
from lead_capture import lead_card
from contextlib import closing

SCENARIOS = {
    'complete': ('Complete intake', ['My kitchen sink is clogged', 'Phoenix', 'today', 'yes']),
    'partial': ('Caller leaves early', ['My kitchen sink is clogged']),
    'interrupted': ('Caller interrupts', ['My kitchen sink is clogged']),
    'booking': ('Booking is unavailable', ['I would like to book an appointment']),
}


def build_demo(scenario='complete', voice=False):
    if scenario not in SCENARIOS:
        raise ValueError('Unknown demonstration.')
    # Deliberately do not import app.py, load .env, or read provider credentials.
    with tempfile.TemporaryDirectory(prefix='mca-agent-demo-') as directory:
        app = Flask(__name__)
        app.config['DATABASE_PATH'] = str(Path(directory) / 'demo.db')
        with app.app_context():
            init_db()
            business = provision_business('Demo Business', 'demo', 'offline-demo-password',
                                          '+15550000001', '+15550000002', '+15550000003')
            init_agent_store()
            configure_business(business, questions=('service', 'location', 'urgency', 'callback'))
            engine = AgentEngine(business, OfflineInterpreter())
            interaction = engine.start('offline-demo', '+15551111111')
            relay = None
            if voice:
                import time
                from voice_relay import CallBinding, RelaySession, init_relay_store
                init_relay_store()
                binding = CallBinding('CA' + '1' * 32, 'AC' + '2' * 32,
                                      '+15551111111', '+15550000002', time.monotonic() + 30)
                relay = RelaySession(engine, interaction, binding)
                relay.receive(json.dumps(dict(type='setup', sessionId='VX' + '3' * 32,
                                              callSid=binding.call_sid, accountSid=binding.account_sid,
                                              **{'from': binding.caller, 'to': binding.destination})))
            timeline = []
            for index, text in enumerate(SCENARIOS[scenario][1]):
                if relay:
                    frames = relay.receive(json.dumps(dict(type='prompt', voicePrompt=text, last=True)))
                    response = next((f['token'] for f in frames if f['type'] == 'text'), None)
                    if response is None:
                        response = 'Human review requested. No transfer or booking was performed.'
                else:
                    response = engine.handle(interaction, str(index), text)['reply']
                timeline.append({'customer': text, 'assistant': response})
            if scenario in {'partial', 'interrupted'}:
                if relay and scenario == 'interrupted':
                    relay.receive(json.dumps(dict(type='interrupt', utteranceUntilInterrupt='Where', durationUntilInterruptMs=100)))
                elif relay:
                    relay.disconnect()
                else:
                    engine.handle(interaction, 'end', kind='disconnected')
            outcome = engine.outcome(interaction)
            with closing(get_connection()) as db:
                row = db.execute('SELECT * FROM conversations WHERE id=?', (outcome['conversation_id'],)).fetchone()
                card = lead_card(db, row)
                card.update(source='simulated voice', next_action=outcome['next_action'])
                return {'title': SCENARIOS[scenario][0], 'timeline': timeline, 'outcome': outcome,
                        'card': card, 'owner_unread': bool(row['unread'])}


def main(voice=False):
    demo = build_demo(voice=voice)
    for turn in demo['timeline']:
        print('Customer:', turn['customer'])
        print('MCA:', turn['assistant'])
    print(json.dumps(demo['outcome'], indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--voice', action='store_true', help='Simulate ConversationRelay JSON frames offline')
    main(parser.parse_args().voice)
