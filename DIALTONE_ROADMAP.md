# Dialtone — Beyond the Audit: Prioritized Roadmap

**Date:** 2026-07-17
**Scope:** `landline-backend` @ `944e16f`, `landline-frontend` @ `8a7f738` (both `origin/main`, both deployed).
**Status:** Proposal. Nothing here is implemented. Needs approval.

---

## How to read this

Every claim below was checked against `origin/main` with file:line evidence. That is not boilerplate: the previous audit (`landline-frontend/DIALTONE_FRONTEND_IMPROVEMENTS.md`) was written against a checkout 24 commits stale and its headline — "there is no `/api/v1/ai-drafts` endpoint" — was simply false. Where I could not verify something, I say so.

**The headline finding is the opposite of what a feature roadmap usually says.** Dialtone is not missing capability. It has a striking amount already built — intent monitoring, competitive intel, A/B testing, send-time optimization, a multi-channel step model, real HubSpot/Salesforce clients. The gaps that matter are:

1. **The auto-pause rail cannot fire — on anything** (P0-1). Not a missing feature: three metrics reading three columns that nothing writes. It reports "Healthy" unconditionally.
2. **Built features nothing acts on** — intent signals are detected and then sit in a list.
3. **No closed loop** — nothing can currently tell you whether any of this produces revenue.

The best-value work here is mostly *connecting things that already exist*, not building new ones. I would resist the instinct to add LinkedIn until P0/P1 are done, for reasons in **P3-1**.

**P0-1 was the live finding and is now fixed** (`bb60fd6`, deployed): the safety system this product's launch readiness was argued on had never worked. Its complaint half is still blind — see **P1-4**.

---

## P0 — The auto-pause rail cannot fire. At all.

> **✅ FIXED 2026-07-17 in `bb60fd6`, deployed.** The rail now unions the three places outbound mail actually lives (`EmailSend` + `OutreachEvent` + `BounceRecord`) and trips on Dialtone's own bounces. The acceptance test below is committed as
> `tests/test_danger_monitor_populations.py::test_total_bounce_catastrophe_trips_the_breaker`
> and was verified by mutation — it fails against the old aggregation.
>
> Confirmed against live prod: the rail's denominator went from **14 → 31 sends**, the 17 new ones being Dialtone's sequence sends, which it had never been able to see.
>
> **Root cause was deeper than this section said.** It is not only that nothing writes `EmailSend.status = "bounced"` — the sequence path writes **no `EmailSend` row at all** (`tasks/outreach.py:562`). The numerator *and the denominator* were zero for autonomous sends.
>
> **Still open:** `complaint_rate` remains structurally 0. Nothing writes a complaint status anywhere and Gmail gives no complaint signal without a feedback loop — that needs ingestion, not a query change. Tracked in **P1-4** below. The section is kept intact as the record of the finding.

**Severity: critical. Dialtone's only automatic brake is decorative — every metric it evaluates reads a column nothing ever writes.**

`danger_monitor.py:241` computes the metric the whole rail exists for:

```python
bounces = (await db.execute(base.where(EmailSend.status == "bounced"))).scalar() or 0
...
bounce_rate = round(bounces / sent, 6) if sent else 0.0
```

**Nothing in the codebase ever sets `EmailSend.status = "bounced".`** Verified by exhaustive grep across `app/`. The only two writers of that literal are:

- `bounce_handler.py:104` — `enr.status = "bounced"` — that's an **OutreachEnrollment**, not the send.
- `outreach.py:650` — `enrollment.status = "bounced"` — also the enrollment.

`EmailSend.status` only ever takes `sent` / `pending` / `scheduled` / `skipped` / `error` (`email_history.py:747`, `email_templates.py:363,380,407,416,441`). A bounce arrives asynchronously as a DSN in the mailbox — `send_outreach_email` cannot return "bounced", so nothing writes it.

### What this means

| Consumer | Reads | Actual value | Consequence |
|---|---|---|---|
| `danger_monitor.py:241` | `bounce_rate` | **always 0.0** | Auto-pause can never trigger on bounce rate |
| `danger_monitor.py:253` | `hard_bounce_count` | **always 0** | Auto-pause can never trigger on hard bounces |
| `deliverability_optimizer.py:61` | bounce count | always 0 | Sender reputation scoring is blind to bounces |
| `reports.py:389`, `email_templates.py:711` | bounced counts | always 0 | Every bounce number shown to a user is 0 |

**All three metrics are dead, not two.** I checked every assignment of every relevant literal across `app/`:

| Status literal | Writers found | Where |
|---|---|---|
| `"bounced"` | 2 | `outreach.py:650`, `bounce_handler.py:104` — **both on `OutreachEnrollment`, neither on `EmailSend`** |
| `"complained"` | **0** | — |
| `"complaint"` | **0** | — |
| `"spam"` | **0** | — |

So `complaint_rate` is `0/sent` forever too. `_COMPLAINT_STATUSES` (`danger_monitor.py:60`) and its twin in `deliverability_optimizer.py:63` are read-only vocabulary describing states the system cannot enter.

**The conclusion is blunt: the auto-pause rail has never been capable of firing, and cannot fire today.** `evaluate()` will return `healthy` on every call regardless of what the mailbox is doing. Dialtone sends cold email unattended with no working automatic brake — the kill switch (manual) and the warm-up cap are the only real limits.

This is the rail whose UI I built last session, whose Resume button I carefully routed through an acknowledged re-check with a 409 guard. That machinery is correct and entirely downstream of three numbers that are always zero. **A dashboard I built reports "Healthy" for a system that cannot report anything else** — which makes it worse than no dashboard, by exactly the argument used to fix the fabricated Settings badges.

### Proof — not an inference

I did not want to assert this from grep alone, so I ran it. 100 sends, every one hard-bounced through the real production path (`bounce_handler.record_bounce`, the same call `bounce_ingestion` makes), contacts suppressed (`suppressed=1` in the logs), then asked the monitor:

```
100/100 sends hard-bounced.
overall_status : healthy
breach         : False
bounce_rate          observed=0.0  threshold=0.05   status=pass
complaint_rate       observed=0.0  threshold=0.001  status=pass
hard_bounce_count    observed=0.0  threshold=5.0    status=pass
```

**Total deliverability catastrophe. Rail says healthy.** Reproduction (drop into `tests/`, adapt the assertion once fixed — it currently asserts the *broken* behaviour, so it must not be committed as-is):

```python
# 100 sends, all hard-bounced via the production path
for i in range(100):
    addr = f"dead{i}@gone-corp.com"
    c = Contact(company_id=company.id, first_name=f"D{i}", email=addr, source="test")
    db.add(c); await db.flush()
    db.add(EmailSend(contact_id=c.id, to_email=addr, subject="hi",
                     status="sent", sent_at=now))
    await db.flush()
    await record_bounce(db, addr, status_code="5.1.1",
                        diagnostic="550 5.1.1 mailbox does not exist", kind="hard")
await db.commit()

report = await get_danger_monitor().evaluate(db)
assert report.breach is False          # ← today. After the fix this must be True.
assert report.overall_status == "healthy"
```

**The acceptance test for P0-1 is that inverting those two assertions makes it pass.**

**Note:** `944e16f` (this session) fixed a *different* bug — `bounce_ingestion` read `account.access_token`, which does not exist, so the sweep raised `AttributeError` every 15 minutes and ingested nothing. That fix makes the sweep *run*, and the proof above shows why it isn't sufficient: `record_bounce` worked perfectly here — contacts suppressed, enrollments handled — and the rail still cannot see it, because the bounce never reaches `EmailSend.status`.

### The fix, and the decision it needs

`record_bounce(email, ...)` receives an address, not a send. Linking it back needs a choice:

- **(a) Match on recipient + window** — update `EmailSend` rows where `to_email == email` and `sent_at` within N days. Simple; risks mislabelling an older send when a contact is mailed repeatedly.
- **(b) Match on `provider_message_id`** — parse the DSN's original-message headers and join exactly. Correct; depends on the DSN carrying it, which not all providers do.
- **(c) Don't touch `EmailSend`; change the monitor** to count `BounceRecord` (which `bounce_ingestion` already writes, `models/bounce_record.py`) joined to sends by recipient. Keeps the send record honest — a send genuinely *was* sent — and puts the bounce where it belongs.

**I'd recommend (c) with (b) as an enrichment.** "Status = bounced" conflates *what we did* (sent) with *what happened after* (it bounced), which is why this drifted apart in the first place. But this is a real design fork and I'm not making it unilaterally on a safety path.

**Effort:** small-to-medium. **Value:** restores the only automatic brake on a system that sends cold email unattended.

---

## P0-2 — Nothing measures whether Dialtone works

There is no path from a won deal back to the sequence, step, or campaign that produced it. `reports.py` has retention, revenue-pipeline, funnel, heatmap, top-clients, email-performance — but no attribution join. `Deal` has no `source_sequence_id`; grep for `attribution` across `app/` returns nothing.

So the questions the business actually has — *is the agent making money? which sequence? is the ICP right?* — are unanswerable, including by the ICP learner itself, which fits on won deals (`autonomous_prospector.py:86`) but cannot tell a deal Dialtone sourced from one that walked in.

This is why I rank it P0 alongside a broken safety rail: without it, every item below is a guess, and you cannot rank *any* of them by value. It also compounds — attribution needs data collected from the moment of send, so every week without it is a week of history you can't reconstruct.

**Sketch:** stamp `Deal` (or `Company`) with the originating `prospect_candidate_id` / `sequence_id` at enrollment; add `GET /reports/attribution` returning won revenue by sequence/source/step. The provenance endpoint added last session (`prospecting/companies/{id}/provenance`) already proves the candidate→company link exists — this extends it forward to revenue.

**Effort:** medium. **Value:** the highest on this list. It's the difference between operating this agent and guessing at it.

---

## Status check, 2026-08-02

This document is from 2026-07-17 and several sections below now describe code
that exists. Re-verified against `main` before writing this block, because a
session that trusts the P1 list as written spends its first hour rediscovering
finished work — which is what happened on 2026-08-02.

| Item | Status on `main` |
|---|---|
| **P1-1** boost fit on a live signal | ✅ `scoring._intent_signal_bonus` — active signals add to `fit_score`, capped. |
| **P1-1** inject the signal into copy | ✅ `services/personalization.py::_add_intent` — signal *and* its `outreach_angle` reach the prompt, recorded on the draft/send. |
| **P1-1** trigger outreach on signal arrival | ✅ `services/signal_enrollment.py` now reads **both** `CompanyNews` and `IntentSignal`. Open question 3 below is answered: a hot signal does **not** bypass human review — it is evidence about timing, and skipping review is a claim about the copy. |
| **P1-4** complaint ingestion | ✅ `services/complaint_ingestion.py` + `complaint_handler.py`, migration 096. |
| **P2-3** engagement-driven delays | ✅ `services/adaptive_delay.py`, applied through `services/step_delay.py`. |
| **P0-2** attribution | ✅ campaign analytics + `services/campaign_dashboard.py`. |
| **P1-3** `cadence_pattern` dead config | ❌ Still read by nothing. |
| **P2-1** competitor context at compose time | ❌ Still not joined at outreach time. |
| **P2-2** CRM pull | ❌ Still push-only, contacts-only. Still blocked on "is a CRM actually in play". |

**Added since, and not on this roadmap:** the outreach queue was unordered
(`services/queue_priority.py`). It selected every active enrollment with no
`ORDER BY` and then spent a finite daily cap walking that list, so a sequential
scan's stable row order decided who got sent — the same enrollments losing every
day rather than taking turns. Ranking is value (engagement, invested touches,
fit, live intent) plus an *unbounded* wait term, so nothing starves. The
portfolio-level `capacity` alert on the dashboard reports the shortfall that
causes it, which otherwise surfaced as every campaign looking stalled at once.

---

## P1 — Connect what is already built

### P1-1. Intent signals are detected and then ignored

`intent_monitor.py` is real and good: Google News RSS searches per company for `hiring`, `funding`, `expansion` and similar, scored for relevance/urgency (`intent_monitor.py:34-51`, weights at `:38-48`). `IntentSignal` rows are stored, exposed at `/intent/signals`, and can be marked actioned.

**Nothing consumes them.** Grep for `IntentSignal`/`intent` in `tasks/outreach.py` and `services/autonomous_prospector.py` returns only unrelated reply-intent hits. A company that just raised a Series B and is hiring a Plant Manager — the single strongest buying signal this system can detect — is treated identically to a cold name from a Places search.

**Proposal:** make a fresh, high-urgency signal do something. In rough order of ambition:
- Boost `fit_score` (or bypass the review queue threshold) for a company with a live signal.
- Trigger a prospecting run / enrollment on signal arrival rather than waiting for the daily 9 AM sweep.
- Inject the signal into AI-composed copy — "saw you're hiring a Quality Engineer" is the difference between cold and warm.

**Effort:** small-to-medium. **Value:** high — this is capability you have already paid for and are not using.

### P1-2. Non-email sequence steps are human handoffs pretending to be a cadence

The multi-channel scaffold exists and is more complete than expected: `SequenceStep.channel` (`models/sequence.py:84`) supports `email` / `call` / `linkedin` / `video` / `direct_mail`, and the dispatcher honours it (`tasks/outreach.py:333-352`).

But what a non-email step *does* is create a `FollowUp` task and advance the sequence:

```python
if step.channel and step.channel != "email":
    reminder = FollowUp(..., reason=f"Sequence step {step.step_number}: reach out via {step.channel}")
    db.add(reminder); await _advance_step(db, enrollment)
    return {"status": "channel_task_created", ...}
```

That is a defensible design — LinkedIn cannot be safely automated (see P3-1) — but it means a "multi-channel cadence" is an email cadence plus a to-do list, and **the sequence advances whether or not the human ever does the task**. The step is marked done because a reminder was filed.

Similarly `sequence_optimizer.decide` can recommend `switch_channel` (`sequence_optimizer.py:222`) — "the email is held for a human to action the switch". Same shape: advice, parked.

**Proposal (no new integrations):**
- Don't advance the step until the task is completed, or advance it and record that the touch was *skipped*, not *delivered* — today the funnel counts a LinkedIn step nobody did as a completed step.
- Surface these tasks as a real queue (they land in `FollowUp` alongside everything else).
- Say plainly in the sequence builder that non-email steps are human tasks. Right now the UI implies the agent does them.

**Effort:** small. **Value:** medium-high — it stops the cadence lying about what happened.

### P1-4. Complaint ingestion — the rail's last dead metric

With P0-1 fixed the rail trips on bounces, but `complaint_rate` is still always 0: no code path anywhere writes a complaint status, so there is nothing to count. This is not a query bug — **we have no complaint signal at all.**

Gmail does not tell you when a recipient clicks "Report spam". Getting that requires one of:

- **Google Postmaster Tools** — domain-level spam-rate reporting. Free, but aggregate-only (no per-recipient attribution) and needs DNS verification on the sending domain. Would give a real `complaint_rate` for the rail, which is the metric that matters most for reputation.
- **A proper ESP with a feedback loop** (SES/SendGrid/Postmark) — per-message complaint webhooks, but that's a transport change, not a feature.

Recommend Postmaster Tools: it is the only option that doesn't change how mail is sent, and complaint rate is the signal Gmail actually throttles on. Note `domain_health.py` already has a "not checked automatically" note pointing at Postmaster Tools, so the gap is known.

Until then the rail is protected on bounces and blind on complaints — worth stating plainly on `/dialtone/health` rather than showing a green `complaint_rate: 0.0%` that is really "we don't know".

**Effort:** medium (vendor/DNS + ingestion). **Value:** high — it's the other half of the brake.

### P1-3. `cadence_pattern` is dead config

`OutreachSequence.cadence_pattern` (`models/sequence.py:48`, default `"single_channel"`) is **read by nothing** — grep returns only the model definition. Either wire it or drop it; a column that looks like it configures behaviour and doesn't is the same trap as the Settings badges fixed last session.

**Effort:** trivial. **Value:** low, but it's a lie in the schema.

---

## P2 — Depth on existing capability

### P2-1. Competitive enrichment before outreach

`routers/competitive.py` has full competitor CRUD; `services/` has competitive intelligence. What I could not find is any use of it *at outreach time* — no join from a company to its competitor relationships before composing.

**Proposal:** at AI-compose time, pass known competitor context into the prompt ("they currently use X"), and let a competitor relationship influence fit. This is a prompt/context change, not new infrastructure.

**Effort:** small. **Value:** medium. Depends on how populated the competitor data actually is — **worth checking real row counts before investing.**

### P2-2. Two-way CRM sync

`integrations/providers.py` has **real** clients, not stubs: `HubSpotConnector._action_sync_contact` posts to `api.hubapi.com/crm/v3/objects/contacts` (`:116-142`), Salesforce alongside it, plus Slack, Google Calendar, Clearbit, ZoomInfo.

The limit is scope: `supported_actions = ["sync_contact"]`. Push-only, contacts-only. No pull, no deals, no activity sync, no conflict resolution.

**Proposal, in order:** (1) pull companies/contacts *in* so Dialtone can prospect against an existing book and dedup against it; (2) push outreach activity so the CRM shows what the agent did; (3) deal sync — which also feeds P0-2 attribution.

**Effort:** medium per direction. **Value:** medium — high if a real CRM is in play, near-zero if not. **This is the item most dependent on a fact I don't have: does the business run HubSpot/Salesforce today?**

### P2-3. Smarter cadence — the foundations are already there

Worth being precise, because two similarly-named modules do different things:

- `send_time_optimizer.py` — **wired into the send path.** `process_outreach_queue` calls `should_defer` before dispatch (`tasks/outreach.py:813`), holds a send for the recipient's best window, assigns an A/B arm, caps the delay. This works.
- `send_timing.py` (`SendTimingAnalyzer`) — powers `/send-timing/*` reporting.
- `ContactEngagement` (`models/contact_engagement.py`) already stores per-contact `best_dow`, `best_hour`, `buckets`, `confidence`, resolved `tz`.

So "AI-adjusted timing based on prospect behavior" is **substantially built**. The genuine gaps are *delay* and *content*, not time-of-day:
- `delay_days` is static per step. Behaviour doesn't compress or stretch the gap — a prospect who opened four times gets the same 3-day wait as one who never opened.
- `send_condition` supports `always` / `if_not_opened` / `if_opened_no_reply` (`models/sequence.py:91-93`) — real branching, but coarse.

**Proposal:** let engagement modulate `delay_days` (tighten on high engagement, back off on silence), reusing the confidence signal `ContactEngagement` already computes.

**Effort:** small-to-medium. **Value:** medium.

### P2-4. Reporting gaps

Beyond P0-2 attribution: no cohort view (how does a sequence perform over time), no per-sender comparison in reports, and `clickRate: 0 // click data not available in trends` in the frontend — click data exists (`tracking.py:240`) but isn't in the trend series.

**Effort:** small each. **Value:** low-medium individually.

---

## P3 — New capability (and one I'd push back on)

### P3-1. LinkedIn automation — recommend NOT building this

What exists: `tactics.py:82` `POST /linkedin-draft` generates a connection note / follow-up / post comment via AI (`ai.linkedin_messages`). It **drafts text for a human to paste**. There is no LinkedIn API integration anywhere in `app/`.

That is the correct design, and I'd keep it. Reasons to be honest about before anyone scopes "LinkedIn connection requests + messages as sequence steps":

- **LinkedIn's User Agreement prohibits automated access, scraping, and bots.** Automating connection requests or messages risks the *sender's personal account* — restriction or permanent ban — not just an integration breaking.
- There is **no official API for this**. LinkedIn's partner APIs don't expose connection requests or 1:1 messaging for cold outreach. Every vendor doing it uses browser automation or unofficial endpoints, in violation of the ToS.
- The blast radius lands on a real person's professional identity, and this system runs unattended. Dialtone's whole safety posture — kill switch, warm-up, auto-pause — exists because unattended sending is dangerous. Pointing that at a platform actively policing automation with account bans is a different risk class from email.

**What I'd propose instead:** make the human-in-the-loop path excellent. A LinkedIn step already creates a task (P1-2); give it the drafted note from `tactics.py` attached, one click to copy, one click to mark done — so a rep does 20 in five minutes. That captures most of the value, breaks no terms, and risks nobody's account.

If LinkedIn automation is genuinely wanted, it should be **an explicit, human-approved decision with the ToS risk understood** — and via a third-party vendor who owns that risk, not code we write.

### P3-2. Website visitor de-anonymization

The user asked about "website visits". What exists is **email** open/click tracking only (`tracking.py:183,240`) — pixel and link redirects for mail we sent. There is no tracking of visits to Apprend's own site, and no visitor de-anonymization.

Real intent from a visit ("someone at Acme read your pricing page twice") requires a script on the marketing site plus an IP→company reverse-lookup vendor (Clearbit Reveal, 6sense, Albacross). Clearbit is already a connector (`providers.py:179`), so the vendor relationship may be half there.

**Effort:** medium, plus vendor cost and a privacy/consent review (GDPR: IP is personal data). **Value:** potentially high — it's the strongest intent signal available — but it's a genuine build with legal surface, not a wiring job. **Do P1-1 first**: if you don't act on the intent signals you already collect, you won't act on these either.

### P3-3. Other gaps worth naming

- **No suppression list import.** You cannot bulk-load a do-not-contact list. For outbound compliance that's a real hole — `Contact.unsubscribed` exists but only one-at-a-time.
- **Complaint statuses may have no writer** (see P0-1). If so, the third rail metric is dead too.
- **Migration chain doesn't apply from scratch** — a duplicate `ix_interactions_company_id` in an early revision. Prod is fine (incremental), but a fresh environment cannot be built from migrations. That's a disaster-recovery gap.
- **`landline-backend` git history contains real production secrets** (`.env.production` tracked until `a0b83e5`). Previously flagged; still unrotated as far as I know.

---

## Recommended sequence

| Phase | Work | Why first |
|---|---|---|
| ~~1~~ | ~~**P0-1** danger metrics~~ | ✅ **Done** (`bb60fd6`) — rail now trips on bounces. Complaints still blind → **P1-4**. |
| 2 | **P0-2** attribution | Until this exists, every item below is ranked on vibes — including by the ICP learner. |
| 3 | **P1-1** intent → action | Highest value per hour: the capability is built and idle. |
| 4 | **P1-2** honest channel steps | Stops the funnel counting touches nobody made. Small. |
| 5 | **P2-3** engagement-driven delays | Builds on `ContactEngagement`, already populated. |
| 6 | **P2-2** CRM pull | Only if a CRM is actually in play — confirm first. |
| 7 | **P3-2** visitor intent | Real build + legal review. Only after P1-1 proves you act on signals. |
| — | **P3-1** LinkedIn automation | Recommend against. Do the human-assisted path in P1-2 instead. |

---

## What I could not verify

Stated plainly, because a confident roadmap built on unchecked assumptions is how the last audit went wrong:

1. **How much competitor data actually exists** (P2-1) — row counts, not schema.
2. **Whether a CRM is in use** (P2-2) — decides between "medium value" and "don't bother".
3. **Real prod volumes.** As of this session prod has 1 active Gmail account, an ICP fit on **0 won deals**, and the bounce sweep had never once run successfully. Several items above assume more operational history than may exist — **P0-2's value in particular depends on there being deals to attribute.**

### Since resolved (verified before publishing)

- **Complaint statuses have no writer** — confirmed by exhaustive scan. Folded into P0-1: the rail cannot fire at all.
- **`tactics.py`'s LinkedIn drafts are live**, not dark: `tacticsApi.linkedinDraft` → `useLinkedinDraft` → `CompanyDetail.jsx:1331`. That strengthens P3-1 — the human-assisted path is real and already used, which is the path worth investing in.

## Open questions

1. **P0-1:** update `EmailSend.status`, or repoint the monitor at `BounceRecord`? I lean to the latter; it needs a decision.
2. **P0-2:** attribute to the sequence, the source (Places/Apollo), the ICP version, or all three?
3. **P1-1:** should a hot intent signal be allowed to *bypass the human review queue*, or only re-rank within it? This is a safety question, not a product one.
4. **P3-1:** is LinkedIn automation a hard requirement? If so it needs an explicit risk acceptance, and I'd want that in writing.
5. Is there an owner for the committed-secrets rotation (`reference: landline committed secrets`)? It has outlived several sessions.
