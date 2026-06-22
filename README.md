# Landline — B2B Lead Generation Backend

**By Apprend Technologies**

Landline is a standalone B2B lead generation tool designed for Apprend Technologies to discover, score, and engage manufacturing companies in the United States and Canada. It automates the process of finding potential clients for Apprend's manufacturing software products (press operations, quality control, factory floor management).

## Features

- **Company Discovery** — Scrape manufacturing companies from ThomasNet and Census Bureau CBP API
- **Contact Enrichment** — Enrich company and contact data via Apollo.io
- **Lead Scoring** — Rule-based scoring (NAICS industry fit, company size, location, data completeness) with AI scoring via Claude API
- **Pipeline Management** — Track companies through stages: new → researched → scored → contacted → engaged → qualified → opportunity → customer
- **Outreach Sequences** — Multi-step email sequences with template rendering via Amazon SES
- **CASL Compliance** — Full consent tracking for Canadian contacts (opt-in model) alongside CAN-SPAM for US contacts (opt-out model)
- **CSV Import/Export** — Bulk import companies and contacts; export data as CSV
- **Dashboard Analytics** — Pipeline stats, outreach metrics (open/click/reply rates), score distribution

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Framework | FastAPI (async) |
| Database | PostgreSQL 16 via asyncpg + SQLAlchemy 2.0 (async) |
| Migrations | Alembic |
| Task Queue | Celery + Redis 7 |
| Email | Amazon SES (boto3) |
| AI Scoring | Claude API (anthropic SDK) |
| Enrichment | Apollo.io API |
| Scraping | httpx + BeautifulSoup4 |
| Testing | pytest + pytest-asyncio |

## Quick Start

### Prerequisites

- Python 3.11+
- Docker and Docker Compose (for PostgreSQL and Redis)
- AWS credentials (for SES email sending)
- Apollo.io API key (for enrichment)
- Anthropic API key (for AI scoring)

### 1. Clone and Set Up

```bash
cd landline-backend
python -m venv .venv
source .venv/bin/activate    # Linux/Mac
# .venv\Scripts\activate     # Windows

pip install -r requirements.txt
```

### 2. Configure Environment

```bash
cp .env.example .env
# Edit .env with your API keys and database credentials
```

### 3. Start Infrastructure

```bash
docker compose up -d
```

This starts PostgreSQL 16 on port 5432 and Redis 7 on port 6379.

### 4. Run Migrations

```bash
alembic upgrade head
```

### 5. Start the API Server

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

The API is now available at `http://localhost:8000`. Interactive docs at `http://localhost:8000/docs`.

### 6. Start Celery Workers (optional, for async tasks)

```bash
# Main worker (all queues)
celery -A app.tasks.celery_app worker --loglevel=info -Q scraping,enrichment,outreach

# Beat scheduler (for periodic outreach processing)
celery -A app.tasks.celery_app beat --loglevel=info
```

#### Scheduled (Celery Beat) tasks

The system runs autonomously once the worker + beat are up:

| Task | Cadence | What it does |
|------|---------|--------------|
| `process_outreach_queue` | 5 min | Advances active enrollments to their next sequence step when due (respects send limits) |
| `check_email_replies` | 10 min | Detects Gmail replies, classifies intent with Claude, optionally auto-replies |
| `auto_enrich_companies` | 1 hour | Enriches companies missing industry/employee data via Apollo |
| `auto_score_leads` | 30 min | AI-scores companies that have no AI score yet |
| `generate_stale_lead_followups` | 4 hours | Creates follow-up reminders for active-pipeline leads that have gone quiet |
| `rescore_active_leads` | 12 hours | Re-runs rule-based scoring for active leads so engagement signals stay current |
| `check_company_news_batch` | 6 hours | Pulls and AI-analyzes recent news for tracked companies |

## API Endpoints

All endpoints are prefixed with `/api/v1`.

### Companies

| Method | Path | Description |
|--------|------|-------------|
| POST | `/companies/` | Create a company |
| GET | `/companies/` | List/search companies (with filtering and pagination) |
| GET | `/companies/{id}` | Get company details |
| PATCH | `/companies/{id}` | Update company fields |
| PATCH | `/companies/{id}/stage` | Transition pipeline stage |
| DELETE | `/companies/{id}` | Delete company (cascading) |
| POST | `/companies/{id}/merge` | Merge a duplicate company into this one (reassigns contacts/scores/news/follow-ups, backfills fields, deletes the source) |
| GET | `/companies/{id}/scores` | Get company's lead scores |

### Contacts

| Method | Path | Description |
|--------|------|-------------|
| POST | `/contacts/` | Create a contact (linked to company) |
| GET | `/contacts/` | List contacts (filter by company, search) |
| GET | `/contacts/{id}` | Get contact details |
| PATCH | `/contacts/{id}` | Update contact |
| DELETE | `/contacts/{id}` | Delete contact |
| GET | `/contacts/company/{company_id}` | Get all contacts for a company |

### Lead Scoring

| Method | Path | Description |
|--------|------|-------------|
| POST | `/leads/score` | Score a single company |
| POST | `/leads/score/batch` | Score multiple companies |
| GET | `/leads/pipeline` | View pipeline with scores |
| POST | `/leads/consent` | Record consent for a contact |
| GET | `/leads/consent/{contact_id}` | Check consent status |
| POST | `/leads/consent/{consent_id}/revoke` | Revoke consent |

### Outreach

| Method | Path | Description |
|--------|------|-------------|
| POST | `/outreach/sequences` | Create a sequence with steps |
| GET | `/outreach/sequences` | List sequences |
| GET | `/outreach/sequences/{id}` | Get sequence details |
| PATCH | `/outreach/sequences/{id}` | Update sequence |
| DELETE | `/outreach/sequences/{id}` | Delete sequence |
| POST | `/outreach/sequences/{id}/steps` | Add step to sequence |
| POST | `/outreach/enrollments` | Enroll contact in sequence |
| GET | `/outreach/enrollments` | List enrollments |
| PATCH | `/outreach/enrollments/{id}` | Update enrollment status |
| POST | `/outreach/events` | Record outreach event |
| GET | `/outreach/events/{enrollment_id}` | Get events for enrollment |

### Dashboard

| Method | Path | Description |
|--------|------|-------------|
| GET | `/dashboard/overview` | Full dashboard metrics |
| GET | `/dashboard/pipeline` | Pipeline stage breakdown |
| GET | `/dashboard/outreach-metrics` | Email performance metrics |

### Tactics (AI sales playbook)

Per-lead, multi-channel outreach assets generated by Claude. Backs the "Tactics" tab.

| Method | Path | Description |
|--------|------|-------------|
| POST | `/tactics/discover-executives/{company_id}` | Find decision-makers via Apollo and create de-duplicated contacts |
| POST | `/tactics/linkedin-draft` | Connection note + follow-up + post comment |
| POST | `/tactics/phone-script` | Opener, value prop, discovery questions, objection handlers, close, voicemail |
| POST | `/tactics/referral-draft` | Warm-intro request + forwardable blurb |
| POST | `/tactics/event-outreach` | Pre/at/post-event 3-touch sequence |
| POST | `/tactics/multi-touch-plan/{company_id}` | Sequenced multi-channel campaign plan |

### Import/Export

| Method | Path | Description |
|--------|------|-------------|
| POST | `/import-export/import/csv` | Import companies/contacts from CSV |
| GET | `/import-export/export/companies/csv` | Export companies as CSV |
| GET | `/import-export/export/contacts/csv` | Export contacts as CSV |

## Scoring Rules

Scores range from 0 to 100:

| Factor | Max Points | Logic |
|--------|-----------|-------|
| Industry Fit | 40 | NAICS 31-33 (manufacturing) = 40; related industries = 20; other = 0 |
| Company Size | 25 | 50-500 employees = 25; 500-2000 = 20; 10-50 = 15; 2000+ = 10; unknown = 5 |
| Location | 20 | US = 20; Canada = 15 |
| Data Completeness | 15 | Email +5, phone +3, website +3, contact name +4 |
| Engagement (bonus) | up to 25 | Email replied +12, clicked +7, opened +3; recent actionable news +5 |

The total fit score is capped at 100. Data completeness and the engagement bonus
are combined into the `engagement_score` column; the full per-factor breakdown
(including engagement signals) is returned in the score's `factors` JSON.

## CASL / CAN-SPAM Compliance

- **US contacts (CAN-SPAM)**: Can be emailed without prior consent. Must include unsubscribe mechanism. Contacts who opt out are blocked.
- **Canadian contacts (CASL)**: Require express or valid implied consent BEFORE any commercial email. Express consent has no expiry (unless set). Implied consent typically expires after 2 years.

## CSV Import Format

The CSV importer expects these column headers (all optional except `company_name`):

```
company_name, domain, city, state, country, employee_count, contact_name, contact_email, contact_title
```

## Running Tests

```bash
pytest tests/ -v --tb=short
```

Tests use an in-memory SQLite database and do not require PostgreSQL or Redis.

## Project Structure

```
landline-backend/
├── alembic/               # Database migrations
├── app/
│   ├── main.py            # FastAPI application entry
│   ├── config.py          # Settings via pydantic-settings
│   ├── database.py        # Async SQLAlchemy engine and session
│   ├── models/            # SQLAlchemy ORM models
│   ├── schemas/           # Pydantic request/response schemas
│   ├── routers/           # API endpoint handlers
│   ├── services/          # Business logic (scoring, consent, email, enrichment)
│   ├── scrapers/          # Data scrapers (ThomasNet, Census Bureau)
│   └── tasks/             # Celery async tasks
├── tests/                 # Test suite
├── docker-compose.yml     # PostgreSQL + Redis
├── requirements.txt       # Python dependencies
└── .env.example           # Environment variable template
```

## License

Proprietary — Apprend Technologies. All rights reserved.
