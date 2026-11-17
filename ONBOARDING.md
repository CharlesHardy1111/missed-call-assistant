# Business inbox V1

This release is not deployed. Keep `ENABLE_SMS_FOLLOWUP=false`. That flag controls
**all** outbound texts, including replies. Disabled messages are recorded but never
sent later automatically. No phone number purchases, porting, carrier forwarding,
Twilio settings, or Render settings are performed by the application.

## Number and account model

Each business has one operator-provisioned account, one existing public number,
one dedicated MCA/Twilio voice-and-SMS number, a distinct ringing destination,
and an IANA timezone. Numbers cannot be reused across accounts or roles. All MCA
numbers in this V1 belong to the same platform Twilio account whose credentials
validate callbacks and send messages. Separate Twilio subaccounts/tokens are not
supported yet. Passwords are hashed; users never supply their business ID or sender.

The supported routing arrangement is:

`existing public number → carrier forwarding to dedicated MCA number → MCA rings a separate business destination → terminal result`

The business retains its public number. The operator must verify ownership and
configure carrier forwarding with the owner. Do not forward MCA back to the same
public number that forwards to MCA: that creates a loop. The ringing destination
must not forward back into this chain. Automated verification and self-service
number claiming are intentionally absent. Carrier voicemail can still answer a
call; a Twilio `completed` result is not classified as missed.

With no accounts provisioned, the existing `BUSINESS_PHONE` forwarding and legacy
dashboard continue unchanged. **Creating the first account activates mapped mode**:
incoming `To` must match a provisioned MCA number, unknown calls/numbers fail closed,
the public dashboard redirects to login, and generic `/call-event` intake is rejected
with 409 because it has no trustworthy tenant mapping. Unassigned historical rows
remain in the database, hidden from business inboxes; no automatic ownership guesses
or historical SMS sends occur. Finish in-flight legacy calls before activation.

## Provisioning (local first; production requires explicit approval)

1. Install `requirements.txt` and use a separate test `DATABASE_PATH` locally.
2. Set a stable private `SECRET_KEY` with at least 32 random characters. Do not use
   an example/test key in production. Keep the existing webhook origin and token,
   `BUSINESS_TIMEZONE=America/Phoenix`, `ENABLE_DEV_ROUTES=false`, and SMS disabled.
3. The operator verifies ownership of the public number and that the dedicated
   Twilio number is owned by the configured platform account and supports SMS.
4. Provision the account using Flask's CLI, substituting verified values:

   ```text
   flask --app app create-business --name "Example Business" --login owner --public-phone +15550000001 --mca-phone +15550000002 --forwarding-phone +15550000003 --timezone America/Phoenix
   ```

   The CLI prompts privately for a password of at least 12 characters and its
   confirmation. It creates the account only; it sends no SMS and does not change
   Twilio or carrier configuration. Share login credentials securely with the owner.
   Use a distinct set of numbers and login for the second account.
5. Login at `/login`; `/inbox` shows new activity, status, latest message, and missed
   call time. Open a conversation to view history, mark it read, close/reopen, or
   save a disabled reply. Password reset and additional staff logins are not in V1.

Sessions last eight hours and use Secure, HttpOnly, SameSite=Lax cookies. Browser
POSTs require CSRF tokens. Five failed attempts lock an account for 15 minutes.
Use HTTPS for production and local browser tests. For loopback-only HTTP development,
create an app with `create_app({'SESSION_COOKIE_SECURE': False})` and bind only to
`127.0.0.1`; this is a Python factory override, not a production environment switch.
Never use that override on Render. Tests exercise the real signature and login paths.

## Webhooks for an approved rollout

Keep the existing voice URL `/voice/incoming` and 20-second timeout. Its action and
Number callback continue to use `/voice/dial-result`. Incoming calls save the parent
SID, business ID, and original caller before returning TwiML. Callback ownership
comes from that saved binding, not child-leg From/To or user-provided business IDs.
Unknown parent callbacks return 404 for investigation rather than guessing a tenant.

Set each assigned MCA number's inbound messaging webhook to POST
`https://missed-call-assistant.onrender.com/sms/incoming` **only after approval**.
Use the existing origin-only `TWILIO_WEBHOOK_BASE_URL`. The SMS endpoint uses the
same mandatory Twilio signature validation, including exact query strings and all
form fields. `MessageSid` deduplicates inbound SMS. Destination `To` selects the
account; `(business_id, caller number)` selects the conversation.

Inbound SMS stores received text and UTC receipt time and returns empty TwiML;
there is no automatic TwiML text response. MMS is rejected in this text-only V1.
STOP/START/YES/UNSTOP and Twilio `OptOutType` update a conversation's opt-out state.
Opted-out conversations cannot receive recovery or business replies. Twilio can
send its own opt-out/help acknowledgements independently of MCA's sending flag.
Verify provider opt-out configuration and applicable messaging registration before
any explicitly approved real-send test. MCA does not implement custom HELP replies.
If using Messaging Services, keep each business's opt-out scope separate rather
than sharing a service whose opt-outs apply to all numbers across businesses.

## Persistence and failure behavior

Startup adds `businesses`, `voice_sessions`, `conversations`, and `messages` tables,
plus nullable business/conversation references on `missed_calls`. Existing rows
and timestamps are retained. Back up the production database before an approved
rollout and keep its persistent disk/path. Migration tests cover existing schemas.

Each missed call links to one conversation; repeat callers reuse that conversation.
Each distinct missed parent call may initiate one recovery text. Automated message
creation shares the lead-insert transaction. The existing send claim commits before
network I/O. Manual replies likewise commit a unique form submission key and send
claim before calling Twilio. Retries do not resend disabled, failed, sent, or in-flight
messages. Failures store only safe codes. A timeout or crash can leave an uncertain
send; reconcile with Twilio manually, never blindly retry. There is no background
queue or delivery-status reconciliation. “Sent” means accepted by Twilio, not proven
handset delivery.

All new stored times are aware UTC. Account timezone conversion happens only during
rendering. Conversation messages are ordered by insertion ID (application receipt/
submission order); inbound SMS does not supply an authoritative sent timestamp.
Recovery wording is stored per business with the existing default, without a V1
message-settings UI.

## Safe acceptance test

Run `python -m unittest discover -s tests -v`. Tests use temporary databases, dummy
credentials, real computed signatures, and blocked/mocked outbound network calls.

Next, on a separately approved staging deployment with SMS disabled, provision two
operator-verified accounts and test login/isolation and real signed inbound SMS.
Exercise an answered call (no lead), a genuinely unanswered call (one lead), and
busy/failed paths. Confirm parent/child retries still yield one event and the correct
local timestamp. Confirm recovery and saved manual replies say disabled and no MCA
SMS was sent. An unsigned SMS request must return 403. Opening the other business's
conversation URL must return 404.

Only after explicit approval to enable SMS on staging, test one recovery message,
customer reply, and one business reply, verifying both outgoing messages use the
same business MCA number. Repeat for the second account. Never enable real sending
as part of an automated test or this implementation task.

Remaining operational work: ownership/carrier setup, Twilio number provisioning,
messaging registration and consent review, HTTPS/session-secret setup, account
credential distribution, backup/restore procedures, and edge rate limiting. The
legacy unauthenticated dashboard remains only until the first account is provisioned.
V1 supports one owner login/number set per business, not staff roles or self-service
password recovery. SQLite and synchronous bounded sends retain their existing limits.

Protocol references: [Twilio messaging webhooks](https://www.twilio.com/docs/messaging/guides/webhook-request)
and [Messaging TwiML and opt-out](https://www.twilio.com/docs/messaging/twiml).
