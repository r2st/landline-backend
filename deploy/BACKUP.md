# Database Backups

Landline's PostgreSQL database is backed up daily to S3. A full `pg_dump` covers
**every** table — including the knowledge base (`industry_contacts`,
`knowledge_documents`) added in migration `027` — so new tables are protected
automatically with no per-table configuration.

## What runs

| Piece | Path | Role |
|-------|------|------|
| Backup script | `deploy/backup-db.sh` | `pg_dump` → gzip → upload to S3, prune local |
| systemd service | `deploy/landline-backup.service` | one-shot unit that runs the script |
| systemd timer | `deploy/landline-backup.timer` | fires the service daily at **02:00 UTC** |

- **S3 bucket:** `landline-backups-235494810658` (keys under `db/`)
- **Object key format:** `db/landline_db_YYYYMMDD_HHMMSS.sql.gz`
- **Remote retention:** 30-day S3 lifecycle rule on the bucket
- **Local retention:** last 7 days under `/opt/landline/backups/db/`
- **Auth:** the EC2 instance role provides S3 access — no static keys

## Install / update on the EC2 host

```bash
cd /opt/landline && git pull origin main          # picks up deploy/ changes
sudo cp deploy/landline-backup.service /etc/systemd/system/
sudo cp deploy/landline-backup.timer   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now landline-backup.timer
```

## Verify it's working

```bash
# Timer is active and shows a NEXT run time:
systemctl list-timers landline-backup.timer

# Run a backup right now and watch it:
sudo systemctl start landline-backup.service
journalctl -u landline-backup.service -n 50 --no-pager

# Confirm a fresh object landed in S3:
aws s3 ls s3://landline-backups-235494810658/db/ --recursive | tail

# Confirm the lifecycle (30-day) rule exists:
aws s3api get-bucket-lifecycle-configuration --bucket landline-backups-235494810658
```

A healthy run prints `Upload complete` and `Done.`, and the script aborts before
upload if the dump is empty or fails a gzip integrity check.

## Restore

```bash
# Pull the chosen dump from S3 and restore into the running container.
aws s3 cp s3://landline-backups-235494810658/db/landline_db_YYYYMMDD_HHMMSS.sql.gz .
gunzip -c landline_db_YYYYMMDD_HHMMSS.sql.gz \
  | docker compose -f docker-compose.prod.yml exec -T postgres \
      psql -U landline -d landline_db
```

The dump uses `--clean --if-exists`, so it drops and recreates objects on restore.
After restoring, run `alembic upgrade head` to be sure the schema is current.
