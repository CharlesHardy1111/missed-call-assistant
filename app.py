import os

from dotenv import load_dotenv
from flask import Flask

from database import init_db
from routes import production
from webhook_security import validate_webhooks


def create_app(test_config=None):
    load_dotenv()
    app = Flask(__name__)
    app.config.from_mapping(
        BUSINESS_PHONE=os.getenv("BUSINESS_PHONE"),
        DATABASE_PATH=os.getenv("DATABASE_PATH", "missed_calls.db"),
        ENABLE_DEV_ROUTES=os.getenv("ENABLE_DEV_ROUTES", "").lower() == "true",
        ENABLE_SMS_FOLLOWUP=os.getenv("ENABLE_SMS_FOLLOWUP", "false").strip().lower() == "true",
        TWILIO_ACCOUNT_SID=os.getenv("TWILIO_ACCOUNT_SID", ""),
        TWILIO_AUTH_TOKEN=os.getenv("TWILIO_AUTH_TOKEN", ""),
        TWILIO_SMS_FROM=os.getenv("TWILIO_SMS_FROM", ""),
        TWILIO_WEBHOOK_BASE_URL=os.getenv("TWILIO_WEBHOOK_BASE_URL", ""),
        CALL_EVENT_TOKEN=os.getenv("CALL_EVENT_TOKEN", ""),
    )
    if test_config is not None:
        app.config.update(test_config)
    with app.app_context():
        init_db()
    app.register_blueprint(production)
    app.before_request(validate_webhooks)
    if app.config["ENABLE_DEV_ROUTES"]:
        from development import development
        app.register_blueprint(development)
    return app


# Keep Render's existing gunicorn app:app entry point.
app = create_app()


if __name__ == "__main__":
    app.run(port=5000)
