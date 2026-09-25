# Missed Call Assistant

Flask dashboard and inbound Twilio voice callbacks backed by SQLite. Missed calls
create pending follow-up records with draft text. **No outbound SMS is sent.**

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
`no-answer`, `busy`, `failed`, and `canceled` create missed-call records; answered
and other statuses do not. Missed dial outcomes retain the existing spoken apology.
Repeated external call IDs are ignored. Missing IDs retain the existing behavior
of allowing separate records. A missing forwarding number now returns 503 instead
of emitting a `None` destination; a missed dial callback missing `From` returns 400
instead of failing a database constraint.

SQLite still defaults to `missed_calls.db` in the working directory; existing
tables and the additive legacy migration are preserved. `DATABASE_PATH` optionally
selects a different file, including a provisioned persistent disk location. Its
parent directory must exist. Back up existing data before changing the path.

## Tests

Run `python -m unittest discover -s tests -v`. Tests use temporary SQLite files,
including during app import, and make no Twilio requests. They cover missed and
answered statuses, sequential duplicate callbacks across endpoints, validation,
TwiML forwarding and responses, development route isolation, templates, and
non-destructive legacy schema migration. GitHub Actions runs the same suite.

## Audit findings and remaining risks

- The dashboard still has no authentication and displays caller information.
  Access control needs a separate rollout before exposing sensitive call data.
- Provider/voice webhooks still lack Twilio signature validation, and `/call-event`
  has no authentication. Authentication requires a coordinated configuration
  change to avoid interrupting existing callers; it is not silently enabled here.
- Duplicate checks remain a read followed by an insert, so simultaneous callbacks
  can race. There is no unique constraint; existing duplicates are not deleted.
- SQLite durability depends on the deployed disk. This change does not provision
  storage, migrate to PostgreSQL, or verify the live Render configuration.
- Policy text is moved verbatim and describes SMS capabilities/STOP/HELP that
  are not implemented yet. Review it before launching messaging; this cleanup
  does not implement SMS or revise policy commitments.
- Caller phone/SID debug prints were removed. `.env` variants, virtual environments,
  databases and SQLite sidecars are ignored. `.env.example` contains a dummy number.
  If credentials were ever exposed elsewhere, ignoring files does not rotate them.
- Removed unused SQLAlchemy/PostgreSQL, Stripe, QR/image, HTTP/async and JWT
  dependencies. Retained Flask, dotenv, Gunicorn and their pinned dependency
  versions from the original requirements to avoid unrelated upgrades.
- Live Render/Gunicorn and real Twilio calls still need a deployment smoke test.
  The local regression suite verifies the Flask entry point and callback contracts.
