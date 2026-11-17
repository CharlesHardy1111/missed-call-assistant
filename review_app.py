"""Public synthetic review only. No production DB, secrets, logins or webhooks.

Separate Render start command: gunicorn review_app:app. Never change app:app.
"""
from flask import Flask, abort, jsonify, render_template
from agent_demo import SCENARIOS, build_demo


def create_review_app():
    app = Flask(__name__)
    app.config.update(ENABLE_SMS_FOLLOWUP=False, ENABLE_PROGRESSIVE_CAPTURE=False)
    # Materialize fixed synthetic examples once, then discard their temporary DBs.
    snapshots = {name: build_demo(name, voice=True) for name in SCENARIOS}

    @app.after_request
    def headers(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Content-Security-Policy'] = "default-src 'self'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'"
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Robots-Tag'] = 'noindex, nofollow'
        return response

    @app.get('/')
    @app.get('/scenario/<name>')
    def review(name='complete'):
        if name not in snapshots:
            abort(404)
        return render_template('review.html', demo=snapshots[name], card=snapshots[name]['card'],
                               scenarios=SCENARIOS, selected=name, business=None)

    @app.get('/review.json')
    def data():
        return jsonify(mode='synthetic-review', sms_enabled=False, live_ai=False, scenarios=snapshots)

    @app.get('/health')
    def health():
        return jsonify(status='ok', mode='synthetic-review')

    return app


app = create_review_app()
