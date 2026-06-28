# Email Deliverability Runbook

How Landline outreach reaches the inbox, what the code already does, and the DNS
work that must be done **outside the codebase**.

## How mail is sent

Outreach goes out through the **Gmail API as the connected user** (`app/services/gmail_sender.py`),
not raw SES. That matters: each message inherits the connected account's domain
SPF/DKIM/DMARC alignment, which is the practical reason these land in the inbox.
SES is only a legacy fallback when no Gmail account is connected.

So the domain that needs DNS hygiene is **whatever domain the connected sending
accounts use** (a Google Workspace domain, or `gmail.com` for consumer accounts —
gmail.com is already fully aligned by Google).

## What the code already does (✅ shipped)

- **List-Unsubscribe** (RFC 2369) + **List-Unsubscribe-Post: One-Click** (RFC 8058)
  on every message — now offering **both** a one-click https URL and a `mailto:`
  (the sending account's own monitored address by default). Required by Gmail/Yahoo
  bulk-sender rules since June 2024.
- **CAN-SPAM footer** on every send: physical postal address (Toronto, ON, Canada),
  a working unsubscribe link, and "reply UNSUBSCRIBE" fallback.
- **Multipart text/plain + text/html** — plain-text part is generated from the HTML
  with tags stripped (raw HTML in the text part is a known spam signal).
- **No images, no raw URLs in body text** (links are anchor tags) — enforced by tests.
- **Send limiter** (`app/services/send_limiter.py`): daily cap, per-contact monthly
  cap, minimum gap between follow-ups, and a **warmup ramp** (week 1: 10/day → week
  3+: 40/day).
- **Send window** (new): outreach only dispatches during business hours on weekdays
  in the configured timezone (`send_window_*` settings) — 3 AM / weekend sends read
  as a bot and depress reply rates.
- **Per-account round-robin rotation** spreads volume across senders.

## ⚠️ DNS gaps found (must fix at the DNS provider — not in code)

Checked `apprendtechnologies.com` on 2026-06-28:

| Record | Status | Action |
|--------|--------|--------|
| SPF (TXT) | **MISSING** | Add SPF |
| DKIM | **MISSING** | Enable DKIM in Google Workspace |
| DMARC (`_dmarc`) | **MISSING** | Add DMARC, start at `p=none` |
| MX | **MISSING** | Add MX (or stop using `@apprendtechnologies.com` as a from/reply/unsubscribe address) |

Because the domain has **no MX**, any `mailto:` at `@apprendtechnologies.com` (incl.
an unsubscribe mailbox) would bounce. That is why `unsubscribe_mailto` now defaults
to **empty** and the List-Unsubscribe mailto falls back to the **sending Gmail
account's own address**, which is real, monitored, and reply-synced.

### If you send from a Google Workspace domain (recommended), set:

```
; SPF — authorize Google to send for the domain
apprendtechnologies.com.  TXT  "v=spf1 include:_spf.google.com ~all"

; DMARC — start in monitor mode, then tighten to quarantine/reject once clean
_dmarc.apprendtechnologies.com.  TXT  "v=DMARC1; p=none; rua=mailto:dmarc@apprendtechnologies.com; fo=1"
```

DKIM: Google Workspace Admin → Apps → Google Workspace → Gmail → Authenticate email
→ Generate the 2048-bit key and publish the provided `google._domainkey` TXT record,
then click **Start authentication**.

Once a couple of weeks of DMARC reports show only legitimate, aligned mail, move
DMARC to `p=quarantine` and then `p=reject`.

## Sender-reputation practices

- Keep the **warmup** on for any new sending account (`warmup_enabled=true`).
- Keep daily volume modest per account; rotate across accounts.
- Honor unsubscribes and bounces fast — a `bounced` send marks the account `error`
  and the enrollment `bounced`; replies pause the enrollment.
- Prune invalid addresses before enrolling (high bounce rate is the fastest way to
  get throttled).
- Monitor [Google Postmaster Tools](https://postmaster.google.com/) for the sending
  domain's spam rate and reputation.

## Settings reference (`app/config.py`)

| Setting | Default | Purpose |
|---------|---------|---------|
| `unsubscribe_mailto` | `""` | mailto for List-Unsubscribe; empty → sender's own address |
| `send_window_enabled` | `true` | gate sends to business hours |
| `send_window_start_hour` / `_end_hour` | `8` / `18` | window bounds (24h) |
| `send_window_timezone` | `America/Toronto` | window timezone |
| `send_window_weekdays_only` | `true` | skip weekends |
| `max_emails_per_day` | `50` | global daily cap |
| `max_emails_per_contact_per_month` | `8` | per-contact 30-day cap |
| `min_days_between_followups` | `3` | min gap to same contact |
| `warmup_enabled` | `true` | gradual ramp for new accounts |
