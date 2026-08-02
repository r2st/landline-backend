# CLAUDE.md

This file tells any Claude Code session everything it needs to know to work on the landline-backend codebase effectively.

## Project Overview
Landline is a B2B lead generation and client management platform. FastAPI backend with SQLAlchemy async, PostgreSQL, Redis, Celery Beat.

## Tech Stack
- Python 3.11, FastAPI, SQLAlchemy async, Alembic migrations
- PostgreSQL (docker, port 5433→5432, db: landline_db)
- Redis (port 6380→6379)
- Celery Beat for scheduled automation
- Anthropic Claude API for AI features (tactics, news analysis, scoring)
- Gmail OAuth + SMTP for email sending

## Code Structure
- `app/routers/` — API endpoint files (companies, contacts, email_sends, sequences, outreach, news, scoring, tactics, gmail, smtp, deals, tasks, interactions, pipeline, suggestions, reports)
- `app/models/` — SQLAlchemy models
- `app/services/` — Business logic (health scoring, email dispatch, etc.)
- `app/config.py` — Settings from environment
- `alembic/` — Database migrations (currently at 018)
- `tests/` — Pytest test suite (175+ tests)

## Common Tasks

### Run tests
```bash
pytest tests/ -v
```

### Create a new migration
```bash
alembic revision --autogenerate -m "description"
```

### Run migrations locally
```bash
alembic upgrade head
```

### Add a new router
1. Create `app/routers/new_router.py`
2. Register in `app/main.py`: `app.include_router(new_router.router, prefix="/api/v1/new", tags=["new"])`
3. Add tests in `tests/test_new.py`

## Deploy
Push to main. Deploy is handled externally via SSM to EC2 at `/opt/landline`.
- `git pull origin main`
- `docker compose -f docker-compose.prod.yml build`
- `docker compose -f docker-compose.prod.yml up -d`
- `docker compose -f docker-compose.prod.yml exec -T api alembic upgrade head`

## Conventions
- Push directly to main, no PRs
- Every new feature needs tests
- Use async SQLAlchemy patterns (see existing routers for examples)
- Pipeline stages: `new_lead → researching → outreach → engaged → negotiating → client → churned`
- API responses follow existing patterns — check similar routers
- Alembic migrations must chain correctly (check `down_revision`)

## Environment Variables
Key vars in `.env.production`: `DATABASE_URL`, `REDIS_URL`, `ANTHROPIC_API_KEY`, `APOLLO_API_KEY`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_OAUTH_REDIRECT_URI`, `FRONTEND_URL`, `FERNET_KEY`

## AI provider chain
Every AI feature routes through `AIService` → `app/services/llm_providers.py`, which
tries providers in order (`LLM_PROVIDER_CHAIN`, default
`openrouter,gemini,groq,cerebras`), then Anthropic, then the caller's static
template. Keys: `OPENROUTER_API_KEY`, `GEMINI_API_KEY`, `GROQ_API_KEY`,
`CEREBRAS_API_KEY` — an unkeyed provider is skipped, not attempted.

A provider failing 3× in a row is skipped for 5 minutes
(`LLM_BREAKER_THRESHOLD` / `LLM_BREAKER_COOLDOWN_SECONDS`).

### Rate limits are a routing decision, not a wait

`app/services/llm_rate_limit.py` paces each provider to its own cap
(`LLM_PROVIDER_RPM`, `"name:rpm"` pairs) and honours `Retry-After` on a 429 by
parking that provider. The breaker is the wrong instrument for this in both
directions: it needs three consecutive failures, so a batch send hits the cap and
then burns three more round trips before anything stops — and then it blackholes
a *healthy* provider for five minutes over a limit that clears in one.

Three rules hold this together:

- **Throttled is not failed.** A paced or parked provider records no failure and
  never moves the breaker. Being early is not being broken.
- **Don't wait when another provider is idle.** `LLM_RATE_LIMIT_MAX_WAIT_SECONDS`
  (3s) is deliberately small — the chain's whole premise is an independent quota
  one hop away. The exception is when *every* provider is paced out before a byte
  is sent, where returning would degrade the caller to static mail-merge copy
  because we were two seconds early; then the chain stalls for the soonest one, up
  to `LLM_RATE_LIMIT_MAX_STALL_SECONDS`. It does **not** stall after real 429s —
  those round trips are already spent.
- **A park picks providers; it doesn't interrupt one mid-call.** The slot is
  claimed once per provider, before its model list. OpenRouter's free model 429ing
  is exactly when the paid sibling — separate allowance — should be tried, and a
  success clears the park, because a provider that just served us is not limited.

`GET /api/v1/ai/providers` reports `rate_limits` next to `circuit_breakers`:
"outreach went out as templates" and "outreach went out personalized but
throttled" are otherwise indistinguishable from outside the process.

Services that predate `AIService` and call `client.messages.create(...)` directly
(sales coach, orchestrator, meeting/relationship/competitive intelligence,
sentiment, script generator, investor outreach) use `chain_client()` from the
same module — it keeps the Anthropic call shape and response
(`message.content[0].text`) but serves it from the chain. Gate those on
`any_llm_available()`, **not** on `settings.anthropic_api_key`: a box carrying
only a Groq key has working AI, and the narrow gate would report it dark.

**Never add a bare `httpx.post` to a model endpoint, and never construct
`anthropic.AsyncAnthropic` in a service.** Twice now a single-provider
call has run out of quota and silently degraded outreach to static mail-merge
copy while every log line said "fine". `GET /api/v1/ai/providers` reports which
provider is actually serving.

## Sent mail lives in two tables

There is **no single "sent emails" table**. A send lands in one of two places
depending on which path put it on the wire:

- `email_sends` (`EmailSend`) — manual sends, template sends, inbox replies,
  smart-reply auto-sends.
- `outreach_events` (`OutreachEvent`, `event_type='sent'`) — **the sequence
  engine** (`app.tasks.outreach.send_sequence_step`), which is where the bulk of
  outreach volume goes. It writes **no** `EmailSend` row; the message body,
  subject, recipient and dispatcher result live in the event's `metadata`.

Anything that answers "what did we send?" must read **both**. Reading only
`EmailSend` is how the unified inbox went blind to every sequence send — mail
sitting in Gmail's Sent folder, nothing in Landline, and prospect replies shown
as threads with no opening message. `EmailHistoryService._fetch_sequence_sends`
is the read-side union (inbox, thread view, lead view); mirror it rather than
adding a second write path, because the send accounting (warm-up counters,
deliverability, A/B attribution) is keyed on today's split.

Inbox surfaces that still read `EmailSend` alone — `get_inbox_analytics`,
the dashboard's `emails_sent` tile — therefore under-report sequence volume.

## The daily send cap is a Redis reservation, not a row count

`SendLimiter.can_send_today` counts `OutreachEvent(event_type='sent')` rows and
compares them to the limit. It is **advisory only** — safe for dashboards and
for the queue sweep's cheap pre-check, and wrong for anything that then sends.
Between that read and the event insert sit recipient validation, Hunter
verification, AI composition and an SMTP round-trip, and every concurrent worker
reads the same pre-send count. N workers each conclude they are the last one
under the cap, and the cap is exceeded by N.

The send path must claim its slot with `await limiter.reserve_daily_send(db)`
immediately before the wire, and hand it back with `release_daily_send()` on any
outcome that isn't a real send (soft skip, dispatcher exception, bounce). One
`INCR` per intended send, so exactly `limit` workers get a slot however many ask
at once. The counter seeds from the DB (`SET … NX`) so a restart or an eviction
resumes at the real number. With Redis unreachable it degrades to the row-count
check — per-worker, never looser — and says `[degraded: …]` in the reason.

## A step goes on the wire at most once

Every send gate in this codebase asks the *database* — and the window between
"Gmail has the message" and "the transaction saying so committed" has not
written any of it. `send_sequence_step` sends and then records the
`OutreachEvent`, the advanced `current_step` and the thread ids; a dropped
`COMMIT` (or a raise, or a killed worker) in that window rolls the record back
and leaves the mail. The Celery retry then finds `step N, active, due` and sends
the same cold email to the same prospect again.

`app.services.send_claim.acquire(db, enrollment_id, step)` is the interlock: a
conditional UPDATE on `outreach_enrollments.send_claim_step`, **committed on its
own session** immediately before the dispatcher call, so it survives that
rollback. The row lock also decides the concurrent case — two workers, one
enrollment, neither having committed.

- **Never release a claim on an ambiguous outcome.** `send_outreach_email` does
  work *after* the transport returns, so an exception out of it does not prove
  the message stayed home. Release only on a returned soft-skip status
  (`skipped` / `suppressed` / `halted`) — the dispatcher declined, it didn't fail.
- **A stale claim is reconciled, never re-sent.** `send_claim.reconcile` writes
  the missing `sent` event (flagged `reconciled` / `wire_outcome: unconfirmed`,
  and counted, because deliverability rates divide by *attempts*) and advances
  the cadence, so the enrollment stops being re-dispatched and refused forever.
- **Any new send path must claim.** A path that sends without one reintroduces
  the duplicate for itself.

`OutreachEnrollment.dispatched_at` is the other half — a lease taken by the
queue sweep before the task exists. The sweep marked nothing before, so an
enrollment whose task was still in the broker was dispatched again five minutes
later; a worker half an hour behind held six copies of every task in its
backlog. Time-based, never explicitly cleared, floored above the sweep interval
(a lease shorter than 5 minutes is inert).

## An opt-out stops the mail, and keeps stopping it

There are three ways out of Dialtone mail — a reply saying "unsubscribe", the
bulk compliance route, and **the link in the footer of every email plus the RFC
8058 one-click header Gmail and Yahoo require**. The first two set
`contact.suppressed`, wrote a consent record and stopped every live enrollment.
The third set `contact.unsubscribed` and nothing else — and no send gate has ever
read that column. So the only opt-out a recipient can actually reach was the only
one that let steps 2 through 5 keep arriving on schedule.

**`app.services.opt_out.record(db, email, org_id=…)` is the one way to record an
opt-out.** It sets both flags, writes the revoked `ConsentRecord`, stops every
enrollment in `ACTIONABLE_STATUSES`, *and* adds the address to the org
suppression list. Never write the flags directly.

- **The list is the durable half.** Flags live on a contact row and the scraper
  creates rows: someone who opts out today and is rediscovered next month arrives
  with `suppressed=False`. `SuppressionEntry` is keyed by `(org, email)` and
  outlives every row. `unsubscribe` was a legal `SUPPRESSION_REASONS` value that
  nothing had ever written.
- **Match every row, case-insensitively.** `contacts.email` has no unique
  constraint. A `scalar_one_or_none` here raised `MultipleResultsFound` on a
  re-scraped duplicate, which became a 400 — "That link didn't work" to the human,
  a broken one-click affordance to the mailbox provider, and nothing recorded.
- **One serializer, `opt_out.unsubscribe_serializer()`.** The sender used to fall
  back to a hardcoded secret and the endpoint to a random per-process key, so on
  a box with no `SECRET_KEY` every unsubscribe link we sent was unverifiable.
- **Tokens carry the sending org**, so an opt-out lands on that sender's list
  rather than every org holding the address. Bare-string tokens are the legacy
  shape, stay valid a year, and are in real inboxes — they decode to
  `org_id=None`, which deliberately applies the opt-out everywhere.
- **Only promise what happened.** The confirmation page and the one-click 200 are
  claims that the address is suppressed; return the error page when it isn't.

The send path checks `suppressed` **or** `unsubscribed` **or** the list, in both
`send_sequence_step` and the queue sweep. The `unsubscribed` read is deliberate
redundancy: rows written by the old link handler are in the database now.

## Pre-send spam gate

Every outbound path runs the deterministic content scan
(`app.services.spam_guard`) on the final rendered copy. `flag` records the score
on the send; `block` is a **soft skip** that holds the enrollment for a copy
edit. Blocking is off by default (`SPAM_GUARD_BLOCK_ENABLED`) — a content
heuristic that stops outreach on its own judgement is a worse failure than the
spam it prevents.

The sequence path scans **before** `apply_tracking` and passes
`skip_spam_scan=True` to the dispatcher: scoring the post-tracking body would
count our own rewrapped links as "too many links". Any new send path either
scans its own pre-tracking copy or lets `send_outreach_email` do it.

## Auto-pause: match the response to the scope

- **Global-scope** danger breach → the system-wide kill switch. An aggregate
  bounce rate that high is a shared domain/IP reputation problem.
- **Account-scope** breach → `DangerMonitor.pause_accounts` disables just that
  mailbox (`sending_enabled = False`) and the rest of the fleet keeps sending.
  One pause and one alert per account; a breach that persists across sweeps
  stays silent, because an alert that repeats every 5 minutes gets muted.
- **Out-of-office reply** → `out_of_office.pause_for_ooo` sets the enrollment to
  `paused` with `pause_reason='out_of_office'` and `next_send_at` carrying the
  return date; the hourly `resume_out_of_office` task brings it back.

`OutreachEnrollment.pause_reason` is what makes an automatic resume safe. A
sweep that resumed every paused enrollment with a past due date would also
revive the ones paused for a missing address, a dead domain, a consent block, or
an operator's decision. **NULL means "not ours — leave it alone."**

## One resolver decides when a step is due

`app.services.step_delay.due_at(step, anchor, factor=, enrollment_id=)` computes
every due date in the system. **Do not add a sixth `anchor + timedelta(days=step.delay_days)`.**

There were five, and they had drifted: the post-send advance, the optimizer
advance and `adaptive_delay.reschedule_pending_step` applied
`OutreachEnrollment.adaptive_delay_factor`; the queue sweep's legacy path and the
sequences router's preview did not. An enrollment whose factor had moved off 1.0
was therefore "due" to the sweep and "not yet due" to the send task the sweep
dispatched — a step that oscillates instead of sending.

A step's cadence is `delay_days` + `delay_hours`, optionally counted in business
days (`delay_business_days`) and spread by `delay_jitter_minutes`. Three rules
worth knowing before touching it:

- **Day-granular steps must keep their exact schedule.** A step with
  `delay_hours == 0` delegates to `adaptive_delay.effective_delay_days` — whole-day
  rounding and the `[1, 14]`-day clamp included. Every sequence in the database is
  one of these, and changing where they land silently reschedules live outreach.
- **Jitter is derived from the enrollment id, never drawn fresh.** The sweep
  recomputes due dates for enrollments with no `next_send_at`, so a random offset
  would move the deadline on every poll.
- **This decides the schedule, not the send.** Business hours, weekends,
  per-contact timezone, warm-up, the daily cap and per-mailbox pacing are all
  enforced at dispatch by `send_limiter` / `send_pacing`. What `step_delay` owns is
  the *spacing between touches*, which nothing downstream can reconstruct — the
  send window holds a Sunday send to Monday, which is exactly why a Thursday +3
  and a Friday +2 used to land on the same Monday.

## A/B tests are judged on their own metric

`evaluate_experiment(experiment, metric)` and `auto_declare_winner(..., metric=)`
take the metric the test declared; the resolver passes `test.metric` through, the
hard-cap default winner sorts by that metric's rate, and the conclusion alert
counts it. Subject-line tests default to `opened` — the subject decides whether a
message is opened and barely touches what happens after, and cold reply rates
(a few percent) need an order of magnitude more volume than open rates to reach
significance. Omitting `metric` keeps the legacy reply-then-open behaviour.
