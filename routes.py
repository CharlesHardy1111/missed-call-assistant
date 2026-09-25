from xml.sax.saxutils import escape

from flask import Blueprint, Response, current_app, jsonify, render_template, request

from database import add_missed_call, call_already_exists, get_calls
from events import MISSED_STATUSES, event_data

production = Blueprint("production", __name__)


@production.route("/")
def index():
    calls = get_calls()
    return render_template(
        "index.html",
        calls=calls
    )


@production.route("/call-event", methods=["POST"])
def call_event():

    data = event_data()

    phone_number = data.get("phone_number")

    caller_name = (
        data.get("caller_name")
        or "Unknown Caller"
    )

    call_status = str(data.get("call_status") or "").lower()

    external_call_id = data.get(
        "external_call_id"
    )

    if not phone_number:
        return jsonify({
            "success": False,
            "error": "phone_number is required"
        }), 400

    if not call_status:
        return jsonify({
            "success": False,
            "error": "call_status is required"
        }), 400

    # Call was answered.
    # It is not a missed lead.
    if call_status not in MISSED_STATUSES:

        return jsonify({
            "success": True,
            "recorded": False,
            "message": (
                "Call was not missed. "
                "No lead was created."
            )
        }), 200

    # Prevent the same telephone event
    # from creating duplicate leads.
    if (
        external_call_id
        and call_already_exists(external_call_id)
    ):

        return jsonify({
            "success": True,
            "recorded": False,
            "duplicate": True,
            "message": (
                "This call event was already recorded."
            )
        }), 200

    call_id = add_missed_call(
        phone_number=phone_number,
        caller_name=caller_name,
        call_status=call_status,
        external_call_id=external_call_id
    )

    return jsonify({
        "success": True,
        "recorded": True,
        "message": "Missed call lead created.",
        "call_id": call_id
    }), 201


@production.route("/provider/call-status", methods=["POST"])
def provider_call_status():

    phone_number = request.form.get("From")

    provider_status = (
        request.form.get("CallStatus") or ""
    ).lower()

    external_call_id = request.form.get("CallSid")

    status_map = {
        "completed": "answered",
        "in-progress": "answered"
    }

    call_status = status_map.get(
        provider_status,
        provider_status
    )

    if not phone_number:
        return jsonify({
            "success": False,
            "error": "Caller phone number missing."
        }), 400

    if call_status not in MISSED_STATUSES:
        return jsonify({
            "success": True,
            "recorded": False,
            "call_status": call_status,
            "message": "Call was not missed."
        }), 200

    if (
        external_call_id
        and call_already_exists(external_call_id)
    ):
        return jsonify({
            "success": True,
            "recorded": False,
            "duplicate": True,
            "message": "Call already recorded."
        }), 200

    call_id = add_missed_call(
        phone_number=phone_number,
        caller_name="Incoming Caller",
        call_status=call_status,
        external_call_id=external_call_id
    )

    return jsonify({
        "success": True,
        "recorded": True,
        "call_id": call_id,
        "call_status": call_status,
        "message": "Provider missed call recorded."
    }), 201


@production.route("/voice/incoming", methods=["POST"])
def voice_incoming():

    if not current_app.config["BUSINESS_PHONE"]:
        return Response("Business phone is not configured.", status=503)

    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Dial action="/voice/dial-result" method="POST" timeout="20">
        <Number>{escape(current_app.config["BUSINESS_PHONE"] or "")}</Number>
    </Dial>
</Response>"""

    return Response(
        twiml,
        mimetype="text/xml"
    )


@production.route("/voice/dial-result", methods=["POST"])
def voice_dial_result():

    phone_number = request.form.get("From")

    dial_status = (
        request.form.get("DialCallStatus") or ""
    ).lower()

    call_sid = request.form.get("CallSid")


    if dial_status in MISSED_STATUSES and not phone_number:
        return Response("Caller phone number missing.", status=400)

    if dial_status in MISSED_STATUSES:

        if not call_already_exists(call_sid):

            add_missed_call(
                phone_number=phone_number,
                caller_name="Incoming Caller",
                call_status=dial_status,
                external_call_id=call_sid
            )

        twiml = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say>
        Sorry we missed your call.
        We will follow up with you shortly.
    </Say>
</Response>"""

    else:

        twiml = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
</Response>"""

    return Response(
        twiml,
        mimetype="text/xml"
    )


@production.route("/privacy")
def privacy():
    return render_template("privacy.html")


@production.route("/terms")
def terms():
    return render_template("terms.html")
