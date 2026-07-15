# Autonomous Lead Generation

Status: **implemented** — this document is the design + gap analysis for the
`feat/autonomous-lead-gen` work. It is honest about what Landline **already**
does (a lot) and specifies only the concrete pieces this change adds.

## 1. Context: most of this already exists

Landline shipped four AI phases before this change. A large fraction of the
"autonomous lead gen" surface area is already live on `main` (migration 055).
Rebuilding it would duplicate code and threaten the 460+ test suite, so this
change is deliberately **surgical**: fill the real gaps and wire up code that
was written but never consumed.

| Requested capability | Already in the codebase |
|---|---|
| Find/qualify manufacturing companies by vertical | `services/autonomous_prospector.py` (feature 3.1) — learned **ICP**, Apollo sourcing, 0-100 qualification, auto-enroll / review-queue / reject, daily guardrails. Scheduled daily (`tasks/prospecting.run_prospecting_agent`) + weekly ICP relearn. |
| Apollo / scraper enrichment | `services/enrichment.py` (Apollo org + people), `scrapers/{thomasnet,census}.py`, `tasks/enrichment.py` (hourly auto-enrich, 30-min auto-score). |
| Lead scoring (firmographic + behavioural) | `services/scoring.py` — industry/size/location + engagement + **signal-recency decay** + **velocity multiplier** + intent + ABM tier + score-history. |
| ICP matching & fit | `models/prospecting.py:IcpProfile` + `AutonomousProspector.qualify`. |
| Warm-up ramp / daily caps / send window | `services/send_limiter.py` — global warm-up weeks, per-day cap, per-contact monthly cap + min-gap, business-hours/weekday window. |
| Sender reputation & throttling | `services/deliverability_optimizer.py` (feature 3.3) — 7-day bounce/complaint/open → reputation 0-100 + `throttle_factor` + `SenderHealth`, hourly beat task. |
| Spam-content scanning | `deliverability_optimizer.scan_content[_heuristic]` — trigger phrases, caps ratio, `!` count, link count, unsubscribe presence, optional AI. |
| Sender rotation | `services/email_dispatch.py` — LRU round-robin across Gmail + SMTP, per-account `daily_send_count`. |
| Unsubscribe / List-Unsubscribe | `email_dispatch` footer + `gmail_sender` `List-Unsubscribe` + `List-Unsubscribe-Post` headers; `routers/tracking.py` one-click unsubscribe. |
| Multi-channel cadence steps | `models/sequence.py` — `SequenceStep.channel` (email/call/linkedin/video/direct_mail), `cadence_pattern`. |
| Follow-ups / stale-lead chase | `tasks/followups.py`, `tasks/outreach.process_outreach_queue`, `services/sequence_optimizer.py` (AI skip/delay/switch-channel/rewrite). |
| Auto-pause on reply | `services/reply_tracker.py` — reply → enrollment `replied`, company → `engaged`. |
| A/B testing + winner selection | `services/ab_testing_service.py` — z-test significance, UCB1 bandit, auto-declare winner. |

## 2. The real gaps this change closes

1. **Per-account throttle is dead code.** `send_limiter.account_capacity()`
   computes a deliverability-aware per-account cap (base × `throttle_factor`),
   but **nothing calls it** — rotation and the outreach queue ignore it, so a
   throttled/critical account still sends at full volume. → *wire it in, plus a
   per-account warm-up ramp keyed off `connected_at` so brand-new mailboxes ramp
   independently of the global warm-up date.*
2. **No SPF / DKIM / DMARC verification.** Requested explicitly; absent
   (`dnspython` isn't even a dependency). → *add a DNS auth checker + endpoint.*
3. **No bounce → contact suppression.** A send that bounces sets the
   `EmailSend`/enrollment to `bounced`, but the **contact is never suppressed**,
   so the next sequence/enrollment emails the dead address again. Hard vs soft
   isn't distinguished. → *classify bounces, suppress hard bounces, track soft.*
4. **Send window is global only.** `within_send_window()` uses one configured
   timezone; a contact's own timezone is ignored. → *per-contact timezone.*
5. **No lead-quality reporting.** No grade distribution / qualified-vs-
   disqualified / source-quality report or A-D letter grade on a lead. → *add a
   grade helper, a `/reports/lead-quality` endpoint, and a weekly beat summary.*
6. **No declarative conditional branching.** The AI optimizer adapts a step, but
   there is no rule-based "only send this step if the prior one was not opened /
   opened-but-not-replied". → *add `SequenceStep.send_condition`.*

## 3. Architecture

All additions are network-free and fail-open, matching the existing services'
defensive style (a missing dep / failed DNS lookup / absent `SenderHealth` row
never blocks a send or crashes a request).

### 3.1 Per-account send enforcement (`email_dispatch` + `send_limiter`)
- `send_limiter.account_warmup_limit(account)` implements a **per-account
  warm-up** ramp (week1/2/3 caps) keyed off `connected_at` age; returns `None`
  once warmed or when `account_warmup_enabled` is False.
- `send_limiter.account_capacity()` now folds warm-up into the effective cap:
  `effective = min(reputation_throttled_cap, warmup_cap)`, and is exposed for
  reporting.
- `email_dispatch._filter_by_sender_health` (the existing rotation gate, feature
  3.3) is extended to exclude accounts over this **effective** cap — so a
  brand-new mailbox is held to its warm-up volume even before any SenderHealth
  assessment exists — while staying fail-open for mocks / missing data.
  `get_next_rotation_account` keeps preferring the filtered pool and never halts
  sending entirely (falls back to the raw LRU pick when all are capped).

### 3.2 Domain authentication (`services/domain_auth.py` + deliverability router)
- `check_spf/check_dmarc/check_dkim/check_domain_auth(domain, selectors)` using
  `dns.resolver`. Each returns `{present, valid, policy, record, issues}`.
- Overall verdict `pass|warn|fail` + recommendations.
- Graceful degradation: if `dnspython` is not installed or a lookup errors, the
  record is reported `status="unknown"` with a reason — never an exception.
- Endpoint: `GET /api/v1/deliverability/domain-auth?domain=&dkim_selector=`.

### 3.3 Bounce handling (`services/bounce_handler.py`)
- `classify_bounce(status_code, diagnostic)` → `hard|soft` (5.x.x / "user
  unknown" / "no such user" / "mailbox not found" → hard; 4.x.x / "full" /
  "temporarily" / "timed out" → soft).
- `record_bounce(db, email, kind, diagnostic)` increments `Contact.bounce_count`,
  stamps `last_bounce_type` / `last_bounced_at`; a **hard** bounce (or
  `soft_bounce_suppress_threshold` soft bounces) sets `Contact.unsubscribed =
  True` — the existing do-not-email gate honoured by `send_limiter`, consent,
  and stale-lead follow-ups — and pauses the contact's active enrollments to
  `bounced`.
- Exposed via `POST /api/v1/deliverability/bounce` for webhook/manual feeds; the
  outreach queue also records a bounce when a send returns `status="error"` with
  a hard SMTP code.

### 3.4 Per-contact timezone (`send_limiter` + `Contact.timezone`)
- `Contact.timezone` (IANA name, nullable).
- `within_send_window(tz_name=None)` uses the contact's tz when provided, else
  the global default. `process_outreach_queue` passes the contact tz so a lead
  in `America/Los_Angeles` isn't emailed at 5 AM their time.

### 3.5 Lead-quality reporting (`scoring.grade_for_fit` + reports router + task)
- `grade_for_fit(fit)` → `A|B|C|D|F` (A ≥ 80, B ≥ 65, C ≥ 50, D ≥ 35, else F);
  surfaced on lead responses and used by the report.
- `GET /api/v1/reports/lead-quality?days=30` → grade distribution (latest score
  per company), qualified vs disqualified counts, new leads by source, average
  fit, and the last prospecting run summary.
- `tasks/followups.generate_weekly_lead_quality_report` (Mondays) logs/notifies
  the summary.

### 3.6 Conditional step branching (`SequenceStep.send_condition`)
- Column `send_condition` ∈ `always | if_not_opened | if_opened_no_reply`
  (default `always`, so every existing step is unchanged).
- `send_sequence_step` evaluates the condition against the **prior** step's
  engagement for that contact; if unmet it advances past the step without
  sending (recorded as `skipped_by_condition`).

## 4. Database changes — migration `056_autonomous_lead_gen`

| Table | Column | Type | Default |
|---|---|---|---|
| `contacts` | `timezone` | `varchar(64)` | NULL |
| `contacts` | `bounce_count` | `integer` | `0` |
| `contacts` | `last_bounce_type` | `varchar(10)` | NULL |
| `contacts` | `last_bounced_at` | `timestamptz` | NULL |
| `sequence_steps` | `send_condition` | `varchar(20)` | `'always'` |

All nullable / defaulted → safe, reversible, no backfill.

## 5. New config (`app/config.py`)
```
account_warmup_enabled: bool = True
account_warmup_daily_limit_week1: int = 10
account_warmup_daily_limit_week2: int = 25
account_warmup_daily_limit_week3: int = 40
soft_bounce_suppress_threshold: int = 3      # N soft bounces → suppress
dkim_default_selectors: list[str] = ["google", "default", "selector1", "selector2", "k1"]
```

## 6. API summary
- `GET  /api/v1/deliverability/domain-auth?domain=&dkim_selector=`
- `POST /api/v1/deliverability/bounce` `{email, status_code?, diagnostic?}`
- `GET  /api/v1/reports/lead-quality?days=30`
- lead responses gain `grade` (derived, non-persisted).

## 7. Testing
New test files, all offline (DNS monkeypatched, no external calls):
`test_domain_auth.py`, `test_bounce_handler.py`, `test_account_send_limits.py`,
`test_contact_timezone_window.py`, `test_lead_quality_report.py`,
`test_conditional_steps.py`. The full existing suite must stay green.

## 8. Provider note
AI features continue to use the already-configured Anthropic Claude API via
`services/ai_service.py` (unchanged). Nothing here calls an LLM directly; the
new services are deterministic.
