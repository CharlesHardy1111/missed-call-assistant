# Implementation 001: offline conversational agent foundation

## Audit and checkpoint — November 17, 2026

The attached strategy document predates PR #7. The actual starting point is deployed
main `cd59854a674b43b4629ddf06291b0d12b7cb8c4a`, including the private business inbox,
signed inbound SMS, opt-out controls, guarded replies, and progressive lead capture.
The prior working tree was clean; nothing unfinished was overwritten. Work continues
on `codex/agent-foundation`. The existing baseline has 108 tests.

Existing Flask/Gunicorn, SQLite migrations and persistent Render disk remain the
deployment model. Voice intake still forwards for 20 seconds; terminal callbacks
deduplicate parent calls and record missed leads. Webhook signatures use the public
HTTPS origin plus exact path/query. UTC storage/Phoenix display is already repaired.
Account sessions, CSRF, tenant-scoped inbox queries, and signed SMS are implemented.
SMS and automatic SMS clarification remain disabled. Production private login still
needs SECRET_KEY and verified account provisioning; first-account activation changes
call routing and is outside this task.

Missing before this work: an interaction-level agent controller, replaceable intent
interpreter, business-specific agent permissions/knowledge, and operational tool
results distinct from conversational claims. No calendar integration, live voice
agent, provider delivery acknowledgement, or external owner notifier exists.

## Smallest safe implementation

`agent_engine.py` is a transport-independent internal service, not a public API.
`Interpreter.interpret(text, pending)` returns a typed Decision (intent, facts).
The included OfflineInterpreter is an explicitly deterministic test/demo substitute,
not an OpenAI integration. Existing extraction helpers are reused. The engine accepts
only known intents/fields and verbatim evidence from the current utterance. This
checks provenance, not semantic truth; facts remain customer-reported. Caller ID
is a candidate until the customer answers the callback question. Model output never
provides arbitrary speech, executable code, tenant IDs, URLs, or tool destinations.

One engine supports both service and office workflows through per-business knowledge,
ordered intake questions, and tool permissions. Four optional questions maximum;
an unclear answer ends automatic questioning instead of looping. Business profiles
are operator-controlled and snapshotted at interaction start. Medical configurations
are rejected; this is not a medical-content classifier or a compliance system.

The existing `conversations` row remains the shared lead. Partial facts are saved
after each final utterance and are visible through the existing lead card when an
account is configured. Agent evidence adds `agent_event` references to existing
field metadata, without inventing SMS message IDs. Offline sessions do not fabricate
missed-call records or mark their source as a real call/SMS. Source stays unchanged.
No second CRM, tab, dashboard redesign, new dependency, or live webhook was added.

Explicit `init_agent_store()` adds four tables only to the selected database:
profiles, interactions, events, actions. Flask startup does NOT call it. The demo
uses a temporary database, never loads `.env`, and never imports `app.py`.
Events and local actions commit atomically; identifiers deduplicate retries, conflicting
replays are rejected, and every interaction lookup includes the trusted business ID.
Finalized input and outcomes survive interpreter errors/disconnections. Partial audio
interruptions generate no answer/facts; actual audio cancellation is a later transport task.

| Controlled interface | Initial implementation |
| --- | --- |
| get_business_information | Confirmed result from operator-configured knowledge only |
| check_calendar_availability | Permission checked; not_configured |
| create_appointment | Permission checked; not_configured; no booking claims |
| transfer_call | Permission checked; not_configured; needs-human outcome, no transfer claims |
| create_customer_record | Reuses the existing provisional lead; confirmed local record ID |
| notify_business_owner | Marks the existing inbox unread; not proof of reading/email/SMS delivery |

Only trusted application code may construct AgentEngine with a business ID; this
Python API is NOT an authentication layer. Future adapters must resolve identity from
authenticated sessions/signed call bindings. It is not safe to expose caller-provided
business IDs. Tool results must be the sole basis for future booking/transfer speech.
Stubs deliberately accept no appointment/destination arguments until integration
validation, customer confirmation, idempotency and permission contracts are defined.

## Voice integration assessment

Sources checked November 17, 2026; no paid service activated:

| Approach | Fit and tradeoffs |
| --- | --- |
| Twilio bidirectional Media Streams + OpenAI Realtime WebSocket | Native audio conversation; requires an async WSS bridge, bounded buffers, audio codec negotiation, cancellation/clear handling, signature validation on the handshake, reconnect policy and supervised workers. Existing synchronous Gunicorn worker is not this bridge. Keep Flask control/data services and introduce a separately tested transport. |
| Twilio ConversationRelay + text-model adapter | Twilio handles speech recognition/synthesis and exposes text over WSS. Easier to connect to this text-turn engine; adds provider speech costs and a staged speech/text latency path. Still requires WebSocket infrastructure and real interruption tests. |
| Twilio SIP + OpenAI voice | Offloads media transport and supports server-side control. Requires SIP routing/project webhooks and provider-specific authentication; changes live routing and therefore belongs in an explicitly approved staging milestone. |

Recommendation (engineering judgment): first validate an isolated Twilio test-number
Media Streams/Realtime bridge with measured response latency and human fallback;
retain this common business/tool boundary. Compare ConversationRelay if operational
simplicity outweighs native audio behavior. Do not replace the repaired forwarding
webhook to demonstrate the agent. No latency or reliability benchmark is claimed here.

Official references:
- [OpenAI quickstart](https://developers.openai.com/api/docs/quickstart)
- [OpenAI Realtime conversations](https://developers.openai.com/api/docs/guides/realtime-conversations)
- [OpenAI SIP](https://developers.openai.com/api/docs/guides/realtime-sip)
- [Twilio Media Streams](https://www.twilio.com/docs/voice/media-streams)
- [Twilio stream protocol](https://www.twilio.com/docs/voice/media-streams/websocket-messages)
- [Twilio ConversationRelay](https://www.twilio.com/docs/voice/conversationrelay)

## Operating cost planning

This implementation/demo costs **$0 in provider usage**. Existing hosting costs are
unchanged. Published US list prices inspected today: inbound local voice $0.0085/min,
Media Streams $0.0044/min, ConversationRelay $0.07/min, local number $1.15/month;
outbound US/Canada call legs $0.014/min. Thus a five-minute inbound stream is about
$0.0645 before model usage, hosting, taxes, rounding, and any transfer leg. A five-minute
ConversationRelay call is about $0.3925 before text-model costs and those extras.
[Twilio pricing](https://www.twilio.com/en-us/voice/pricing/us)

Current OpenAI pricing lists gpt-realtime-2.1 audio input/output at $32/$64 per million
tokens (mini $10/$20), with separate text/cached-input rates. Example budget arithmetic:
10,000 audio input + 10,000 output tokens would cost $0.96 on that full model, excluding
text. This is NOT an estimate of tokens in a five-minute call. Instrument real session
usage, silence, retained context, interruptions, and concurrent calls before selecting
a model/budget. Prices/model availability must be rechecked at activation.
[OpenAI pricing](https://developers.openai.com/api/docs/pricing)

## Verification and safe manual demo

From the repository in PowerShell:

```powershell
.\.venv\Scripts\python.exe agent_demo.py
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

The demo uses synthetic numbers and a disposable DB. It runs clogged sink -> Phoenix
-> today -> callback confirmation and prints the saved outcome. Inspect unknown name,
customer-reported facts, confirmed callback, UTC timestamps, and no booking promise.
It deletes only its own temporary directory when finished. No credentials required.
Tests cover both business workflows, abandonment/restart, duplicate/conflicting events
and tools, isolation, interruption, unavailable/malformed interpreter, denied tools,
unconfigured calendar/transfer, medical rejection, migration repeatability, and no
public route activation. The full suite retains real signature/routing/SMS regressions.

Final verification: **128 tests passed, 0 failures (56.072 seconds)**: 108 existing
regressions plus 20 agent-foundation tests. The offline demo completed successfully;
Python compilation and `git diff --check` passed. No frontend files changed; existing
Flask-rendered dashboard tests are included in the full suite. No live provider test
was performed. Changes remain local and uncommitted for review.

## Limits and next milestone

No live AI, speech, calendar, transfer, or external notification was built or tested.
The fallback records needs_human; it cannot actually connect a human. Existing opted-out
and closed leads block intake; new voice consent/withdrawal handling must be designed
before a voice adapter. Do not route live SMS into this engine: existing signed SMS
consent handling remains authoritative and untouched. Knowledge is trusted operator
content. A verbatim quote can still be semantically misclassified; the mock is narrow
English parsing and cannot establish model quality.

Offline interpreter work runs inside a short SQLite transaction. Do NOT plug a remote
model into that synchronous boundary without durable turn claims, deadlines, cancellation,
stale-result checks, and network work outside the write lock. No external side effects
are currently possible; future connectors need provider idempotency, ambiguous-result
reconciliation, customer confirmation, and at-most-once execution tests before activation.
No automatic retry of a booking/transfer may be inferred from a dropped connection.

Full utterances and results are private DB data (no transcript logging). Retention,
deletion, encryption/access operations, consent notices, per-session budgets, load/rate
limits, transport replay controls and interruption timing remain pre-live requirements.
Configuration snapshots require ending an active session to revoke its permissions.

No new Render variables are required now. Later approved OpenAI use needs a server-side
API credential and project/model choice; SIP also needs a separately validated webhook
signing secret. Calendar OAuth scopes/account and verified transfer destination remain
unconfigured. Never paste keys/passwords into chat or commit them. Keep both existing
SMS flags false. Nothing in this branch is deployed or changes production settings.
