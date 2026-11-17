# Progressive service lead capture

This extends the existing private business inbox; it does not add a new CRM or voice
agent. Business-account provisioning and approved carrier/Twilio setup remain as
documented in [ONBOARDING.md](ONBOARDING.md). No deployment or live setting changes
are part of this implementation.

## Shared lead record

The existing `conversations` row is the shared lead, unique by business and original
contact number. Missed calls and message history already link to this row. An additive
migration adds `lead_source` and `lead_details` (versioned JSON stored as SQLite TEXT).

| Information | Representation |
| --- | --- |
| Account, contact, conversation ID | Existing business ID, phone number, row ID |
| Source | `call`, `sms`, `call+sms`, or `unknown` for migrated rows |
| Customer name, service requested, location, urgency, callback | Value, evidence state, and originating inbound message ID in `lead_details.fields` |
| Evidence state | `unknown`, caller-ID `candidate`, `customer_reported`, customer `confirmed`, or `declined` |
| Summary, missing details, next action | Derived consistently from the saved facts and current send/opt-out/status state; not a generated promise |
| Status and timestamps | Existing new/contacted/replied/closed status, aware UTC creation/update times, linked call-event times |
| Opt-out | Existing persisted `opted_out` state with original inbound messages retained as evidence; absence of opt-out is not blanket marketing consent |
| Capture progress | Asked fields, last question/message ID, and paused state, persisted across retries/restarts |

A missed call creates a provisional lead even if the customer never texts back.
The first inbound SMS also creates a provisional lead when no missed call exists.
Replies enrich the same lead. Answered calls still create no false missed-call lead.
Incoming voice sessions preserve parent/caller mapping before Dial, but a terminal
callback is still required to classify a missed call; this does not add voice transcription.

Old conversations keep their messages, IDs, and timestamps. Migration does not replay
historical messages, infer unknown details, or send a backlog. New inbound messages
populate the structured facts. There is one active service opportunity per existing
business/customer conversation, not separate job pipelines.

## Short SMS interaction

The first recovery message is unchanged. After the customer describes the problem,
MCA saves what was stated, then optionally asks one relevant question:

1. Service requested, if missing.
2. City/area when an explicit service description matches the V1 onsite-service
   vocabulary (for example sink, plumbing, roof, heater, HVAC, electrical, pests).
3. Urgency, if missing.
4. Whether the existing number is the best callback number, if not yet supplied or
   confirmed. A different callback number is displayed to the owner, never silently
   substituted as the SMS destination.

Name is optional and never an extra mandatory question. Explicit details supplied
together are captured together and their questions are skipped. “Unknown” never
becomes an invented name, address, urgency, quote, appointment, or commitment.
Facts are customer statements, not independently verified facts. The short summary
quotes the captured service description rather than inventing a diagnosis.

V1 is deterministic English-language intake, not general-purpose NLP. It recognizes
service phrases, explicit labels such as `location: Phoenix`, `urgency: today`,
`my name is Alex`, `call me at +15552222222`, and answers to the last successfully
sent question. Unrecognized input remains in full message history. Ambiguous answers
pause automation for owner review; the app does not repeatedly interrogate a customer.
It attempts each question at most once, at most four clarification messages per
conversation. A declined field remains missing for the owner. A business reply or
closing the conversation pauses automatic capture questions; passive explicit facts
can still be recorded. Reopening does not silently restart the bot.

## Send safety and owner notification

`ENABLE_PROGRESSIVE_CAPTURE=false` is the default. Structured capture and lead cards
work while it is false. Automatic clarifications require BOTH this flag and
`ENABLE_SMS_FOLLOWUP=true`, plus no opt-out. Keep both flags false unless real sending
has been explicitly approved. The existing SMS flag still controls all outgoing SMS.
No new provider, credentials, webhook URL, or dependency is required.

Inbound message, structured facts, and unread/new-activity state are committed in
one transaction before sending. The persisted clarification message gets a stable
per-conversation/per-field key and an exclusive send claim. Repeated `MessageSid`
webhooks do not advance capture or repeat texts. Before claiming a queued question,
MCA rechecks opt-out, pause state, and whether the question is still needed.
No automatic retries follow a failed or uncertain send. In-flight provider requests
cannot be recalled if STOP arrives after dispatch; provider opt-out enforcement
remains necessary. A process crash can leave pending/sending status needing owner
review, while the partial lead remains durable.

STOP and supported opt-out signals suppress further questions; common explicit
“stop texting/messaging/contacting me” requests also opt out locally. START/control
messages are not service facts and do not themselves trigger a clarification.
The existing opt-out rules and sender mapping apply to both automated and owner SMS.

Owner notification reuses the inbox's persisted unread/new-activity indicator.
There is no existing email, push, or owner-SMS notifier to reuse, so none was invented.
The existing inbox shows request, urgency, completeness, and next action; the
conversation adds name, callback confidence, location, missing information, and
opt-out state. No new tab or website redesign is introduced. Authentication,
CSRF protection, account scoping, escaping, and webhook signatures remain mandatory.

## Verification and brief manual script

Offline acceptance (no real messages):

```powershell
& .\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

For a separately approved staging test with a provisioned owner account:

1. Keep SMS and progressive questions disabled initially. Place an unanswered call;
   confirm one partial lead with caller-ID candidate, correct local time, and no SMS.
2. Verify a signed inbound text “My kitchen sink is clogged” appears on that same
   lead and creates new activity. Confirm partial facts survive reload and restart.
3. Only after explicit sending approval, enable both flags on staging and use a new
   test conversation. Repeat missed call → recovery → the sink message. Expect one
   location question. Reply “Phoenix”, then “Today”, then “Yes”. Expect one question
   at each step and a complete basic lead card; no appointment/quote is promised.
4. Stop after any step and confirm the partial lead remains. Replay one webhook and
   confirm no duplicate lead, detail advance, or clarification.
5. Reply STOP: retain the lead and opt-out state, with no further MCA texts. Inspect
   the mocked failure tests rather than deliberately disrupting production sends.
6. Sign into a second test account: the first business's lead and messages must be
   inaccessible. Answered calls must still produce no missed lead or recovery text.

Deployment requires the full, still-uncommitted business-inbox foundation as well
as this incremental change. Back up SQLite before the additive migration and review
that foundation's activation boundary and session-secret requirements. Do not enable
live SMS as part of installing this code. Live delivery, carrier behavior, provider
opt-out settings, and any future additional owner-notification channel remain manual
verification or separate work.
