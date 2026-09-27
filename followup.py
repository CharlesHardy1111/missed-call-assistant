"""One follow-up workflow shared by production missed-call entry points."""
from flask import current_app

from database import (
    add_missed_call, get_call_by_external_id, claim_follow_up, finish_follow_up,
)
from sms import SendResult, send_sms


def record_missed_call(phone_number, caller_name="Unknown Caller",
                       call_status="no-answer", external_call_id=None,
                       allow_sms=True):
    """Return a new lead ID, or None for a retry of the same telephone event."""
    call_id = add_missed_call(phone_number, caller_name, call_status,
                              external_call_id, deduplicate=True)
    existing = get_call_by_external_id(external_call_id) if call_id is None else None
    target_id = call_id if call_id is not None else existing["id"]
    enabled = current_app.config.get("ENABLE_SMS_FOLLOWUP") is True
    skip_reason = None
    if not allow_sms:
        skip_reason = "development_simulator"
    elif not external_call_id:
        skip_reason = "missing_call_id"
    row = claim_follow_up(target_id, enabled, skip_reason)
    if row is not None:
        try:
            result = send_sms(row["phone_number"], row["follow_up_message"])
        except Exception:
            # Keep the voice response working even if a send adapter fails.
            result = SendResult("failed", error="unknown_outcome")
        finish_follow_up(target_id, result)
    return call_id
