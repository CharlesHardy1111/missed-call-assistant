# Isolated voice protocol milestone — November 17, 2026

This includes a **local protocol harness and opt-in ASGI host application**, not a
deployed voice agent or running WebSocket service. It extends Implementation 001 without modifying Flask routes, TwiML,
production authentication, Render configuration, requirements, or SMS switches.
The prior uncommitted agent work is preserved on `codex/agent-foundation`.

## What is implemented

`voice_relay.py` maps documented Twilio ConversationRelay setup, final prompt,
partial prompt, interrupt, error and DTMF messages to the existing AgentEngine.
It emits speech text or a structured needs-human end disposition. Twilio would
provide speech recognition/synthesis later; this harness opens no sockets, reads
no credentials, and invokes no provider. The interpreter is still deterministic.

ConversationRelay was selected for this preparatory step because its text protocol
matches the existing controlled response engine. This avoids forwarding unconstrained
model audio that could claim an unconfirmed booking. The previously assessed native
Realtime/Media Streams option remains available; this is not a procurement decision.

The protocol and security requirements are based on Twilio's official
[message reference](https://www.twilio.com/docs/voice/conversationrelay/websocket-messages)
and [onboarding guidance](https://www.twilio.com/docs/voice/conversationrelay/onboarding).

- A handshake helper validates the configured public URL and exact raw path/query
  using Twilio's SDK. Missing token/signature or changed query/host is rejected.
  No proxy header or alternate URL is trusted. Configuration may specify HTTPS or
  WSS; **only the configured string** is validated. Provider signing behavior must
  be confirmed on the isolated staging hostname before activation, not inferred
  from synthetic signatures. This helper is not yet mounted on a listener.
- Setup must match a trusted expiring call/account/caller/destination binding.
  Custom parameters never select the business. Constructor checks tenant ownership.
  A durable single-use call/interaction claim rejects duplicate connections, including
  across new adapter instances. Setup does not create a new lead.
- Only final transcripts enter the engine. Partial transcripts are ignored. Limits:
  8 KiB per frame, 1,600 transcript characters, 30 seconds idle, five-minute session.
  The host must call `tick()` during silence. Partial frames cannot keep it alive forever.
- Speech is interruptible/preemptible. On interruption this initial version ends
  automation and requests human review, preserving partial facts. It does not resume
  with ambiguous question state or treat a subsequent “yes” as verified consent.
  This conservative behavior needs usability work before customer use.
- Output is escaped for SSML so knowledge/customer text cannot inject audio markup.
- Provider errors are represented by safe codes; raw exception descriptions are not
  stored. Disconnections preserve the lead. STOP/withdrawal sets opt-out; spoken YES
  does not resubscribe an opted-out conversation. DTMF requests human review.
- End messages request a disposition; **no transfer is claimed or performed**.
  A future signed Connect action must execute/confirm an authorized fallback.
- A bounded numeric controller timing list is available for instrumentation. It
  excludes telephone/STT/TTS/network delay and is not a voice-latency benchmark.

## Safe manual check

```powershell
.\.venv\Scripts\python.exe agent_demo.py --voice
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

The demo reuses the existing disposable-DB fixture, simulates setup and four final
transcripts, prints outbound protocol frames and the structured saved lead. No
audio is played and no call is placed. Use the focused regression tests to inspect
interruptions, malformed input, replay rejection, silence, opt-out and failed handoff.

Final local verification: **144 tests passed, 0 failures in 62.135 seconds** (108
existing application tests, 20 agent tests, 16 relay tests). The voice-frame demo,
Python compilation and `git diff --check` passed. Protected production files have
no changes. No live latency, speech quality, provider signature interoperability,
phone transfer, or paid-service behavior has been verified.

## Remaining live-demo gate

Do not point the production number at this code. Before any approved live test:

1. Supply an isolated test number and staging hostname; approve a spending limit and
   any required provider terms. Keep production number/webhook/20-second Dial intact.
2. Deploy and verify the opt-in ASGI host with a supervised WebSocket runtime and
   TLS, enforce transport limits before buffering, and resolve bindings from signed intake.
   Neither this internal Python API nor caller-provided setup is authentication.
3. Define signed test intake/Connect action and verified ringing destination. Test
   no-answer, provider failure, socket loss, silence and actual human fallback.
   Retain a fallback when DB/storage itself is unavailable. No optimistic transfer.
4. Add a bounded async model adapter outside SQLite write transactions, with stale
   result rejection, cancellation and tool permissions. Current synchronous mock
   must not be replaced by network I/O inside the engine's transaction.
5. Keep reconnection disabled until there is durable provider event identity and
   safe replay reconciliation. ConversationRelay prompts have no event ID in this
   protocol; a single ordered socket gets monotonic local IDs. Repeated text on that
   socket is treated as a new utterance, not guessed to be a webhook retry.
6. Verify exact provider-signed URL, opt-out/voice disclosure/retention behavior,
   elapsed turn latency, interruption experience, action confirmations and cost.

No environment variables, credentials or production changes are required for this
offline checkpoint. `ENABLE_SMS_FOLLOWUP=false` and `ENABLE_PROGRESSIVE_CAPTURE=false`
remain unchanged. Live activation, provider terms, paid use, and deployment still
require explicit approval. See AGENT_FOUNDATION.md for voice cost comparisons.

## ASGI host review checkpoint

`voice_host.RelayHost` now performs signature validation before WebSocket acceptance
or session-factory invocation. It rejects missing/duplicate signature headers and
missing raw request targets, uses the configured public URL including its exact
query, and ignores proxy-provided scheme/host values. Its session factory is trusted
configuration, not caller data. A separate authenticated intake/resolver is still
needed to populate bindings for real calls.

The application runs timer ticks during silence, caps per-process simultaneous
connections and per-session frame count, bounds awaitable socket writes, releases
capacity after exceptions/cancellation, and makes a best-effort needs-human response
on host failure. Failed writes are not claimed delivered. Completed intakes remain
complete after transport disconnect; unfinished ones retain partial facts and need
review. Binding validation additionally checks stored caller/business identity.

Usage contract (not a public production entry point):

```python
host = RelayHost(
    configured_public_url, private_twilio_auth_token,
    trusted_session_factory,
    context_factory=isolated_flask_app.app_context,
)
# Serve `host` with an independently configured ASGI WebSocket server.
```

No ASGI server package is installed in this environment or added to production
requirements. Do not use synchronous `gunicorn app:app` to serve WebSockets. A
separate launcher/runtime with TLS, server-level message limits and queue limits
is required before accepting real traffic. Current application limits apply after
the ASGI server assembles a frame. Synchronous local DB/interpreter work can still
block the event loop; socket deadlines do not impose deadlines on that work. This
host is not production-ready until that execution boundary and live load are tested.

Windows sandbox asyncio initialization hung while creating its internal loopback
socket pair, before application code ran. Those runs were interrupted. The host
tests therefore use a test-only in-memory timer/task loop on Windows that rejects
all file/socket registration; it cannot serve a socket. On Linux they use normal
asyncio; Windows can opt in with `MCA_TEST_REAL_ASYNCIO=true` when local sockets
are permitted. This is a test harness option, not a webhook security bypass.
No actual socket/TLS/provider interoperability result is claimed by these tests.

Final host-review verification: **158 tests passed, 0 failures in 71.169 seconds**
(the prior 144 plus 14 ASGI/security/lifecycle tests). Python compilation and
`git diff --check` passed. This supersedes the earlier protocol-only checkpoint.
