"""Optional local simulator and Twilio trial diagnostic routes."""
from flask import Blueprint, Response, jsonify

from followup import record_missed_call
from events import event_data

development = Blueprint("development", __name__)


@development.route("/missed-call", methods=["POST"])
def missed_call():

    data = event_data()

    phone_number = data.get("phone_number")
    caller_name = (
        data.get("caller_name")
        or "Unknown Caller"
    )

    if not phone_number:
        return jsonify({
            "success": False,
            "error": "phone_number is required"
        }), 400

    call_id = record_missed_call(
        phone_number=phone_number,
        caller_name=caller_name,
        call_status="no-answer",
        allow_sms=False,
    )

    return jsonify({
        "success": True,
        "message": "Missed call recorded successfully.",
        "call_id": call_id
    }), 201


@development.route("/voice/trial-test", methods=["POST"])
def voice_trial_test():
    twiml = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say>
        Success. Your missed call application received this real Twilio call.
    </Say>
</Response>"""

    return Response(
        twiml,
        mimetype="text/xml"
    )
