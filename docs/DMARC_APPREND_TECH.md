# DMARC for apprend.tech

_Last checked: 2026-07-17 (live DNS lookup)._

Dialtone sends cold email from **apprend.tech** (via Google Workspace). SPF and
DKIM are already published and passing — the one missing piece is **DMARC**.
Adding it tells receivers what to do with mail that fails SPF/DKIM alignment,
protects the domain from spoofing, and improves inbox placement.

## Current DNS state

| Record | Host | Status | Value found |
|--------|------|--------|-------------|
| **SPF**   | `apprend.tech` | ✅ present | `v=spf1 include:_spf.google.com ~all` |
| **DKIM**  | `google._domainkey.apprend.tech` | ✅ present | `v=DKIM1; k=rsa; p=…` (Google selector) |
| **MX**    | `apprend.tech` | ✅ Google | `1 smtp.google.com.` |
| **DMARC** | `_dmarc.apprend.tech` | ❌ **missing** | _(no record)_ |

Because SPF and DKIM both already align on Google's infrastructure, apprend.tech
can safely start DMARC and later move straight to enforcement.

## The record to add (start here)

Create **one TXT record** at your DNS provider:

| Field | Value |
|-------|-------|
| **Type** | `TXT` |
| **Host / Name** | `_dmarc` (some providers want the full `_dmarc.apprend.tech`) |
| **Value** | `v=DMARC1; p=none; rua=mailto:dmarc@apprend.tech; fo=1; pct=100` |
| **TTL** | default (e.g. 3600) |

What the tags mean:

- `v=DMARC1` — DMARC version (required, first).
- `p=none` — **monitor only**: nothing is quarantined/rejected yet. This is the
  safe first step; it lets you watch the aggregate reports before enforcing.
- `rua=mailto:dmarc@apprend.tech` — where aggregate (daily XML) reports are sent.
  Make sure `dmarc@apprend.tech` exists, or point it at a mailbox you read.
- `fo=1` — request failure reports when SPF **or** DKIM fails.
- `pct=100` — apply the policy to 100% of mail.

> ⚠️ You need to add this record yourself at the DNS registrar/host for
> apprend.tech — Dialtone has no DNS access. Once it's live, the Domain Health
> page in Landline will pick it up on the next daily sweep (or hit **Re-check**)
> and DMARC will flip from ❌ to ⚠️ monitor.

## Phased rollout (recommended)

DMARC should be tightened gradually so you never blackhole legitimate mail:

1. **Monitor** (now): `p=none` — the record above. Watch the `rua` reports for
   ~1–2 weeks to confirm all your legitimate senders align.
2. **Quarantine**: once reports look clean, publish
   `v=DMARC1; p=quarantine; rua=mailto:dmarc@apprend.tech; fo=1; pct=100`
   (failing mail goes to spam).
3. **Reject** (final): `v=DMARC1; p=reject; rua=mailto:dmarc@apprend.tech; fo=1; pct=100`
   (failing mail is refused outright — strongest anti-spoofing).

The Domain Health page surfaces the exact next-step record for whichever stage
you're at (the **"Add this DNS record"** box on the DMARC card), so you can copy
it straight into DNS at each step.

## Verifying after you add it

```bash
dig +short TXT _dmarc.apprend.tech
# expect: "v=DMARC1; p=none; rua=mailto:dmarc@apprend.tech; fo=1; pct=100"
```

Or in Landline: **Domain Health → apprend.tech → Re-check**. DMARC should read
`warn` at `p=none` and `pass` once you reach `quarantine`/`reject`.
