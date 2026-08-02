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

## Tenant isolation is opt-in, so the opt-in is the thing to check

A model is isolated because it mixes in `OrgScopedMixin`. That gives it a NOT
NULL `org_id` and enrols it in the `do_orm_execute` filter that scopes every
SELECT to the caller's org. A model that doesn't mix it in is not "unscoped
pending review" — it is a table every customer on the platform shares, and it
looks identical from every angle except the mixin.

`company_groups` was one for two years. The chain that hid it is worth knowing,
because it is the chain, not the missing mixin, that will do it again:

- `app/models/__init__.py` did not import `company_group`, so the class was
  never registered on `Base.metadata`;
- `alembic/env.py` built `target_metadata` from a hand-written list of 19 module
  names, last extended around migration 030, so it didn't have it either;
- migration 078 — the autogenerated one that put `org_id` on 71 tables —
  therefore had nothing to notice, and skipped it.

`GET /groups` listed every org's groups, and the id it returned was enough to
rename, delete or re-populate another tenant's folder. Auto-grouping was the
quiet one: it asks "is there already a group called 'Aerospace'?", found another
tenant's, and filed this tenant's companies into it. Migration 111 fixed the
table; `alembic/env.py` now imports the package.

Three rules:

- **Every new model mixes in `OrgScopedMixin`** unless it is genuinely
  platform-wide, and every new model is imported in `app/models/__init__.py`.
  `tests/test_org_scoping_coverage.py` enforces both — it walks the model
  *directory*, not the package, so a missing import cannot hide a model from it.

  The registration half of that check runs in a **subprocess**, and has to.
  `Base.registry` is process-global and only grows, so once conftest and the
  tests above it have imported every module, "what does `import app.models`
  register?" can no longer be asked in-process — the first version of this check
  passed with the `company_group` import deleted again, i.e. against the exact
  bug it was written for. Any future check of the form "is X reachable from a
  bare import?" has the same trap.
- **Three models are exempt, and the reasons don't generalise.**
  `Organization` (scoping the tenant to a tenant is circular), `TenantUsage`
  (keyed by `tenant_id` already), and `User` (membership *is* the `org_id`
  column; login and the platform-admin cross-org views read users before any org
  context is bound).

  **Every `select(User)` scopes itself.** There is no filter behind it, and an
  unscoped one is invisible on review precisely because every *other* query in
  the codebase looks identical and is scoped. Use
  `dependencies.scope_users_to_org(stmt, caller)` when a caller is in hand, or
  `scope_users_to_current_org(stmt)` in services and Celery tasks, which have an
  org binding instead. Both exempt `platform_admin`, which exists to cross orgs.

  Four hand-rolled copies of the predicate existed and three call sites had
  none: `?view_as_user_id=`, `GET /coaching/team` (which enumerated every user
  on the platform *and* wrote a `RepPerformance` row for each, stamped with the
  caller's org), and `POST /coaching/generate?target_user_id=`. The worst was
  `generate_weekly_coaching`: it runs inside `for_each_org`, which binds each org
  precisely so scoped reads come out right, and then read users — so every org's
  pass generated snapshots for every user on the platform, N-1 of them attributed
  to the wrong tenant, every week.

  Reading users is not automatically a leak — `compute_team_benchmarks` was safe
  without scoping because every *metric* it reads is org-scoped, so a foreign rep
  came back with no activity and was dropped. That is a property of the tables it
  happens to read, not a rule; it scopes its user query anyway.
- **The filter skips relationship *lazy* loads** (`is_relationship_load`), but
  loader criteria injected into a statement do propagate into its `selectinload`
  legs. That is why a foreign group came back reading `member_count: 0`: the
  group row leaked, the companies did not. Don't rely on it for a new surface —
  an association table like `company_group_members` has no `org_id` of its own,
  so what scopes it is the owning row you looked up to reach it.

## `scalar_one_or_none` asserts a uniqueness the schema rarely promises

This has now cost four times, in unrelated code, the same way every time: the
query reads as the careful option, and is in fact an assertion that raises
`MultipleResultsFound` — a 500 — the moment a second row exists.

- `contacts.email` has no unique constraint and the scraper re-creates rows, so
  one duplicate contact broke **every unsubscribe link** (see the opt-out
  section). The same duplicate also aborted the **inbox sync**, where the
  failure is worse than loud: a mailbox that stops ingesting replies looks
  exactly like a mailbox with no replies.
- `company_groups.name` has no unique constraint and `POST /groups` accepts any
  name, so a tenant that created "Aerospace" twice got a 500 from **all three**
  `/groups/auto-group-by-*` endpoints, with no way out that the UI explains.
- `gmail_accounts` is unique on `email` and `google_sub` and nothing else, so
  `get_active_account` broke on the **second connected mailbox** — while the
  function directly below it returns a *list* of connected accounts, because
  multi-mailbox sending is the whole design. `welcome_email` and `notifications`
  call it with no rotation in front, so for them it was unconditional.
- `companies` is unique on `domain`, not `name`. Two firms sharing a name is
  ordinary B2B data, and it 500'd **quick-add** — the business-card scanner's
  entire job.

Before writing one, check for the constraint. If there isn't one, take
`.order_by(...).limit(1)` and `.scalars().first()` — and order by something
stable, or repeat runs pick different rows and the work scatters between
duplicates instead of accumulating in one. For a pure existence check,
`select(Model.id)…limit(1)` is both correct and cheaper.

Two things this experience is worth:

**Order by more than a timestamp.** `created_at` is a `server_default`, so rows
written in one transaction — which is how duplicates usually arrive — share a
reading and tie. Without a second key the database may return either, which is
the scattering above with extra steps. `(<timestamp>, id)`.

**Reviewing for this does not work, so it is checked mechanically.** The unsafe
call is character-for-character identical to the safe one; what separates them
lives in another file, in the model's constraints.
`tests/test_single_row_assertions.py` walks every `scalar_one_or_none` in `app/`
and requires each to be covered by a `.limit(1)`, by an equality filter on
genuinely unique columns, or by a reasoned entry in its `REVIEWED` map. It
carries self-tests that plant a violation and assert the scan catches it — a
checker matching nothing passes forever and reads as coverage.

An "existence check" is the most common false sense of safety here: code that
asks "is there already one?" and enforces uniqueness itself. It is not a
constraint. It cannot stop a race, it does not bind other write paths, and the
row it fails on is the one it was written to detect.

## The rate-limit key is the caller, and behind a proxy that takes saying so

`request.client.host` is the peer that opened the socket, which is the caller
only when nothing sits in front of the app. In production nginx proxies to
`127.0.0.1:8001` — a Docker published port — so the peer every request presents
is the bridge gateway. Uvicorn ships a `ProxyHeadersMiddleware` that would fix
this and does not: its `forwarded_allow_ips` defaults to `127.0.0.1`, the
gateway is not that, so it declines to rewrite anything and says nothing.

That is not a loose limit, it is an inverted one. `AUTH_RATE_LIMIT` is
10/minute, and on a single shared key that is ten logins a minute **for the
whole platform**, spendable by any unauthenticated stranger. A control installed
to stop credential stuffing became the cheapest denial of service the API
offers. Note the shape: the limiter was working exactly as designed and the
tests were green, because what was wrong was the identity it was counting.

`TRUSTED_PROXY_HOPS` (set to `1` in `docker-compose.prod.yml`) is how the app is
told what is in front of it. `X-Forwarded-For` cannot simply be trusted — it is
attacker-controlled, and reading its leftmost entry lets anyone mint a fresh
identity per request, removing the limit entirely rather than sharing it. What
*is* trustworthy is position: each proxy appends the peer it saw, so with one
hop the **last** entry is the one nginx wrote and no client can reach. Hence a
count rather than a boolean, a default of 0, and a clamp so an over-configured
count cannot walk left into client-supplied entries.

Two related traps in slowapi, both of which 500 the endpoint:

- **`headers_enabled` must stay False.** It injects `X-RateLimit-*` by reaching
  for a `response: Response` kwarg the endpoints don't declare.
- **`swallow_errors` is necessary and not sufficient.** When storage raises,
  slowapi catches it and returns without setting `request.state.view_rate_limit`
  — which its endpoint wrapper then reads unconditionally, before any
  `headers_enabled` check. `RateLimitStateMiddleware` pre-seeds the attribute.
  Without it a Redis blip returns a clean 401 for every *rejected* login and a
  500 for the correct password: failures fine, success broken, and invisible to
  any test that only checks that bad credentials are refused.

`conftest` sets `limiter.enabled = False` for the whole session, which is the
right default — it stops unrelated tests tripping a 429 — and it means a
rate-limit test that forgets to re-enable it asserts nothing while looking
exactly like one that works. Anything testing limiter behaviour needs the
`live_limiter` fixture in `tests/test_rate_limit_storage_outage.py`, and needs a
control proving the limiter was on.

## The Apprend org id is copied into every migration, so it is checked

Migrations inline `APPREND_ORG_ID = "a9910000-0000-4000-8000-000000000001"`
rather than importing `app.org_context.APPREND_ORG_ID`, and should keep doing
so: a migration describes the schema at its own revision and must not change
meaning when application code is refactored. The cost is a literal copied a
dozen times with nothing comparing the copies, and two of them read
`00000000-0000-0000-0000-000000000001` — an id that looks exactly like what a
first-party tenant would have and matches no row in `organizations`.

`tests/test_migration_org_id_constant.py` pins every copy. It is worth having
because the two failure modes look nothing alike:

- **111** backfilled `company_groups.org_id` with the bad id and *then* added the
  foreign key, so it aborts mid-deploy on any database that has ever held a
  group — which is every real one.
- **081** used it as a `server_default` on two new, empty tables. Nothing failed,
  and nothing would: ORM inserts are stamped by the `before_flush` hook before
  the default is consulted. The default is what a raw insert, a backfill script
  or a `COPY` falls back on, and there it is an immediate FK violation. Migration
  112 corrects the databases already carrying it; 081's own literal is corrected
  for databases built from scratch. **Both are needed** — editing the old
  migration only fixes future builds, and adding the new one only fixes existing
  ones.

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

## Every do-not-contact writer writes the list, erasure most of all

Five paths suppress an address (unsubscribe link, reply, bounce, complaint, bulk
compliance) and a sixth erases a subject outright. **All of them write a
`SuppressionEntry`**, because the flags live on a contact row and the scraper
makes rows — suppress someone today, rediscover them next month through another
source, and the new row arrives `suppressed=False`.

**`gdpr.erase_subject` is the one to understand.** It sets the flags and then
overwrites `contact.email` with `anonymized+<id>@redacted.invalid`. The flags
survive; the address they were about does not. Before the list write, erasure
did not merely fail to be durable — it *deleted the only link* between the
address and the decision, for the one class of subject where re-contact is a
regulatory violation rather than an annoyance.

- **Record the address the request named**, never one read back off a contact
  row. The write sits above the scrub loop to keep that obviously true rather
  than merely true today.
- **Unconditional, not gated on a contact existing.** An erasure request for an
  address we hold no row for — or whose rows a previous erasure already
  anonymised — still has to stick if that address is ever rediscovered.
- **`reason="erasure"` is its own value** (migration 110 widened
  `ck_suppression_reason`). An operator can re-verify a `bounce` and argue about
  a `manual`; an erasure entry is the one whose removal re-opens legal exposure.
  `DELETE /dialtone/suppression/{id}` refuses it with a 409.

**Un-suppressing must lift something, but only its own.**
`suppression.remove_entry` deletes entries in `REVERSIBLE_REASONS` — `manual`
alone. Without it, `bulk-suppress {suppressed: false}` takes the flag off, leaves
the entry, and the send gate goes on refusing with no explanation. With it
widened, a checkbox round-trip would erase a complaint or an erasure record.
Removing any other reason stays an explicit, per-entry action on the list itself.

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
