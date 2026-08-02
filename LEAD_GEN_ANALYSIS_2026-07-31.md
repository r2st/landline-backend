# Why Landline Is Still Not Generating Leads — Production Investigation, 2026-07-31

**Scope:** live production audit via SSM on `i-05bf9d5a16704ad24` (100.30.136.86), database `landline_db`,
celery/api logs, and the local repo at `ea828f6`.

**TL;DR:** The AI-composition fix (`ea828f6`) is deployed and working as designed — but it was an
*observability* fix, and what it will now make visible is that **AI composition still cannot succeed,
because every LLM provider in production is dead** (OpenRouter free-tier is rate-limited, Anthropic is out
of credits, and no other provider has a key). On top of that, the funnel is failing at every other stage
too: **prospecting has sourced 0 new companies for 8+ straight days**, the contact list is small and
partly junk, sending happens from consumer `@gmail.com` addresses with Gmail already hard-blocking at
least one message, and the platform's one recorded "reply" is actually a bounce notification misclassified
as a reply. **True results to date: 98 cold sends → 47 opens (48%) → 4 clicks → 0 real replies → 0 deals.**
At this volume and quality, zero leads is the statistically expected outcome.

---

## 1. Current state of the system

| Component | Status |
|---|---|
| Containers | `landline-api` healthy, `landline-celery` "unhealthy" (known false positive — `inspect ping` ~34s vs 10s timeout), postgres/redis healthy |
| Deployed code | `ea828f6` — pulled and **rebuilt today at 15:44 UTC** (one minute after the commit) |
| Kill switch | `sending_enabled = true` ("enabled via configure_sender after deliverability check") |
| Danger monitor | Running every 5 min, `overall_status: healthy`, no breaches, no paused accounts |
| Queue sweep | Running every 5 min; today: `dispatched: 0`, ~6 skipped (junk recipients / frequency caps), 3–5 held for optimal send window |

The machinery is on and turning. The problem is what's flowing (and not flowing) through it.

## 2. The funnel, by the numbers

```
Companies in system:        73   (67 stage "scored", 4 engaged, 1 researched, 1 contacted)
Contacts (all with email):  39
Prospect candidates:        59 enrolled / 25 rejected / 1 in review
Active enrollments:         22   (11 currently overdue for their next step)
Sequence sends (all time):  98   (first 2026-06-26, last 2026-07-30 16:02 UTC — none since deploy)
Opens:                      47   (48% — healthy, though Gmail image-proxy inflates this)
Clicks:                      4   (4.1%)
Replies:                     1 recorded — SEE §4: it is a bounce DSN, real replies = 0
Bounces:                     2 recorded (+1 Gmail "Message blocked")
Unsubscribes:                3 — all three are suman.onenet@gmail.com (your own test address)
Deals:                       0
ai_email_drafts rows:        0
```

Nearly all volume went through one sequence: **CNC Machining — Deal Cracker** (81 of 98 sends, 43 opens,
4 clicks, the 1 "reply", 1 bounce). Step cadence: day 0 / +2 / +5 / +9, all `send_condition=always`.

**Volume reality check:** cold-email reply benchmarks are 1–3% on a *good* list. 98 sends over five weeks
would be expected to produce roughly 1–3 replies in the best case. Zero real replies is within noise.
Landline hasn't disproven its outreach — it hasn't run enough of it to test anything.

## 3. Finding A — every AI provider in production is dead (the ea828f6 story continues)

`ea828f6` (deployed today) makes AI-vs-static visible per-send via `ai_generated` /
`compose_reason` on the sent `OutreachEvent` and an `ai_composed_rate` rollup. Good. But the celery log
shows what those markers will say once sends resume — **every AI call currently fails**:

```
LLM provider openrouter model openai/gpt-oss-20b:free returned HTTP 429 — trying the next model
LLM provider chain exhausted (all LLM providers failed; ...) — falling back to Anthropic for this call
POST https://api.anthropic.com/v1/messages "HTTP/1.1 400 Bad Request"
"Your credit balance is too low to access the Anthropic API. Please go to Plans & Billing..."
```

This repeats every hour, all day, for every AI feature (the visible failures are conversation analysis;
composition takes the same path).

Root causes, in order:
1. **Only 2 of 5 possible providers have keys.** The chain default is
   `openrouter,gemini,groq,cerebras`, but the containers carry only `OPENROUTER_API_KEY` and
   `ANTHROPIC_API_KEY`. Gemini/Groq/Cerebras are skipped as unkeyed (by design).
2. **OpenRouter is configured on a free model** (`openai/gpt-oss-20b:free`), which is rate-limited to
   near-uselessness — it 429s on essentially every call.
3. **The Anthropic account has no credits**, so the final fallback 400s.
4. Result: every compose falls to the static template — exactly the failure `ea828f6` was written to
   expose. The fix worked; the underlying outage it exposed was never resolved.

**`ai_email_drafts` is still 0 rows and will stay 0** until at least one provider actually works.

## 4. Finding B — the platform's only "reply" is a bounce, and real replies are zero

The single `replied` OutreachEvent (2026-07-23, attributed to `sales@omniadesignmfg.com`) carries:

```json
{"subject": "Delivery Status Notification (Failure)", "from_email": "mailer-daemon@googlemail.com", ...}
```

It's a mailer-daemon DSN misclassified as a prospect reply. Consequences:
- **Real reply count is 0**, not 1. Every dashboard/report counting replies is over-reporting by the
  only reply it has.
- The enrollment was moved to `status='replied'` (auto-pause on reply), so a **hard-blocked address is
  parked in the success state** instead of being marked bounced/suppressed.
- The earlier email-pipeline audit fixed DSN→reply misfiling in one path; this event shows a path
  (likely `reply_tracker`'s thread scan) that still misfiles. Worth a targeted fix + a backfill that
  reclassifies this event.

Related: Gmail's mailer-daemon messages on 07-20/07-21 include **"Message blocked — your message to
sales@omniadesignmfg.com has been blocked"**. That is a Gmail policy rejection of your mail, not a bad
address — a deliverability red flag at only ~100 sends of volume.

## 5. Finding C — the top of the funnel has been completely dry for 8+ days

Every `prospecting_run` from **Jul 24 → Jul 31: `companies_found: 0`**. The run results show exactly why:

- **Apollo: HTTP 403** — `"The api/v1/mixed_companies/search API is not included in your Free plan…
  even with a master key."` Apollo cannot source anything on the current plan. It still burns a credit
  per attempt, daily.
- **Google Places: working but exhausted** — the run queries the *same search every day*
  (`places_query: "CNC Machining"`, `places_location: "US"`) and now gets
  `places_duplicates: 60, places_candidates: 0`. The pool for that one query is fully harvested.
  No query rotation, no metro-level variation, no other verticals queried — despite four other active
  vertical sequences (Injection Molding, Metal Stamping, Food & Beverage) sitting idle.
- **Hunter verification is starved:** `HUNTER_DAILY_CREDIT_BUDGET=3` and the log shows
  `verify_pending_contacts: daily Hunter credit budget exhausted` on every sweep. Three verifications
  per day cannot support even the current trickle.

With 22 active enrollments (several junk — see §6) and 0 new companies/day, the send queue will be fully
drained within days and outreach volume goes to literal zero.

## 6. Finding D — list quality: small, partly junk, and self-polluted

Of 39 contacts / 22 active enrollments:
- **3 enrollments target `suman.onenet@gmail.com`** — your own test address (it's also generating the
  "too soon to email again" skips and all 3 "unsubscribes", which pollute every metric).
- **2 enrollments target `…@sentry.wixpress.com`** — Sentry ingest artifacts scraped off Wix sites; the
  sweep skips them every 5 minutes as "invalid recipient domain" but they stay enrolled forever.
- Most of the rest are **role addresses** (`info@`, `sales@`) — the lowest-reply-rate class of cold
  targets; decision-maker personal addresses are largely absent because Places yields no emails
  (`places_with_email: 0`) and the pattern-generator/Hunter budget can't fill the gap.

## 7. Finding E — deliverability: sending consumer Gmail, no DMARC on the real domain

Sender fleet (all `sending_enabled=true`): `sumaninster15/16/17/18@gmail.com`, `suman.onenet@gmail.com`,
and **one** business address, `suman.das@apprend.tech`.

- **Cold B2B outreach from raw `@gmail.com` addresses** is both a spam-classifier signal and a
  credibility killer for manufacturing buyers. Gmail's bulk-sender rules (enforced since 2024) make
  unauthenticated cold volume from consumer accounts progressively deliverable to spam only. The
  "Message blocked" in §4 is the early warning.
- **`apprend.tech` domain health: 62/100, band `at_risk`** — SPF pass, DKIM pass, but **no DMARC record
  at all**. The checker even supplies the fix:
  `TXT _dmarc.apprend.tech = "v=DMARC1; p=none; rua=mailto:dmarc@apprend.tech; fo=1; pct=100"`.
- Warm-up plans exist for all 6 accounts, danger thresholds are sane (5% bounce / 0.1% complaint), and
  the earlier audit's bounce-ingestion work is feeding the monitor. The *infrastructure* is fine; the
  *identity* you're sending from is the problem.

## 8. Finding F — ICP/company scoring is still zeroing out its own best-fit industry

Tonight's re-scoring sweep scored ~20 CNC machine shops — the exact ICP — like this:

```
Scored company ZYCI CNC Machining: fit=36 [industry=0, size=5, location=20, completeness=11, ...]
tier=tier_3_prospect
```

**`industry=0` for a company literally named "CNC Machining"** — the same taxonomy mismatch fixed for
*prospect candidates* in `183d5a9` (search_query-as-industry-signal) evidently does not cover the
*companies-table* scorer that drives routing/tiering. Everything lands in `tier_3_prospect` at fit 28–56,
so nothing ever qualifies for higher-touch treatment, and 67 of 73 companies sit parked in stage
"scored". (Auto-enroll at threshold 65 works off the candidate scorer, which was fixed — that's why
enrollments happened at all.)

## 9. Finding G — the new tracking API works but nothing uses it

Commit `3603678` (`GET /leads/stats`, `/leads/recent`, `/dialtone/status`) is deployed and correctly
reuses the EmailSend+OutreachEvent union. **API access logs show 0 calls to any of the three endpoints
in the last 24h** — no external poller is wired up yet. Also note the numbers it reports will include the
phantom "reply" from §4 until that's reclassified.

## 10. Why no sends today (it's not a new failure)

Zero sends on 07-31 is the queue behaving normally on a starved pipeline: of 22 active enrollments, the
due ones were either held for the recipient's optimal send window / outside business hours (6 skipped,
3–5 held per sweep) or skipped as junk (sentry domains, own-address frequency cap). It will trickle out
sends tomorrow — and then run dry, per §5.

---

## 11. Recommendations

### Do today (unblocks everything else)

1. **Restore one working LLM provider.** Cheapest path: add a `GROQ_API_KEY` and/or `GEMINI_API_KEY`
   (both have workable free tiers with real rate limits) to `.env.production` — the chain will pick them
   up with no code change. Alternatively fund Anthropic credits, or switch the OpenRouter model off
   `:free`. Then verify with `GET /api/v1/ai/providers` and watch `ai_composed_rate` climb on the funnel
   report. Until this is done, ea828f6 can only document the failure, not fix it.
2. **Publish the DMARC record for apprend.tech** (exact TXT in §7). Five-minute DNS change; moves the
   domain out of `at_risk` and is a precondition for Gmail/Yahoo bulk delivery.
3. **Clean the enrollment list:** cancel the 2 `@sentry.wixpress.com` enrollments (and suppress the
   addresses), remove the 3 self-enrollments of `suman.onenet@gmail.com` from active sequences (keep one
   as a seed/canary *outside* metrics), and reclassify the phantom "reply" (the enrollment currently in
   `replied` should be `bounced`, and the DSN misfile path in `reply_tracker` needs the same fix the
   audit gave the other path).

### This week (rebuild the funnel)

4. **Un-starve prospecting.** Apollo search is a paid feature — either pay for it or stop querying it
   (it burns a credit per failed daily attempt). For Places, rotate queries: iterate
   vertical × metro (e.g. "injection molding" / "metal stamping" / "precision machining" ×
   top-30 manufacturing metros) instead of one national query forever. The 60-duplicate wall is
   per-query, not a market limit. Add the other four vertical sequences' industries to the rotation —
   the sequences already exist.
5. **Fix the companies-table scorer's industry taxonomy** (§8) the same way `183d5a9` fixed the
   candidate scorer, so CNC shops stop scoring `industry=0`. Otherwise tiering, routing, and any
   "qualified lead" definition built on fit score are permanently deflated by ~30–40 points.
6. **Raise the Hunter budget** (3/day → at least 25/day to match `prospecting_daily_max`), and lean on
   the DIY pattern-generator + MX-check layer first so Hunter credits go only to pattern-ambiguous
   domains. Target *named people* (owner/GM/ops manager) over `info@`/`sales@` wherever possible —
   role-address-heavy lists are a large part of the 0% reply rate.

### This month (make it worth scaling)

7. **Move sending identity off consumer Gmail.** Buy 2–3 look-alike domains (e.g. `apprendtech.io`,
   `getapprend.com`), set SPF/DKIM/DMARC from day one, create 2–3 mailboxes each, and run the existing
   per-account warm-up ramp for 3–4 weeks before real volume. Retire `sumaninster1x@gmail.com` from cold
   outreach entirely (keep them as reply/monitoring inboxes if useful). Never send cold from the root
   `apprend.tech` — protect it.
8. **Personalization > sequence count.** There are 10 sequences for ~40 contacts. Consolidate: one
   active sequence per vertical, and spend the effort on AI-composed step 1 (once providers work,
   research briefs already exist and `ai_compose` will use them). Keep the current voice — 48% opens
   says the subject lines work; 4 clicks / 0 replies says the *body/CTA* is what needs the AI
   personalization. Consider a softer CTA (question about their scrap/quoting process) over a demo link
   for step 1–2.
9. **A/B properly once volume exists.** The A/B engine is wired and judged on its declared metric;
   subject tests on `opened` need hundreds of sends per variant, reply-metric tests need thousands.
   Don't start tests before the send volume can conclude them — at 98 sends/5 weeks every test so far is
   noise.
10. **Wire a poller to the tracking API** (§9) — a daily cron hitting `/leads/stats` +
    `/dialtone/status` that alerts on `sends_today == 0`, `companies_found == 0`, or
    `ai_composed_rate == 0` would have caught *every one* of findings A, C, and the June→July send gap
    within a day. The API exists; it needs one consumer.

### Realistic expectations

With working AI composition, 2–3 warmed domains, query-rotated sourcing at ~25 new companies/day, and
verified named contacts, a defensible steady state is **500–1,000 quality sends/month → 5–30 replies →
2–8 conversations → first deals**. The current configuration (98 sends in 5 weeks, static copy, Gmail
senders, dry pipeline) cannot produce leads at any setting of the scoring thresholds — the thresholds
were never the binding constraint; **supply (companies, contacts, working AI, sending identity) is**.

---

## Appendix — evidence index

- AI chain failure: `docker logs landline-celery` 19:46–21:46 UTC 07-31 (429 → chain exhausted → Anthropic 400 credit balance)
- Provider keys present: container env — `OPENROUTER_API_KEY`, `ANTHROPIC_API_KEY` only
- Deploy freshness: images built 2026-07-31 15:44:27 UTC; `git log` on box at `ea828f6`
- Funnel counts: `outreach_events` group-by; `email_sends`; `prospect_candidates`; `deals`
- Phantom reply: `outreach_events.metadata` where `event_type='replied'` (mailer-daemon DSN)
- Gmail block: `email_replies` 2026-07-20 ("Message blocked … sales@omniadesignmfg.com")
- Dry prospecting: `prospecting_runs.results` (apollo 403 verbatim, `places_duplicates: 60`, `places_with_email: 0`)
- Domain health: `domain_health` — apprend.tech 62 `at_risk`, DMARC absent (recommended record included)
- Scoring: celery log 22:16 UTC — 20 CNC companies scored `industry=0`, tier_3
- Tracking API: 0 matches for `leads/stats|leads/recent|dialtone/status` in 24h of api logs
