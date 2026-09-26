"""Bounded, non-retrying Twilio SMS transport. Never log provider responses."""
import base64
from dataclasses import dataclass
import json
import re
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from flask import current_app

DEFAULT_MESSAGE = "Hi, sorry we missed your call. We'll get back to you shortly. How can we help?"
SAFE_ERRORS = {
    "configuration_missing": "SMS credentials or sender are missing or invalid.",
    "invalid_recipient": "The caller number is not a valid SMS destination.",
    "provider_rejected": "Twilio rejected the send request. Check the Twilio console.",
    "unknown_outcome": "Send outcome is uncertain. Check Twilio before any manual retry.",
    "missing_call_id": "Not attempted: no stable call ID was supplied.",
    "development_simulator": "Not attempted: development simulator.",
}


@dataclass(frozen=True)
class SendResult:
    status: str
    message_sid: str | None = None
    provider_status: str | None = None
    error: str | None = None
    error_code: str | None = None


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward Authorization to a redirect target.
        return None


def _open(request):
    return build_opener(_NoRedirects()).open(request, timeout=5)


def _error_code(value):
    value = str(value or "")
    return value if re.fullmatch(r"[0-9]{4,6}", value) else None


def send_sms(phone_number, message):
    """'sent' means Twilio accepted the API request, not handset delivery."""
    config = current_app.config
    # Defense in depth: even a direct transport invocation obeys the switch.
    if config.get("ENABLE_SMS_FOLLOWUP") is not True:
        return SendResult("disabled")
    sid = config.get("TWILIO_ACCOUNT_SID") or ""
    token = config.get("TWILIO_AUTH_TOKEN") or ""
    sender = config.get("TWILIO_SMS_FROM") or ""
    if not (re.fullmatch(r"AC[0-9a-fA-F]{32}", sid) and token
            and re.fullmatch(r"\+[1-9][0-9]{7,14}", sender)):
        return SendResult("failed", error="configuration_missing")
    if not isinstance(phone_number, str) or not re.fullmatch(r"\+[1-9][0-9]{7,14}", phone_number):
        return SendResult("failed", error="invalid_recipient")

    credentials = base64.b64encode(f"{sid}:{token}".encode()).decode("ascii")
    request = Request(
        f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
        data=urlencode({"From": sender, "To": phone_number, "Body": message}).encode(),
        headers={"Authorization": f"Basic {credentials}",
                 "Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with _open(request) as response:
            data = json.loads(response.read(65536))
        message_sid = data.get("sid", "")
        status = data.get("status")
        if not isinstance(message_sid, str) or not re.fullmatch(r"SM[0-9a-fA-F]{32}", message_sid):
            return SendResult("failed", error="unknown_outcome")
        if status in {"failed", "undelivered", "canceled"}:
            return SendResult("failed", message_sid, status, "provider_rejected", _error_code(data.get("error_code")))
        if status not in {"accepted", "queued", "sending", "sent", "delivered"}:
            return SendResult("failed", message_sid, error="unknown_outcome")
        return SendResult("sent", message_sid, status)
    except HTTPError as exc:
        try:
            data = json.loads(exc.read(65536))
            code = _error_code(data.get("code"))
        except Exception:
            code = None
        finally:
            exc.close()
        # A server failure could occur after acceptance. Never retry automatically.
        error = "unknown_outcome" if exc.code >= 500 else "provider_rejected"
        return SendResult("failed", error=error, error_code=code)
    except Exception:
        # Timeouts, malformed responses, and transport exceptions may include
        # credentials or phone numbers in their text. Persist only a fixed code.
        return SendResult("failed", error="unknown_outcome")
