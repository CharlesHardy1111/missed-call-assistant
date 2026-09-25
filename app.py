import os

from dotenv import load_dotenv
from flask import Flask, render_template, request, jsonify, Response

from database import (
    init_db,
    add_missed_call,
    get_calls,
    call_already_exists
)

load_dotenv()

BUSINESS_PHONE = os.getenv("BUSINESS_PHONE")

app = Flask(__name__)

init_db()


MISSED_STATUSES = {
    "no-answer",
    "busy",
    "failed",
    "canceled"
}


@app.route("/")
def index():
    calls = get_calls()
    return render_template(
        "index.html",
        calls=calls
    )


# -----------------------------------
# STAGE 1 SIMULATOR
# -----------------------------------

@app.route("/missed-call", methods=["POST"])
def missed_call():

    data = request.get_json(silent=True) or request.form

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

    call_id = add_missed_call(
        phone_number=phone_number,
        caller_name=caller_name,
        call_status="no-answer"
    )

    return jsonify({
        "success": True,
        "message": "Missed call recorded successfully.",
        "call_id": call_id
    }), 201


# -----------------------------------
# STAGE 2 REAL CALL EVENT ENDPOINT
# -----------------------------------

@app.route("/call-event", methods=["POST"])
def call_event():

    data = request.get_json(silent=True) or request.form

    phone_number = data.get("phone_number")

    caller_name = (
        data.get("caller_name")
        or "Unknown Caller"
    )

    call_status = (
        data.get("call_status")
        or ""
    ).lower()

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

# -----------------------------------
# STAGE 3 - PHONE PROVIDER WEBHOOK
# -----------------------------------

@app.route("/provider/call-status", methods=["POST"])
def provider_call_status():

    phone_number = request.form.get("From")

    provider_status = (
        request.form.get("CallStatus") or ""
    ).lower()

    external_call_id = request.form.get("CallSid")

    status_map = {
        "no-answer": "no-answer",
        "busy": "busy",
        "failed": "failed",
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
@app.route("/voice/incoming", methods=["POST"])
def voice_incoming():

    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Dial action="/voice/dial-result" method="POST" timeout="20">
        <Number>{BUSINESS_PHONE}</Number>
    </Dial>
</Response>"""

    return Response(
        twiml,
        mimetype="text/xml"
    )


@app.route("/voice/dial-result", methods=["POST"])
def voice_dial_result():

    phone_number = request.form.get("From")

    dial_status = (
        request.form.get("DialCallStatus") or ""
    ).lower()

    call_sid = request.form.get("CallSid")

    print("CALLER:", phone_number)
    print("DIAL STATUS:", dial_status)
    print("CALL SID:", call_sid)

    missed_statuses = {
        "no-answer",
        "busy",
        "failed",
        "canceled"
    }

    if dial_status in missed_statuses:

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

@app.route("/voice/trial-test", methods=["POST"])
def voice_trial_test():

    phone_number = request.form.get("From")
    call_sid = request.form.get("CallSid")

    print("REAL TWILIO CALL RECEIVED")
    print("FROM:", phone_number)
    print("CALL SID:", call_sid)

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

@app.route("/privacy")
def privacy():
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Privacy Policy - Missed Call Assistant</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
    </head>

    <body style="font-family: Arial, sans-serif; max-width: 800px; margin: 40px auto; padding: 0 20px; line-height: 1.6;">

        <h1>Privacy Policy</h1>

        <p><strong>Missed Call Assistant</strong><br>
        Operated by Charles Hardy<br>
        Effective Date: September 25, 2026</p>

        <p>
        Missed Call Assistant provides text-message communications related
        to customer inquiries and missed calls.
        </p>

        <h2>Information We Collect</h2>

        <p>
        We may collect information that you provide when interacting with
        the service, including your name, mobile phone number, call information,
        message content, and other information you voluntarily provide during
        a customer-service interaction.
        </p>

        <h2>How We Use Information</h2>

        <p>
        Information collected through Missed Call Assistant is used to respond
        to customer inquiries, provide requested customer-service communications,
        facilitate missed-call follow-up, maintain service records, and operate
        and improve the service.
        </p>

        <h2>SMS Communications</h2>

        <p>
        Users who consent to SMS communications may receive customer-service
        and missed-call follow-up messages. Message frequency varies depending
        on the user's interactions with the business. Message and data rates
        may apply.
        </p>

        <p>
        Users may reply STOP to opt out of SMS communications and HELP for
        assistance.
        </p>

        <h2>Sharing of Mobile Information</h2>

        <p>
        We do not sell or share SMS opt-in data or personal information with
        third parties for marketing purposes.
        </p>

        <p>
        Mobile phone numbers, SMS consent records, and opt-in information will
        not be shared with third parties or affiliates for their marketing or
        promotional purposes.
        </p>

        <h2>Service Providers</h2>

        <p>
        Information may be processed by service providers used to operate the
        communications service, such as telecommunications, hosting, and
        infrastructure providers, solely as necessary to provide and maintain
        the service.
        </p>

        <h2>Data Security</h2>

        <p>
        Reasonable administrative and technical measures are used to protect
        information processed through the service. No method of electronic
        storage or transmission can be guaranteed to be completely secure.
        </p>

        <h2>Your Choices</h2>

        <p>
        You may opt out of SMS communications at any time by replying STOP.
        You may reply HELP for assistance.
        </p>

        <h2>Contact</h2>

        <p>
        Charles Hardy<br>
        Email: Charlesphardy56@gmail.com
        </p>

    </body>
    </html>
    """


@app.route("/terms")
def terms():
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Terms & Conditions - Missed Call Assistant</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
    </head>

    <body style="font-family: Arial, sans-serif; max-width: 800px; margin: 40px auto; padding: 0 20px; line-height: 1.6;">

        <h1>Terms & Conditions</h1>

        <p><strong>Missed Call Assistant</strong><br>
        Operated by Charles Hardy<br>
        Effective Date: September 25, 2026</p>

        <h2>Program Description</h2>

        <p>
        Missed Call Assistant provides SMS communications related to customer
        inquiries and missed calls. Users who have provided consent may receive
        text messages to facilitate customer-service follow-up after contacting
        a participating business.
        </p>

        <h2>Consent to Receive Messages</h2>

        <p>
        By expressly opting in to SMS communications, you consent to receive
        customer-service and missed-call follow-up text messages associated
        with your inquiry. Consent is voluntary and is not a condition of
        purchasing goods or services.
        </p>

        <h2>Message Frequency</h2>

        <p>
        Message frequency varies depending on your interactions with the
        business and the nature of your inquiry.
        </p>

        <h2>Message and Data Rates</h2>

        <p>
        Message and data rates may apply according to your mobile carrier
        and service plan.
        </p>

        <h2>Opting Out</h2>

        <p>
        Reply STOP to stop receiving SMS messages. After opting out, you will
        no longer receive SMS messages from the program unless you subsequently
        provide consent again.
        </p>

        <h2>Help</h2>

        <p>
        Reply HELP for assistance.
        </p>

        <h2>Carrier Disclaimer</h2>

        <p>
        Carriers are not liable for delayed or undelivered messages.
        </p>

        <h2>Privacy</h2>

        <p>
        Information collected through this SMS program is handled according
        to our Privacy Policy.
        </p>

        <p>
        <a href="/privacy">View Privacy Policy</a>
        </p>

        <h2>Changes to These Terms</h2>

        <p>
        These Terms & Conditions may be updated from time to time to reflect
        changes to the service, applicable requirements, or messaging practices.
        </p>

        <h2>Contact</h2>

        <p>
        Charles Hardy<br>
        Email: Charlesphardy56@gmail.com
        </p>

    </body>
    </html>
    """

if __name__ == "__main__":
    app.run(
        debug=True,
        port=5000
    )