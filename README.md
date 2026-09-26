# Missed Call Assistant

Flask dashboard and inbound Twilio voice callbacks backed by SQLite. Missed calls
create a lead and run an SMS follow-up workflow. **Real SMS is disabled by default**
and must not be enabled without explicit approval.

## Run locally

Create a Python virtual environment and install `pip install -r requirements.txt`.
Copy `.env.example` to `.env` and set `BUSINESS_PHONE` to the forwarding number.
Run `python app.py` (port 5000, debugger disabled), or `flask --app app run --debug`
when intentionally debugging locally.

Set `ENABLE_DEV_ROUTES=true` only for local development to enable the dashboard
simulator, `POST /missed-call`, and `POST /voice/trial-test`. They retain their
existing URLs and responses but return 404 by default. Restart after config changes.

## Production and Render compatibility

Keep the existing Render build (`pip install -r requirements.txt`) and start
command (`gunicorn app:app`, including any existing arguments). No Render service
settings are changed by this repository cleanup. Keep `BUSINESS_PHONE` in Render's
environment and leave `ENABLE_DEV_ROUTES` unset or false.

Production routes remain:

| Route | Purpose |
| --- | --- |
| `GET /` | Existing missed-call dashboard |
| `GET /privacy`, `GET /terms` | Existing policy content, now in templates |
| `POST /call-event` | Generic JSON/form call-event integration |
| `POST /provider/call-status` | Provider form status callback |
| `POST /voice/incoming` | TwiML forwarding to `BUSINESS_PHONE` |
| `POST /voice/dial-result` | TwiML dial outcome and missed-call recording |

Twilio should continue posting incoming voice calls to `/voice/incoming`. The
`Dial` action still posts to `/voice/dial-result`, with a 20-second timeout.
The nested `Number` also posts its terminal event to `/voice/dial-result` using
`statusCallbackEvent="completed"`. That event includes unsuccessful final statuses
such as `no-answer`; it does not mean the forwarded call was answered.
The action sends `DialCallStatus` with the parent `CallSid`; the number callback
sends `CallStatus` with `ParentCallSid`. Both record against the parent ID in a
serialized SQLite transaction, so either callback can record the lead without
duplicating it when the other callback or a retry arrives. Number status callbacks
return empty TwiML; the action retains the existing spoken apology. No URL query
parameters contain caller details, and logs contain only callback source, missed
status, and recorded/duplicate outcome.
`no-answer`, `busy`, `failed`, and `canceled` create missed-call records; answered
and other statuses do not. Missed dial outcomes retain the existing spoken apology.
Repeated external call IDs are ignored. Missing IDs retain the existing behavior
of allowing separate records, but those records cannot send SMS without a stable
call ID. A missing forwarding number returns 503 instead
of emitting a `None` destination; a missed dial callback missing `From` returns 400
instead of failing a database constraint.

SQLite still defaults to `missed_calls.db` in the working directory; existing
tables and the additive legacy migration are preserved. `DATABASE_PATH` optionally
selects a different file, including a provisioned persistent disk location. Its
parent directory must exist. Back up existing data before changing the path.

## SMS follow-up milestone

This branch starts at `d9c2f13`, the merged 20-second timeout repair identified as
deployed. The `/voice/incoming` function, Dial timeout, forwarding destination,
business-number environment configuration, and Render entry point are unchanged.

All production missed-call entry points use one workflow. The two Twilio dial
callbacks and provider callback use the parent call ID; generic events use
`external_call_id`. The existing missed-call row is the lead: one row per identified
call, not one row per phone number. A distinct future missed call can create a new
lead. A retry reuses the existing row and never repeats a completed send attempt.

The default message is:

> Hi, sorry we missed your call. We'll get back to you shortly. How can we help?

| Environment variable | Default / purpose |
| --- | --- |
| `ENABLE_SMS_FOLLOWUP` | `false`; only explicit `true` enables the transport |
| `TWILIO_ACCOUNT_SID` | Empty; Twilio account SID, supplied through secret configuration |
| `TWILIO_AUTH_TOKEN` | Empty; Twilio auth token, supplied through secret configuration |
| `TWILIO_SMS_FROM` | Empty; SMS-capable Twilio sender in E.164 format |

`TWILIO_SMS_FROM` is separate from `BUSINESS_PHONE`; do not change the latter.
No credentials are needed for the automated or disabled-mode manual tests.
The simulator never sends, even if the feature flag is true. Production events
without a stable ID are also not sent, because retries cannot be deduplicated safely.

| Stored status | Dashboard meaning |
| --- | --- |
| `disabled` | Not attempted / disabled |
| `not_attempted` | Legacy row, simulator, or missing call ID; no send |
| `pending` | New row not yet claimed by the workflow |
| `sending` | Attempt claimed; outcome not yet confirmed |
| `sent` | Twilio accepted the send request; delivery unconfirmed |
| `failed` | Request rejected, configuration invalid, or outcome uncertain |

The SMS REST request uses Python's standard library, Basic authentication over
HTTPS, a five-second socket timeout, no automatic retries, and no redirects.
The workflow stores the provider message SID/status, UTC attempt/completion times,
a fixed safe error category, and an optional numeric provider error code. Raw
provider errors, credentials, and request bodies are never logged or stored.
`sent` measures successful submission in this milestone, not carrier delivery.
Twilio normally returns `queued` initially. Delivery receipts and asynchronous
delivery failures are not ingested yet. See the [Twilio Message resource](https://www.twilio.com/docs/messaging/api/message-resource).

SQLite serializes both lead creation and the follow-up claim. The claim commits
before network I/O; no database lock is held during the send. Disabled, failed,
sent, and already-claimed rows are never automatically retried. If a process stops
after claiming or Twilio accepts a request but the response is lost, the outcome
may need manual reconciliation in Twilio. Do not reset a `sending`/uncertain record
to pending without checking whether Twilio already accepted it. This favors avoiding
duplicate texts over automatic recovery; it is not an exactly-once delivery guarantee.
An interrupted pending row can resume on a webhook retry before it has been claimed.

Migration adds nullable lifecycle columns, preserves existing records, and marks
legacy pending rows `not_attempted` to prevent a historical SMS backlog. It updates
only the old default unsent message wording; historical sent/custom text is preserved.
Enabling the flag later does not send previously disabled records.

The send currently runs inside the callback request with a bounded socket timeout.
Transport failures are recorded and the existing voice TwiML response is retained.
A database failure can still cause a webhook error; a committed send claim prevents
that error's retry from producing another text. A durable worker queue and delivery
reconciliation are follow-up work, not part of this milestone.

## Safe local verification (no real SMS)

From the repository in PowerShell:

```powershell
$env:ENABLE_SMS_FOLLOWUP = 'false'
& .\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

The suite uses dummy credentials and mocked sends; tests of the enabled path also
block the real transport. It exercises sent and failed dashboard states offline.

To inspect the disabled state manually, start a local server with a separate file:

```powershell
$env:ENABLE_SMS_FOLLOWUP = 'false'
$env:DATABASE_PATH = 'missed_calls_sms_test.db'
& .\.venv\Scripts\python.exe app.py
```

In a second PowerShell terminal, simulate the existing dial-result callback:

```powershell
Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:5000/voice/dial-result' -Body @{
    From = '+15555550101'
    CallSid = 'safe-local-demo-001'
    DialCallStatus = 'no-answer'
}
```

Open `http://127.0.0.1:5000/`: expect one lead, the new message, and
**Not attempted / disabled**. Repeat the identical POST: still one lead and no SMS.
Use a new dummy CallSid for a separate simulated call. Stop the server with Ctrl+C;
these shell variables do not edit `.env` or Render settings.

Before an explicitly approved real-send test, supply the three Twilio variables
privately, verify the sender has SMS capability and the account permits the intended
recipient/destination (trial accounts require a verified recipient), and confirm
the sender's required messaging registration/opt-out setup. No voice webhook changes
are required. Resolve webhook authentication before enabling sending on public
endpoints. Keep `ENABLE_SMS_FOLLOWUP=false` until approval; no real-send validation
or production deployment has been performed by this milestone.

## Tests

Run `python -m unittest discover -s tests -v`. Tests use temporary SQLite files,
including during app import, and make no Twilio requests. They cover missed and
answered statuses, sequential duplicate callbacks across endpoints, validation,
TwiML forwarding and responses, development route isolation, templates, and
non-destructive legacy schema migration. They also follow the generated callback
URLs through to the dashboard, test number-only reporting, both callback orders,
concurrent voice callbacks, and privacy-safe logging. Follow-up tests cover the
safety switch, mocked sends, rejection/timeouts, retries/concurrency, restart/crash
behavior, migration, and dashboard statuses. GitHub Actions runs the same suite.

## Diagnosing missing dial results

The original code already specified a valid relative `Dial action` URL and handled
`DialCallStatus=no-answer`. The identified reporting gap was no independent
`Number statusCallback` subscription and no handling of its `CallStatus` /
`ParentCallSid` payload. A no-answer child leg in Twilio's console alone does not
prove that a callback reached this app. This fix closes that gap; the cause of a
particular live callback failure still needs Twilio request/response evidence.

After deploying, verify that the Twilio number's incoming POST reaches this app's
`/voice/incoming` and the returned TwiML includes both callbacks. Leave a test
forwarded call unanswered, check the child status callback and action HTTP results,
then confirm one lead appears. If callbacks return 404, check the deployed revision
and webhook target; for 5xx check application/database errors. Successful callbacks
with an empty dashboard require checking that both use the same persistent database.
Forwarding through a separate TwiML Bin or Studio flow will bypass this code.
Voicemail answering the forwarded leg is a completed call, not a no-answer result.

Protocol references: [Twilio Dial action](https://www.twilio.com/docs/voice/twiml/dial#action)
and [Number status callbacks](https://www.twilio.com/docs/voice/twiml/number#statuscallbackevent).

## Audit findings and remaining risks

- The dashboard still has no authentication and displays caller information.
  Access control needs a separate rollout before exposing sensitive call data.
- Provider/voice webhooks still lack Twilio signature validation, and `/call-event`
  has no authentication. Authentication requires a coordinated configuration
  change to avoid interrupting existing callers; it is not silently enabled here.
- All production entry points now use the serialized duplicate check and follow-up
  claim. Direct database writers can bypass this contract; existing duplicates are
  not deleted. Events without IDs preserve record creation but cannot send SMS.
- SQLite durability depends on the deployed disk. This change does not provision
  storage, migrate to PostgreSQL, or verify the live Render configuration.
- Existing policy text describes STOP/HELP capabilities. Inbound SMS/HELP handling
  is not implemented by this milestone; confirm opt-out behavior and policy wording
  before launching messaging.
- Caller phone/SID debug prints were removed. `.env` variants, virtual environments,
  databases and SQLite sidecars are ignored. `.env.example` contains a dummy number.
  If credentials were ever exposed elsewhere, ignoring files does not rotate them.
- Removed unused SQLAlchemy/PostgreSQL, Stripe, QR/image, HTTP/async and JWT
  dependencies. Retained Flask, dotenv, Gunicorn and their pinned dependency
  versions from the original requirements to avoid unrelated upgrades.
- Live Render/Gunicorn and real Twilio calls still need a deployment smoke test.
  The local regression suite verifies the Flask entry point and callback contracts.
