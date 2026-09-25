import os

from dotenv import load_dotenv
from flask import Flask

from database import init_db
from routes import production


def create_app(test_config=None):
    load_dotenv()
    app = Flask(__name__)
    app.config.from_mapping(
        BUSINESS_PHONE=os.getenv("BUSINESS_PHONE"),
        DATABASE_PATH=os.getenv("DATABASE_PATH", "missed_calls.db"),
        ENABLE_DEV_ROUTES=os.getenv("ENABLE_DEV_ROUTES", "").lower() == "true",
    )
    if test_config is not None:
        app.config.update(test_config)
    with app.app_context():
        init_db()
    app.register_blueprint(production)
    if app.config["ENABLE_DEV_ROUTES"]:
        from development import development
        app.register_blueprint(development)
    return app


# Keep Render's existing gunicorn app:app entry point.
app = create_app()


if __name__ == "__main__":
    app.run(port=5000)
