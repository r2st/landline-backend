# Discovery: document → leads → AI email → send → follow-ups

**Status:** approved 2026-10-17 (no-key → block; key scope Discovery only; 2 follow-ups +3/+7 business days; files purged after 30 days; no OCR in v1) · **Repos:** prospect-backend, prospect-frontend · **Model:** `gemini-2.5-flash`

## Goal

On `/discovery`, a user uploads a document (CSV, XLSX, PDF, DOCX, TXT) listing companies or people. Gemini identifies the companies and contacts in it, the user reviews them and imports the ones they want, Gemini drafts an outreach email plus follow-ups, and the user sends. The follow-ups go out automatically unless the contact replies. All AI calls use **the org's own Gemini key**, which lives only in the backend.

## What already exists (and gets reused, not rebuilt)

| Need | Existing piece | Gap |
|---|---|---|
| Per-org Gemini key (item 6) | `org_llm_keys` table (121), `GET/PUT/DELETE /settings/llm-key` (org-admin only, Fernet-encrypted, returns only a masked hint), Settings → API Keys UI | **Nothing reads the key.** Every AI call uses the platform chain. Fix: a resolver that returns the org key |
| Gemini calls | `llm_providers.chat_completion(providers=[...])` + `ProviderSpec` (OpenAI-compatible Gemini endpoint) | Need a one-provider Gemini spec built from the org key, with model `gemini-2.5-flash` |
| Company/contact creation + dedupe | `IngestionService.ingest()` with `CandidateRecord` / `ContactRecord` (domain + name_key dedupe, lead scoring) | None. Gemini output maps straight onto it |
| Sending | `email_dispatch.send_outreach_email()`: kill switch, do-not-contact list, spam gate, warm-up ramp, daily cap, unsubscribe footer, sender rotation | None. Every send goes through it |
| Follow-ups | `OutreachSequence` + `SequenceStep` (subject/body templates, `delay_days`/`delay_hours`, business days, jitter), `_enroll_into_sequence`, Celery sequence processor (stops on reply) | None |

So the new work is the document → leads step, the drafting step, and the UI. Sending and follow-ups ride on infrastructure that's already compliance-gated.

## User flow (new "From a document" tab on /discovery)

The existing search form stays as the first tab. The new tab is a four-step wizard:

1. **Upload.** Drag-drop one file (≤10 MB). The backend parses it and Gemini extracts candidates. The UI shows a progress state.
2. **Review.** A table of extracted companies, each with its contacts (name, title, email, domain, plus a confidence score and the source line/page). Rows matching an existing company are flagged "already in Prospect". The user ticks rows and clicks **Import selected**, which creates the companies and contacts through `IngestionService` with source `document`.
3. **Draft.** The user enters a short brief (what we offer, CTA, tone) and the number of follow-ups (0–3, default 2, delays 3 and 7 days). Gemini returns the initial email plus follow-ups as subject/body templates with `{{first_name}}`, `{{company_name}}` and similar merge fields. Everything is editable inline, there's a "regenerate" per step, and preview renders it for a sample contact. Optionally **Save as template** (existing email-templates API).
4. **Send.** Pick the sender (existing rotation, or a specific account) and confirm the recipient count. The backend creates a sequence named `Document: <filename> · <date>` from the drafted steps and enrolls the selected contacts. Step 1 goes out through the normal queue, so the daily cap, warm-up and pacing apply, and follow-ups follow on schedule. The UI links to the sequence page for status and pause/stop.

"Send" deliberately means **enroll in a sequence**, not a blast loop. That way the initial email and the follow-ups share one engine with reply-stop, pause, caps and reporting. A single-email send is just a sequence with 0 follow-ups.

## Backend design

### Gemini resolution: `app/services/org_gemini.py`

- `get_org_gemini_key(db, org_id) -> str | None`: decrypts the `org_llm_keys` row where `provider='gemini'`.
- `gemini_provider(key) -> ProviderSpec`: model `settings.discovery_gemini_model` (new setting, default `gemini-2.5-flash`), Gemini OpenAI-compatible base URL. `reasoning_effort: "low"` so 2.5-flash's thinking tokens don't eat the output budget.
- `generate_json(db, org_id, system, prompt, schema, max_tokens)`: calls `chat_completion` with **only** that provider and its **own circuit breaker**, so a bad key in one org can't trip the shared platform `gemini` breaker. It asks for JSON output and validates it with Pydantic.
- **No platform fallback** for these features. If the org has no key, the endpoint returns `409 {"code":"gemini_key_required"}` and the UI shows "Add your Gemini API key in Settings → API Keys" with a link. (Open question 1.)
- The key never leaves the backend: it isn't logged, isn't in responses (only the existing masked hint), and isn't in errors.

### Document parsing: `app/services/document_extract.py`

| Type | Parsing | What goes to Gemini |
|---|---|---|
| CSV / XLSX | `csv` / `openpyxl` (new dep) | **Header + 5 sample rows only.** Gemini returns a column mapping (`company_name`, `domain`, `email`, `first_name`…), and the backend applies it deterministically to every row. This is cheap, exact, and handles 10k rows |
| PDF | `pypdf` (new dep) text extraction, page-tagged | The text, chunked at about 30k chars; each chunk extracts companies/contacts as JSON |
| DOCX | `python-docx` (new dep) | Same as PDF |
| TXT | as is | Same as PDF |

- **Limits:** 10 MB per file and 200 pages; chunks are capped so one upload costs at most around 20 Gemini calls. Scanned PDFs with no extractable text get a clear "no text found" error in v1 (OCR is out of scope).
- **Validation:** the content type is checked against the file's magic bytes, and the filename is cleaned with the existing `sanitize_filename`.
- **Accuracy:** Gemini is told to extract only what's in the text and **never invent emails or domains**. Every field carries `source_ref` (row or page). Server-side, emails are syntax-checked and domains normalized with `normalize_domain`.

### Storage: migration `123_discovery_documents`

`discovery_documents` (org-scoped via `OrgScopedMixin`): `id, filename, content_type, size_bytes, content (LargeBinary, deferred, deleted after 30 days), status (parsing|ready|failed), error, extracted (JSONB: candidates + confidences + source_refs), sequence_id (nullable), created_by, created_at`. The extraction result is kept so the review step survives a page reload, and so the user can come back later.

### Endpoints (router `discovery_documents`, under `/discovery/documents`, authenticated and org-scoped)

| Method | Path | Does |
|---|---|---|
| POST | `/discovery/documents` (multipart) | Store the file and enqueue a Celery parse/extract job; returns `{id, status:"parsing"}` |
| GET | `/discovery/documents` | Recent uploads for this org |
| GET | `/discovery/documents/{id}` | Status + extracted candidates (with duplicate flags) |
| POST | `/discovery/documents/{id}/import` | `{candidate_ids}` → `IngestionService.ingest`; returns created/matched company + contact ids |
| POST | `/discovery/documents/{id}/draft` | `{brief, tone, follow_ups, sample_contact_id}` → Gemini → `{steps:[{subject, body, delay_days}]}` |
| POST | `/discovery/documents/{id}/send` | `{contact_ids, steps, sender_account_id?, name?}` → create `OutreachSequence` + `SequenceStep`s, then `_enroll_into_sequence`; returns `{sequence_id, enrolled, skipped}` |

Parsing runs in Celery because PDFs plus several Gemini calls can take more than 30 s; the UI polls `GET …/{id}` every 2 s. The send endpoint requires the existing "member" role or higher, and contacts on the do-not-contact list are reported as `skipped` up front (the send path enforces it anyway).

### Tests

These cover the parsers (CSV mapping, PDF chunking), the JSON contract with Gemini mocked (including malformed output and invented emails being rejected), org isolation (org A can't read B's document or use B's key), the 409 when there's no key, ingestion dedupe, and send → sequence + enrollment rows created and the daily cap respected.

## Frontend design

- `src/pages/Discovery.jsx`: tabs "Search" (existing) and "From a document" (new).
- `src/components/discovery/DocumentWizard.jsx` with `UploadStep`, `ReviewStep`, `DraftStep`, `SendStep`.
- `src/hooks/useDiscoveryDocuments.js` (react-query), plus `discoveryApi` additions in `api/client.js`.
- A "no Gemini key" banner linking to Settings → API Keys. The existing Settings UI already covers item 6; its copy changes to say the key powers Discovery AI.
- **No key, model name, or Gemini URL appears anywhere in the frontend.**

## Delivery plan (each step lands on main on its own)

1. **Backend: org Gemini resolver + tests.** This also fixes "stored key never used", but only for the new features. (Open question 2.)
2. **Backend:** documents table + upload/parse/extract + import.
3. **Backend:** draft + send endpoints.
4. **Frontend:** the wizard plus the Settings copy tweak.
5. **Deploy** after your go-ahead (the server backend checkout is still diverged from `origin/main`; see the earlier note).

## Open questions for you

1. **No org key:** block with "add your key" (proposed), or fall back to the platform AI chain?
2. **Scope of the key:** use the org's Gemini key only for these new Discovery features (proposed), or also switch the existing AI features (generate-email, scoring, etc.) to prefer it?
3. **Follow-up default:** 2 follow-ups at +3 and +7 business days, stopping on reply. OK?
4. **Retention:** delete uploaded file bytes after 30 days and keep only the extracted results. OK?
5. **Scanned PDFs:** out of scope for v1 (proposed), or worth adding Gemini vision OCR now?

## Out of scope (v1)

OCR of scanned documents, web-enriching companies found in documents (the existing "Enrich" button still works afterwards), multi-file batch upload, and per-recipient AI personalization beyond merge fields.
