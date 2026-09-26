"""Sign existing behavior tests without bypassing application authentication."""
from flask.testing import FlaskClient
from twilio.request_validator import RequestValidator
from werkzeug.datastructures import MultiDict
from urllib.parse import urlsplit

TEST_ORIGIN = "https://webhooks.example.test"
TEST_TOKEN = "dummy-webhook-test-token"
TEST_EVENT_TOKEN = "dummy-event-test-token"


class SignedClient(FlaskClient):
    def open(self, *args, **kwargs):
        path = args[0] if args and isinstance(args[0], str) else kwargs.get("path", "")
        parsed = urlsplit(path)
        headers = dict(kwargs.get("headers") or {})
        if parsed.path == "/call-event":
            headers.setdefault("Authorization", "Bearer " + self.application.config["CALL_EVENT_TOKEN"])
        elif parsed.path in {"/voice/incoming", "/voice/dial-result", "/provider/call-status", "/voice/trial-test"}:
            data = MultiDict(kwargs.get("data") or {})
            url = self.application.config["TWILIO_WEBHOOK_BASE_URL"].rstrip("/") + path
            headers.setdefault("X-Twilio-Signature", RequestValidator(
                self.application.config["TWILIO_AUTH_TOKEN"]
            ).compute_signature(url, data))
            kwargs.setdefault("content_type", "application/x-www-form-urlencoded")
        kwargs["headers"] = headers
        return super().open(*args, **kwargs)


def signed_app(factory, config):
    app = factory({"TWILIO_WEBHOOK_BASE_URL": TEST_ORIGIN,
                   "TWILIO_AUTH_TOKEN": TEST_TOKEN,
                   "CALL_EVENT_TOKEN": TEST_EVENT_TOKEN, **config})
    app.test_client_class = SignedClient
    return app
