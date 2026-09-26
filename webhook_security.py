"""Fail-closed authentication before webhook handlers perform any work."""
import hmac
import re
from urllib.parse import urlsplit

from flask import Response, current_app, request
from twilio.request_validator import RequestValidator

TWILIO_ENDPOINTS = frozenset({
    "production.voice_incoming",
    "production.voice_dial_result",
    "production.provider_call_status",
    "development.voice_trial_test",
})


def _public_origin(value):
    """Use configured origin, never client-supplied Host/Forwarded headers."""
    if not isinstance(value, str) or not value or value != value.strip():
        return None
    parsed = urlsplit(value)
    if (not parsed.hostname or parsed.username is not None or parsed.password is not None
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
        return None
    if parsed.scheme != "https" and not (
        parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    ):
        return None
    # Force validation of malformed ports; reject whitespace in the authority.
    parsed.port
    if any(c.isspace() for c in parsed.netloc):
        return None
    return value.rstrip("/")


def validate_webhooks():
    if request.endpoint == "production.call_event":
        token = current_app.config.get("CALL_EVENT_TOKEN")
        if not token:
            return Response("Webhook authentication is not configured.", status=503)
        supplied = request.headers.get("Authorization", "")
        if not hmac.compare_digest(supplied.encode(), ("Bearer " + token).encode()):
            return Response("Forbidden.", status=403)
        return None

    if request.endpoint not in TWILIO_ENDPOINTS:
        return None
    token = current_app.config.get("TWILIO_AUTH_TOKEN")
    try:
        origin = _public_origin(current_app.config.get("TWILIO_WEBHOOK_BASE_URL"))
    except ValueError:
        origin = None
    if not token or not origin:
        return Response("Webhook authentication is not configured.", status=503)
    signature = request.headers.get("X-Twilio-Signature", "")
    if not re.fullmatch(r"[A-Za-z0-9+/]{27}=", signature):
        return Response("Forbidden.", status=403)
    # These voice callbacks use form POSTs. Never validate JSON as an empty form.
    if request.mimetype != "application/x-www-form-urlencoded":
        return Response("Expected form-encoded webhook.", status=415)
    try:
        url = origin + request.path
        if request.query_string:
            url += "?" + request.query_string.decode("ascii")
        valid = RequestValidator(token).validate(url, request.form, signature)
    except (ValueError, TypeError, UnicodeError):
        valid = False
    if not valid:
        return Response("Forbidden.", status=403)
    return None
