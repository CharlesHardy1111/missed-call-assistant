# Proposed PR: Add isolated conversational agent and voice-host foundations

Target: `main`. Working branch: `codex/agent-foundation`.
Status: prepared for branch publication and a separate synthetic review deployment;
no merge or production voice activation is included.

## Problem and resulting behavior

MCA already records missed calls and captures service leads through guarded SMS.
This change adds one configurable internal conversation controller for service and
office businesses. A synthetic caller can describe a clogged sink, supply a location
and urgency, verify a callback number, and leave a structured lead in the existing
conversation record. Configured business information and local inbox notifications
return confirmed results. Calendar/transfer integrations return not_configured;
the controller never promises a booking or transfer that did not happen.

The isolated voice adapter translates ConversationRelay frames into that controller.
An opt-in ASGI application validates signatures before accepting a connection,
binds the session to a trusted call and existing lead, bounds connection resources,
and preserves partial leads on failures. No production route imports these modules.

## Scope

- `agent_engine.py`: operator profiles, explicit offline schema, controlled tools,
  fact provenance, deduplicated events/actions, tenant-scoped outcomes, local mock
  interpreter and finite intake questions.
- `voice_relay.py`: exact configured-URL signature helper, durable single-use call
  claim, binding verification, final-turn processing, conservative interruption
  fallback, opt-out, SSML escaping and session limits.
- `voice_host.py`: ASGI handshake enforcement, timer ticks, bounded writes,
  connection/frame limits, cancellation cleanup and safe failure dispositions.
- `agent_demo.py`: synthetic temporary-DB demo, optional `--voice` protocol mode.
- `tests/test_agent_engine.py`, `tests/test_voice_relay.py`,
  `tests/test_voice_host.py`, `tests/memory_async.py`: behavioral/security regression
  tests and a socket-free Windows test harness; normal asyncio on Linux.
- `README.md`, `AGENT_FOUNDATION.md`, `VOICE_STAGING.md`: audit, architecture,
  voice-option/cost assessment, manual checks, and remaining activation gates.

No changes to app.py, routes.py, webhook_security.py, sms.py, followup.py,
requirements.txt, production templates, live Twilio settings, or Render environment.
No credentials added. Keep ENABLE_SMS_FOLLOWUP and ENABLE_PROGRESSIVE_CAPTURE false.

## Verification

- `python -m unittest discover -s tests -q`: **158 passed, 0 failed (71.169s)**.
- `python agent_demo.py --voice`: four-turn synthetic intake completed and saved.
- Python compilation and `git diff --check`: passed.
- No actual calls, SMS, paid model requests or deployment.
- Real Windows event-loop initialization hung on the sandbox's loopback socket
  pair, so host unit tests used an explicitly socket-free task/timer loop. These
  are ASGI message-exchange tests, not a real network/TLS smoke test.

## Material review risks / activation gates

1. Interpreter is deterministic/mock, not a production AI integration. Semantic
   extraction accuracy is not established merely by requiring verbatim evidence.
2. No running WebSocket listener or ASGI server dependency is installed. TLS,
   transport-level frame/queue limits, proxy behavior and exact provider signature
   interoperability still require an isolated deployment smoke test.
3. Interpreter/SQLite operations remain synchronous. Do not add remote I/O within
   the write transaction; async deadlines, cancellation and stale-result handling
   need their own tested boundary. Application socket timeouts do not bound DB work.
4. Interruptions conservatively request human review. No automatic transfer occurs;
   a signed Connect action with a verified fallback destination is still required.
5. Reconnect is rejected. Provider prompt frames lack durable event IDs; repeated
   text on a single ordered connection is not guessed to be a retry.
6. Profiles are snapshots; live permission revocation needs session termination.
   Internal Python APIs assume trusted business identity resolved by the host.
7. Transcript retention, deletion/access operations, consent/disclosure, quotas,
   load testing and real end-to-end latency/cost measurement remain pre-live work.
8. Explicit agent/relay migrations are not wired into production startup. The demo
   only touches a disposable database. Account activation on production still
   requires verified number mapping and a private session secret.

This is ready for foundation/code review, **not for production voice activation**.
The subsequent viewing request adds `review_app.py`, `templates/review.html`, and
four preview tests. A separate Render service runs fixed fictional scenarios using
the real engine and existing lead-card template; it has no write routes, credentials,
production database, real AI, telephony, or SMS integrations. `agent_demo.py` exposes
the shared fixture builder so the preview does not duplicate the engine or lead UI.
Production `main` and its start command remain unchanged.
