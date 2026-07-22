# Inbox Improvements — Feature Doc

**Status:** Proposed — awaiting approval before implementation
**Date:** 2026-07-22
**Scope:** Unified email Inbox — `landline-frontend/src/pages/Inbox.jsx`, `landline-backend/app/routers/emails.py` + `app/services/email_history.py`

---

## 1. Where the Inbox is today

The Inbox is a working, thread-grouped email client. What already exists:

| Capability | Status |
|---|---|
| Conversation threading (grouped by `gmail_thread_id`) | ✅ Done |
| Sent + received unified into one view | ✅ Done |
| Filter tabs: All / Received / Sent / Opened / Needs Reply | ✅ Done |
| Text search (subject + from + to only) | ✅ Partial |
| Per-account filter dropdown (Gmail + SMTP) | ✅ Done (recent) |
| Inline reply (plain-text textarea) | ✅ Basic |
| Numbered pagination (20/page) | ✅ Done |
| Expand/collapse accordion per thread + per message | ✅ Done |
| Manual "Sync emails" button | ✅ Done |
| Unread dot (thread + message level) | ✅ Display only |

The data model is richer than the UI exposes. `EmailReply` already carries
`is_read`, `priority_flag` (hot-lead marker), and `reply_classification`
(interested / not_interested / unsubscribe / referral / ooo / unknown). The
backend already has `/thread/{id}/intelligence` (momentum, objections,
recommended next action), plus `email_templates`, `email_signatures`, and
`ai_drafts` routers. **Most of the improvements below surface capability that
already exists in the backend but is invisible in the Inbox UI.**

---

## 2. Gaps a sales rep hits daily

1. **Mark-as-read is a no-op here.** `is_read` is displayed but the unified
   Inbox never sets it. Opening/reading a thread leaves the unread dot on
   forever. A mark-read endpoint exists in `replies.py` but is not wired to this
   view. → reps can't tell what they've actually dealt with.
2. **No archive / snooze / done.** Every thread lives in one flat list forever.
   No way to clear handled conversations or defer one to later.
3. **Search misses the body** and has no date range — a rep searching for a
   phrase inside an email gets nothing.
4. **No bulk actions** — can't mark a page read or archive a selection.
5. **Bare composer** — no AI-draft, no saved templates, no signature, no
   subject/CC edit, despite all three backends existing.
6. **Hot leads aren't surfaced.** `priority_flag` and `reply_classification`
   exist but there's no priority view or label chips.
7. **Conversation Intelligence is hidden** — momentum score, unresolved
   objections, and recommended next action are computed but never shown.
8. **No live updates** — manual Sync button only; no unread badge, no poll.
9. **Accordion-only layout** — no list + reading-pane option for fast triage on
   desktop.

---

## 3. Proposed improvements (prioritized)

### P0 — Correctness + core triage (ship first)

**P0-1 · Working mark-as-read**
- BE: `POST /api/v1/emails/thread/{gmail_thread_id}/read` (and `/unread`) — set
  `is_read` on all `EmailReply` rows in the thread, org-scoped to the caller's
  own accounts (reuse the `_sender_emails_for_user` guard already in `emails.py`).
- FE: mark a thread read on expand; optimistic cache update via
  `useEmailHistory`; manual "mark unread" affordance.
- Tests: endpoint scoping (can't mark another user's thread), idempotency,
  `has_unread` flips in the inbox response.

**P0-2 · Archive / done**
- Migration: add `is_archived` (bool, default false, indexed) to `email_replies`
  (and mirror on `EmailSend` if a fully-sent thread must archive too — decision
  in §5).
- BE: `POST /emails/thread/{id}/archive` + `/unarchive`; inbox gains an
  `archived` filter and excludes archived threads from the default view.
- FE: Archive button on the thread card + an "Archived" tab.
- Tests: archived threads leave the default list, reappear under the tab.

**P0-3 · Full-text search + date range**
- BE: extend `get_inbox` search to also match `body_text` (Postgres `ilike`,
  or a `to_tsvector` GIN index if we want it fast); add `date_from` / `date_to`
  query params.
- FE: keep the search box; add a compact date-range control.
- Tests: body-only match returns the thread; date bounds filter correctly.

**P0-4 · Fix `needs_reply` pagination count**
- `needs_reply` is filtered *after* grouping/pagination, so `total` and page
  math can be wrong. Move the filter into the query/count path.
- Tests: `total` equals the number of needs-reply threads.

### P1 — Sales productivity

**P1-1 · Richer reply composer**
- Insert saved template (`email_templates`), append signature
  (`email_signatures`), and an "AI draft" button (`ai_drafts`) that pre-fills the
  reply. Editable subject; minimal rich-text (bold/italic/link/lists).
- Tests: template/signature insertion, AI-draft happy path + failure toast.

**P1-2 · Labels / classification chips + priority view**
- Surface `reply_classification` as color chips on thread cards; add a
  "Priority" tab driven by `priority_flag`.
- FE-only + a filter param on the inbox endpoint. Tests: priority tab returns
  only flagged threads.

**P1-3 · Bulk actions**
- Checkbox selection → mark read / archive selected / "mark all read on page".
- Reuses P0-1/P0-2 endpoints with an id-list variant.
- Tests: bulk mark-read flips all selected, leaves others untouched.

**P1-4 · Conversation Intelligence panel**
- Show momentum label, unresolved objections, and recommended next action from
  `/thread/{id}/intelligence` inside the expanded thread; one-click "use
  suggested next message" into the composer.
- Tests: panel renders from stored analysis; refresh path.

### P2 — Polish + scale

**P2-1 · Two-pane reading layout (desktop)** — thread list left, reading pane
right; collapses to today's accordion on mobile (test at 375px).
**P2-2 · Auto-refresh + unread badge** — poll inbox (or reuse existing sync
cadence) on an interval; show an unread count on the sidebar Inbox nav item.
**P2-3 · Snooze** — `snoozed_until` timestamp; thread hidden until then, then
resurfaces. (Bigger lift — needs a Celery sweep; deferred intentionally.)
**P2-4 · Infinite scroll** — optional replacement for numbered pagination once
the list-pane lands.

---

## 4. Suggested sequencing

1. **P0-1 + P0-4** together (one BE PR: read endpoint + count fix + tests).
2. **P0-2** archive (migration + endpoints + FE tab).
3. **P0-3** search/date.
4. Then P1 items, composer first (highest rep-visible value).
5. P2 as capacity allows.

Each item is independently shippable; P0s are the ones with actual correctness
bugs behind them.

## 5. Open decisions (need a call before coding)

- **Archive granularity:** archive at the *thread* level (needs a flag reachable
  from both `EmailReply` and `EmailSend`) or only mark incoming replies? Simplest
  is a per-thread archive state table keyed by `gmail_thread_id` + user — avoids
  touching two message tables. **Recommendation: a small `inbox_thread_state`
  table (thread_id, user_id, is_archived, snoozed_until, is_read_override)** —
  clean home for archive/snooze/read overrides without schema churn on the
  message tables.
- **Search performance:** plain `ilike` on body now, or invest in a `tsvector`
  GIN index up front? Recommendation: `ilike` first, index if it's slow at
  current volume.
- **Real-time:** polling (simple, matches current infra) vs. SSE/websocket
  (heavier). Recommendation: polling for P2-2.

---

## 6. Testing commitment

Every backend item ships with pytest coverage (endpoint + scoping + edge cases);
every frontend item ships with a Vitest/RTL test (there are already
`Inbox.test.jsx` and `useEmailHistory` consumers to extend). No item merges
without tests, per repo convention.
