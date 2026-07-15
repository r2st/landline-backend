# Landline Autonomous Email — Launch Readiness Audit

> ## 📌 2026-07-15 update — switched primary sender to `suman.das@apprend.tech`
>
> Outreach now sends via the **apprend.tech Google Workspace** mailbox over Gmail
> OAuth (not SES, not apprendtechnologies.com). This resolves most of blocker B1:
> Google Workspace signs SPF/DKIM for `apprend.tech` automatically.
>
> **Live DNS check for `apprend.tech` (via `app/services/domain_auth.py`):**
>
> | Record | Status | Detail |
> |--------|--------|--------|
> | SPF   | ✅ pass | `v=spf1 include:_spf.google.com ~all` |
> | DKIM  | ✅ pass | `google` selector, valid RSA key (Workspace) |
> | DMARC | ❌ fail | no `_dmarc.apprend.tech` record |
>
> DMARC can't be added — **no DNS access for apprend.tech**. With SPF+DKIM
> passing and mail sent through Google, Gmail/Outlook deliverability is decent;
> the residual gap is no DMARC policy/reporting. Verify anytime with
> `GET /api/v1/email/deliverability`.
>
> **Config/code changes shipped this pass:**
> - `primary_sender_email = suman.das@apprend.tech` (`app/config.py`); the
>   deliverability/test-send endpoints report the apprend.tech domain even before
>   OAuth is connected.
> - Warm-up ramp made explicit & conservative: **20 → 50 → 100 / day** over
>   weeks 1–3, steady at 100 (`warmup_*` + `account_warmup_*`); `WARMUP_START_DATE`
>   reset to `2026-07-15`.
> - Kill switch now **defaults OFF in production** (`SENDING_ENABLED_DEFAULT=false`
>   + migration `059` seeds `sending_enabled={enabled:false}`): sending stays
>   PAUSED until an operator turns it on.
> - `scripts/configure_sender.py` — sets the apprend.tech account as primary,
>   resets warm-up, and flips the kill switch (run after connecting the mailbox).
>
> **Still required before the first real send (operator, not code):**
> 1. Connect `suman.das@apprend.tech` via OAuth (Settings → Connect Gmail). The
>    Google OAuth app is in **Testing** mode → add the address as a test user in
>    the `metal-cascade-500017-k3` project's consent screen first, or it can't
>    connect (see `project_landline-gmail-oauth-debug`).
> 2. Run `scripts/configure_sender.py --reset-warmup` to make it primary.
> 3. Send a real test via `POST /api/v1/email/test-send` to an external inbox and
>    confirm it lands (not spam) + headers show SPF=pass, DKIM=pass.
> 4. Only then `scripts/configure_sender.py --enable-sending` (or the UI toggle).
>
> The audit below predates this change and still describes the
> apprendtechnologies.com/SES posture — kept for history.

---

**Date:** 2026-07-15
**Auditor:** Claude (code + live-DNS audit)
**Verdict:** 🔴 **NOT ready to send to real prospects yet.** The *software* is in
good shape — rate limiting, warm-up, bounce handling, unsubscribe/CAN-SPAM
footer, and the outreach copy are all genuinely well-built. But three
**infrastructure/ops gaps are launch-blocking**, chief among them **the sending
domain has no SPF/DKIM/DMARC records at all** (verified via live DNS today).
Turning on autonomous send in this state would spam-file the mail and burn the
domain's reputation on the first batch.

> **The system is "armed."** Every feature flag defaults to `True`
> (`app/config.py`) and `process-outreach-queue` runs every 5 minutes
> (`app/tasks/celery_app.py:51`). The moment a Celery worker runs with active
> enrollments and a connected sending account, it **will** send. Do not enroll
> the real 105-contact list until the blockers below are cleared.

---

## Scorecard

| # | Area | Status | One-line |
|---|------|--------|----------|
| 1 | Sender accounts | 🟡 PARTIAL | Mechanism solid; need to confirm real accounts are connected on the server |
| 2 | Email templates | 🟢 READY | Professional, personalized, no spam triggers, no fabricated claims |
| 3 | Unsubscribe / CAN-SPAM | 🟢 READY | Link + headers + **full postal address** (`company_postal_address`, config-driven) |
| 4 | Bounce processing | 🟢 READY | Hard/soft classification + auto-suppression + SES webhook wired |
| 5 | Rate limits | 🟢 READY | 50/day cap, 8/contact/month, 3-day gap, business-hours window |
| 6 | Contact data quality | 🟢 READY | Pre-send MX validation marks undeliverable domains (`email_status`); queue skips them |
| 7 | Email content / multipart | 🟢 READY | Multipart HTML+text, clean subjects, no spammy words |
| 8 | Domain SPF/DKIM/DMARC | 🟢 CODE READY | DNS records set on Cloudflare (see below); verify via `GET /email/deliverability` |
| 9 | Warm-up plan | 🟢 READY | Ramp auto-starts on first send + resettable via `POST /system/reset-warmup` |
| 10 | Kill switch | 🟢 READY | Global `sending_enabled` switch halts ALL sends + pauses enrollments (`POST /system/sending-toggle`) |
| 11 | Monitoring | 🟢 READY | Sender-health, email-performance, lead-quality report endpoints exist |
| 12 | Legal (GDPR/CAN-SPAM/India) | 🟡 PARTIAL | Consent model + unsubscribe present; see notes for India/GDPR gaps |
| 13 | Test send | 🟢 READY | `POST /email/test-send` — compliant send + SPF/DKIM/DMARC report; UI card on Settings |

---

## 🔴 Launch blockers (must fix before turning it on)

### B1 — Sending domain has NO email authentication
**Verified by live DNS lookup on 2026-07-15:**

```
dig TXT apprendtechnologies.com          → (no SPF record)
dig TXT _dmarc.apprendtechnologies.com   → (empty — no DMARC)
dig TXT google._domainkey.…              → (empty — no DKIM)
dig MX  apprendtechnologies.com          → (empty — no MX)
```

The domain resolves (AWS/CloudFront), but there is **zero** email auth. The SES
fallback sender is `landline@apprendtechnologies.com`
(`app/config.py:29`, `SES_SENDER_EMAIL` in `.env.production`). Cold mail from an
unauthenticated domain is spam-filed or rejected by Gmail/Outlook and will tank
the domain's reputation immediately.

- **If sending via SES from `@apprendtechnologies.com`:** hard blocker. Publish
  SPF (`v=spf1 include:amazonses.com -all`), enable/verify SES **Easy DKIM** (3
  CNAMEs), and publish a DMARC record (`v=DMARC1; p=none; rua=…` to start).
- **If sending only via connected Gmail/Workspace accounts:** less severe —
  Google signs those with its own SPF/DKIM for the mailbox's own domain — **but
  the SES last-resort path** (`app/services/email_dispatch.py:302`,
  transport `"ses"`) would still send unauthenticated mail. Either fix the DNS
  or disable the SES fallback so a misconfig can't silently send from a cold,
  unauthenticated domain.
- The app already ships a live SPF/DKIM/DMARC checker
  (`app/services/domain_auth.py`, `GET /api/v1/deliverability/domain-auth`) —
  run it against the real domain and require it to pass before enrolling.
- **Also confirm SES is out of sandbox.** A new SES account can only send to
  *verified* addresses until AWS grants production access. Unverified = every
  real prospect send silently fails.

### B2 — No global kill switch
Sending can only be stopped **per-account** (`GmailAccount/SmtpAccount.sending_enabled`,
`app/services/email_dispatch.py:210,218`) or **per-enrollment** (`status="paused"`,
`app/tasks/outreach.py:149`). There is **no single master switch** to halt *all*
autonomous sending instantly. Today, stopping everything means disabling every
account or killing the Celery beat/worker — slow and error-prone in an incident.

**Recommendation:** add a `SENDING_ENABLED` (default `False` until launch)
setting checked at the top of `process_outreach_queue` and the orchestrator, so
one flag flip halts all send loops. This should be in place *before* go-live.

### B3 — No test send to a real external prospect
The only evidence of a real send is an own-inbox roundtrip: an email was
confirmed delivered to `sumaninster7@gmail.com` on 2026-06-25
(`../landline-email-test-report.md`), and `scripts/test_email_roundtrip.py`
exercises the full compose→send→verify path between two owned Gmail accounts.
That proves the **Gmail transport + footer + headers work**, but nobody has sent
a single email to an external, third-party inbox to check inbox placement
(Gmail vs. spam), rendering, and link behavior. Do a **seed/inbox-placement
test** (e.g. mail-tester.com or a few personal accounts on Gmail/Outlook/Yahoo)
before touching the prospect list.

---

## 🟡 Fix-before-scale (not hard blockers, but do them)

### P1 — Physical postal address is city-only
The CAN-SPAM footer (`app/services/email_dispatch.py:324-345`) reads
`Apprend Technologies · Toronto, ON, Canada`. CAN-SPAM requires a **valid
physical postal address** (street address or a registered PO box), not just a
city. Add the real street/suite or a PO box.

### P2 — Contact emails are partly guessed, not verified
The list (`data/industry_contacts.csv`, 55 companies / 105 contacts) is **real**
— real manufacturers, real domains, real names. But the `notes` column shows
several addresses are **pattern-inferred**, e.g. `Pattern: firstname@… (69%)`,
`Pattern: first.last@… (confirmed)`, and a couple of role addresses (`info@…`).
Guessed addresses bounce, and a burst of hard bounces on a cold domain is the
fastest way to get blocklisted. **Run the list through an email-verification
service (NeverBounce/ZeroBounce) and drop/repair anything that isn't
deliverable** before enrolling. The bounce handler will catch failures *after*
the fact (`app/services/bounce_handler.py`), but prevention protects the domain.

### P3 — Global warm-up window has already elapsed unused
`WARMUP_START_DATE=2026-06-20`, `WARMUP_ENABLED=true` (`.env.production`). Warm-up
ramps week1=10 → week2=25 → week3=40 → uncapped at 21 days
(`app/services/send_limiter.py:123-141`, `app/config.py:96-100`). As of today
(2026-07-15) that's **25 days in — warm-up is already "complete," so the global
cap is back to 50/day** even though little/no real volume has actually been
sent. **Reset `WARMUP_START_DATE` to the real launch date** so the ramp reflects
actual sending history. (Per-account warm-up keyed off `connected_at`
— `send_limiter.account_warmup_limit`, `app/config.py:157-160` — still protects
newly-connected mailboxes, which is good.)

### P4 — Confirm sending accounts are actually connected
Gmail OAuth (`app/services/google_oauth.py`, `gmail_sender.py`) and SMTP
(`smtp_sender.py`) are fully implemented, and accounts are added at runtime via
the API/UI — there is **no seed migration that creates them**, by design. Whether
any production sending account is connected *right now* can't be read from code
(the DB lives on the server). Confirm on the server:
`GET /api/v1/gmail/accounts` / `/api/v1/smtp/accounts` should list ≥1 connected,
sending-enabled account before launch.

### P5 — India / GDPR nuance
The consent model (`app/models/consent.py`, `app/services/consent.py`) and
per-contact `unsubscribed` suppression are in place, and the outreach queue
checks consent before sending. Two gaps to close with counsel:
- **India:** the list includes Indian companies (Dalal Plastics, Micro Plastics,
  Kamath). India's DPDP Act + telecom UCC rules are stricter on unsolicited
  commercial email than US CAN-SPAM. Confirm B2B cold email to these is
  acceptable, or segment them out.
- **GDPR:** no EU contacts appear in the current list (all US/India), so this is
  low-risk today, but if EU prospects are added, "legitimate interest" for B2B
  cold email needs a documented basis and an easy opt-out (the latter exists).

---

## 🟢 What's genuinely ready (credit where due)

- **Rate limiting** (`app/services/send_limiter.py`): global 50/day cap
  (`can_send_today`), 8 emails/contact/30-days, 3-day minimum gap between
  follow-ups, and a **timezone-aware business-hours send window** (8am–6pm,
  weekdays, evaluated in the *contact's* own timezone). Deliverability-aware
  per-account capacity scales by reputation throttle × warm-up ramp.
- **Bounce processing** (`app/services/bounce_handler.py` + `POST /outreach/events`
  public webhook + `POST /deliverability/bounce`): classifies hard vs. soft on
  SMTP status + DSN text, hard-suppresses immediately, soft-suppresses after a
  threshold, tracks `bounce_count`/`last_bounce_type`.
- **Unsubscribe mechanics**: signed one-click unsubscribe URL
  (`_make_unsubscribe_url`), `List-Unsubscribe` + `List-Unsubscribe-Post:
  One-Click` RFC 8058 headers (`app/services/gmail_sender.py:40-59`), plus a
  visible footer in both HTML and plain text. Enrollment respects
  `contact.unsubscribed` before every send.
- **Multipart send**: every email goes out as HTML **and** an auto-derived
  text/plain alternative (`email_dispatch.py:_html_to_plain_text`), which helps
  inbox placement.
- **Outreach copy** (`app/services/outreach_templates.py`): a 4-step,
  research-designed molding sequence (Days 0/3/7/14). Professional, specific,
  personalized via `{{first_name}}/{{company_name}}/{{title}}`, **no spam-trigger
  words, no all-caps, no fabricated customer claims** (explicitly FTC-aware).
  This is the strongest part of the system.
- **Monitoring**: `GET /deliverability/senders` (per-account reputation/health),
  `GET /reports/email-performance`, `GET /reports/lead-quality`, plus open/click
  tracking (`app/routers/tracking.py`). Real-time bounce/complaint throttling via
  the hourly `assess-sender-health` beat task.

---

## Go / No-Go checklist

Do **not** enroll the real prospect list until every box is checked:

- [ ] **B1** — SPF, DKIM (SES Easy DKIM or Gmail/Workspace), and DMARC published & passing via `GET /deliverability/domain-auth`
- [ ] **B1** — SES confirmed **out of sandbox** (or SES fallback disabled and sending only via authenticated Gmail/Workspace accounts)
- [ ] **B2** — Global `SENDING_ENABLED` kill switch added and defaulted off
- [ ] **B3** — Real external inbox-placement test passed (mail-tester score ≥ 8/10; lands in Primary, not spam)
- [ ] **P1** — Full physical postal address in the footer
- [ ] **P2** — Contact list run through email verification; undeliverable rows removed
- [ ] **P3** — `WARMUP_START_DATE` reset to the real launch date; start at ≤20/day
- [ ] **P4** — ≥1 sending-enabled account confirmed connected on the server
- [ ] **P5** — India-contact + cold-email compliance confirmed with counsel
- [ ] Kill switch, warm-up, and daily-summary monitoring watched for the first week

**Bottom line:** the code is ~85% launch-ready and the hard engineering is done
well. The remaining 15% is **deliverability infrastructure and operational
safety** (domain DNS, kill switch, verified list, real test send) — exactly the
things that, if skipped, quietly destroy a sending domain on day one. Clear B1–B3
+ P1–P4 and it's safe to start a slow warm-up to real prospects.
