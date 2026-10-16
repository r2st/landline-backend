# Landline (backend) — PROJECT_INFO

**B2B lead-generation backend for Apprend Technologies: discovers, scores and engages manufacturing companies across the US and Canada.**

- **Repo:** https://github.com/r2st/prospect-backend · branch `main`
- **Local path:** `apprend_tech/landline-backend`
- **Frontend:** [`../landline-frontend`](../landline-frontend)

## Tech stack

| Layer | Technology |
|---|---|
| Framework | FastAPI (async) |
| Database | PostgreSQL 16 — asyncpg + SQLAlchemy 2.0 (async) |
| Migrations | Alembic |
| Queue | Celery + Redis 7 — queues `celery,outreach,scraping,enrichment`; beat runs in the same container |
| LLM | Fallback chain `openrouter → gemini → groq → cerebras → static template` (`app/services/llm_providers.py`) |
| Email | Amazon SES |
| Data sources | ThomasNet + US Census CBP API (discovery), Apollo.io (enrichment), Hunter.io |
| Container | Docker Compose (`docker-compose.prod.yml`) |
| Tests | pytest |

## Deploy location

| | |
|---|---|
| Host | AWS EC2 `i-05bf9d5a16704ad24` · `100.30.136.86` · Amazon Linux, `ec2-user` |
| Code | `/opt/landline` (a real git checkout — `git pull origin main`) |
| Public URL | https://landline-api.apprendtechnologies.com (nginx → `127.0.0.1:8001`) |
| Containers | `landline-api`, `landline-celery`, `landline-postgres`, `landline-redis` |
| Ports | `127.0.0.1:8001→8000` api · `127.0.0.1:5433→5432` postgres · `127.0.0.1:6380→6379` redis (all loopback-only) |
| `APP_ENV` | `staging` — despite the name, this box serves production traffic |

**Access is via AWS SSM, not SSH — port 22 is closed by the security group.**

```bash
aws ssm send-command --instance-ids i-05bf9d5a16704ad24 \
  --document-name AWS-RunShellScript \
  --parameters 'commands=["cd /opt/landline && docker ps"]'
```

An older Ubuntu box, `18.209.12.106` (key `../keys/vk.pem`), appears in
[`deploy/DEPLOY-CHECKLIST.md`](deploy/DEPLOY-CHECKLIST.md) and is **not** the current target.

## SSH key

No usable SSH path to the live host — use SSM (above). AWS credentials that authorize it:

- `../keys/aws` — plaintext AWS access key ID + secret (IAM user `vidhi`, account `235494810658`)
- `../keys/ec2-apprend-staging` — SSH private key for the `ec2-vidhi` staging instance
- `../keys/vk.pem` — RSA key for the legacy `18.209.12.106` box

On the server itself, `git pull` authenticates with its own read-only deploy key as `ec2-user`.

## Environment variables

| Where | What |
|---|---|
| `.env.example` | Documented var list |
| `.env` (gitignored) | Local dev — Anthropic, internal API key |
| `keys/openrouter_key` (gitignored) | OpenRouter key |
| `../keys/` | Company-wide: `aws`, `apollo_api_key`, `hunter api key`, `census_api_key`, `openrouter key`, `google OAuth client and secret` |
| **Server** | **`/opt/landline/.env.production`** — the file both `api` and `celery-worker` load via `env_file` |

Because both services use `env_file`, a change to `.env.production` needs
`up -d --force-recreate` — `docker compose restart` will **not** pick it up.

Provider-key background: [`../keys/llm-fallback-providers.md`](../keys/llm-fallback-providers.md).
`GROQ_API_KEY` was added to production on 2026-07-31; `GEMINI_API_KEY` and
`CEREBRAS_API_KEY` are still unset, so the live chain is `openrouter → groq`.

## Key commands

```bash
# Local dev
docker compose up -d
alembic upgrade head
uvicorn app.main:app --reload
pytest

# Deploy (run on the box, or pipe through SSM)
cd /opt/landline && bash deploy-staging.sh

# Restart after an env change
cd /opt/landline
docker compose -f docker-compose.prod.yml up -d --force-recreate api celery-worker

# Health
curl http://127.0.0.1:8001/health
curl http://127.0.0.1:8001/api/v1/ai/providers    # requires a bearer token
```

## Related projects

- [`../landline-frontend`](../landline-frontend) — the dashboard that consumes this API
- [`../apprend-backend`](../apprend-backend) — the main Apprend platform API
- `../ops` — deploy and incident scripts, key rotation
- `../demo-sites` — 20 manufacturing-vertical demo apps used as sales collateral for Landline
- `~/projects/PROJECT-INDEX.md`, `~/projects/keys/KEYS_INDEX.md` — estate-wide index
