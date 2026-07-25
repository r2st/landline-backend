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

## A/B tests are judged on their own metric

`evaluate_experiment(experiment, metric)` and `auto_declare_winner(..., metric=)`
take the metric the test declared; the resolver passes `test.metric` through, the
hard-cap default winner sorts by that metric's rate, and the conclusion alert
counts it. Subject-line tests default to `opened` — the subject decides whether a
message is opened and barely touches what happens after, and cold reply rates
(a few percent) need an order of magnitude more volume than open rates to reach
significance. Omitting `metric` keeps the legacy reply-then-open behaviour.
