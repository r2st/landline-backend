# Dialtone — Deep Audit & Improvement Plan (2026-07-22)

Audit of the Dialtone cold-outreach subsystem (`landline-backend` tasks/services/routers +
`landline-frontend` `/dialtone/` pages), covering everything **beyond** the fixes already
shipped (reply matching, OpenRouter model, 7 reply-rate improvements, complaint ingestion,
auto-send safety, fan-out reply processing, funnel analytics).

Overall the subsystem is **mature and well-engineered**: the send path is defensive (kill
switch, suppression, MX/mailbox verification, consent, warm-up gate, conditional steps, A/B,
spintax, AI compose with review gate, adaptive delays, thread-linking, per-recipient send-time
optimization), warm-up ramps are enforced and back-filled, sender reputation drives per-account
throttling, and domain health runs SPF/DKIM/DMARC + DNSBL with change-only alerting. The gaps
below are real but targeted.

Priorities: **P0** = broken / advertised-but-nonfunctional / safety-signal lost · **P1** =
important gap · **P2** = nice-to-have.

---

## P0 — implemented in this pass

### P0-1. The entire notification/alerting layer is a dead stub
`app/services/notifications.py::NotificationService._send_notification` never delivers — it
only logs `"NOTIFICATION (not yet delivered): … configure email dispatch to enable delivery"`.
Every alert in the product flows through it and is therefore **silently dropped**:

| Caller | Alert that never fires |
|---|---|
| `tasks/replies.py` | lead **replied** / **unsubscribe** audit |
| `services/domain_health_service.py` | sending-domain **blacklisted / health dropped** |
| `services/danger_monitor.py` | **auto-pause** on bounce/complaint spike |
| `services/phone_enrichment.py` | warm-lead **phone found** |
| `services/ab_test_resolver.py` | A/B **winner declared** |
| `services/deal_progression_agent.py` | deal **at-risk / manager escalation** |
| `services/workflow_engine.py` | workflow **send-notification** action |
| `tasks/reporting.py` | **daily activity digest** email (`daily_summary_email_enabled`) |
| `notify_open` / `notify_click` | 3+ opens / link click |

Two of these are safety signals (domain blacklisting, danger-signal auto-pause) — losing them
is why this is P0, not P1. There is already a working transactional-send primitive
(`services/welcome_email.py` → `get_active_account` + `_make_sender().send_email`).

**Fix (done):** `_send_notification` now delivers to `settings.notification_email` through the
active connected sending account (Gmail-preferred, SMTP fallback), mirroring `welcome_email`.
It opens its own short-lived session when a caller doesn't pass one, so **no call site changed**.
Best-effort and non-raising: a missing account / transport error logs and returns a status dict,
never breaks the calling task. Revives all nine alert paths above plus the daily digest.

### P0-2. Sender-reputation degradation is computed but never alerted
`services/deliverability_optimizer.py::assess_all` throttles a degrading account and
`tasks/deliverability.py` only `logger.warning`s the throttled list — the operator is never told
a sender just went **throttled/critical** (bounces/complaints climbing). Domain health alerts on
change; sender health does not.

**Fix (done):** `assess_account` captures the prior status and fires a new
`NotificationService.notify_sender_health_change` on a **degrading transition** into
`throttled`/`critical` (change-only, so no daily noise; recovery is not alerted). Now that P0-1
makes notifications real, this reaches the operator.

---

## P1 — implemented in this pass

### P1-1. Per-sequence funnel omits deliverability (bounces)
`services/funnel_analytics.py` funnels `sent → delivered → opened → replied → positive`, but
**`delivered` is dead**: no `"delivered"` OutreachEvent is ever written (Gmail has no delivered
signal), so `delivered`/`delivered_rate` are always `0`. Meanwhile `"bounced"` events **are**
written by the send path but are **not** surfaced in the funnel — so an operator can't see a
sequence's bounce rate in the one place the funnel exists to answer "is this sequence healthy?".

**Fix (done):** `compute_funnel` now counts `bounced` per (sequence, step), and `_with_rates`
reports a real `delivered = sent − bounced` with `delivered_rate`, plus `bounced` and
`bounce_rate`. Purely additive to the response shape (the FE reads named fields), so no frontend
break; the dead `delivered` becomes meaningful.

---

## P1 — documented, not yet implemented (recommended next)

- **P1-2. Auto-sends are not spam-scanned.** `DeliverabilityOptimizer.scan_content` exists and is
  wired to a manual "Check spam risk" button, but the automated send path
  (`tasks/outreach.send_sequence_step`) never scans the final copy. AI-compose guardrails cover
  AI copy, but static/spintax/optimizer-rewritten copy ships unscanned. Recommend a soft gate:
  scan the rendered body, and on `risk == "high"` either park for review or strip the worst
  offenders, logging the score onto the OutreachEvent for per-template spam trending.
- **P1-3. No per-template performance / versioning.** Analytics roll up per *sequence-step*, but
  there's no template entity with a version history or a "which subject line won" view across
  sequences. A/B testing covers within-step variants only. Recommend a lightweight
  template-performance view keyed by subject/body signature (the spintax `variant_key` already
  captures signatures on the event — surface it).
- **P1-4. CRM sync is one-directional / thin.** `services/integrations/` has a provider base +
  webhook dispatch, but there's no inbound CRM contact/deal sync (HubSpot/Salesforce/Pipedrive)
  and no outbound webhook fired on the key lifecycle events (reply, bounce, meeting-booked,
  unsubscribe). `event_bus.publish` exists — wire the deliverability/reply events onto it and let
  `webhook_dispatch` fan them out to subscriptions.

## P2 — documented

- **P2-1. `process_outreach_queue` loads all active enrollments into memory** and filters
  `next_send_at` in Python (`tasks/outreach.py:828`). Fine at current scale; at 10k+ active
  enrollments push the `next_send_at <= now` filter into the SQL `WHERE`, and add an
  `ORDER BY next_send_at` so the daily-limit `break` is fair (currently arbitrary order).
- **P2-2. Warm-up visibility.** `GET /dialtone/warmup` returns day/limit/status but no graduation
  ETA or progress %. Cheap to add `days_remaining` / `graduates_on` for the operator.
- **P2-3. Funnel has no unsubscribe/complaint stage.** Those stop the enrollment via `status`
  (no OutreachEvent), so they can't come off events; a per-sequence enrollment-status roll-up
  would complete the picture. (`/dialtone/sequences/{id}/analytics` already reports
  `bounced_enrollments` — extend with `unsubscribed`.)
- **P2-4. `funnel_analytics` reads `positive` by loading all replied rows and checking JSON in
  Python** — fine (reply volume is small), but a `metadata->>'positive'` SQL filter on Postgres
  would be tidier.
- **P2-5. Notification email is HTML-naive.** Bodies are plain text wrapped in `<p>`; acceptable
  for internal alerts, noted for completeness.

---

## What was checked and found healthy (no action)
- Celery registration: every `beat_schedule` task is force-imported in `celery_app.py`
  (the previously-missing `reporting` import is present) — no silent-failure tasks.
- Send path org-scoping: `send_sequence_step` rebinds org from the enrollment before any
  scoped read/write; `process_account_replies` rebinds from the account. Correct multi-tenant.
- Suppression / consent / MX + mailbox verification / kill switch: all gate the send.
- Warm-up: ramp enforced via `check_send_allowed`, plans back-filled for legacy senders.
- Adaptive delays, conditional steps (fail-open on untracked), A/B resolution + crash-safe
  assignment, spintax attribution, thread-linking, per-recipient send-time deferral: all
  correct and actually applied at send time (not display-only).
- Domain health: SPF/DKIM/DMARC + DNSBL scored with excluded-unknown denominator, change-only
  alerting (dead only because of P0-1).
