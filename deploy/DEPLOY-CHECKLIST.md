# Landline — Deployment Checklist

## Pre-deployment (do these first)

- [ ] Push landline-backend to GitHub: double-click `deploy/push-to-github.command`
- [ ] Push landline-frontend to GitHub: double-click `landline-frontend/deploy/push-to-github.command`
- [ ] Open EC2 security group `launch-wizard-3` in AWS Console
  - Add inbound rule: SSH (22) from your IP
  - Add inbound rule: Custom TCP 8443 from 0.0.0.0/0 (Landline API)
  - Keep existing Apprend rules intact

## Backend deployment (EC2)

1. SSH into EC2:
   ```bash
   ssh -i keys/vk.pem ubuntu@18.209.12.106
   ```

2. Run initial setup (first time only):
   ```bash
   cd /opt
   sudo git clone https://github.com/Apprend-Technologies/landline-backend.git forge
   cd landline
   sudo chmod +x deploy/ec2-setup.sh
   sudo ./deploy/ec2-setup.sh
   ```

3. Configure environment:
   ```bash
   cp .env.example .env.production
   nano .env.production  # Fill in real secrets
   ```

4. Start services:
   ```bash
   docker compose -f docker-compose.prod.yml up -d
   ```

5. Run migrations:
   ```bash
   docker exec landline-api alembic upgrade head
   ```

6. Set up nginx:
   ```bash
   sudo cp deploy/nginx-landline.conf /etc/nginx/sites-available/landline
   sudo ln -sf /etc/nginx/sites-available/landline /etc/nginx/sites-enabled/
   sudo nginx -t && sudo systemctl reload nginx
   ```

7. Verify:
   ```bash
   curl http://127.0.0.1:8001/health
   ```

## Frontend deployment (Amplify)

1. In AWS Amplify Console, create a new app
2. Connect to `Apprend-Technologies/landline-frontend` repo
3. Framework: Vite (auto-detected from amplify.yml)
4. Environment variables:
   - `VITE_API_URL` = `https://18.209.12.106:8443` (or your domain)
   - `VITE_APP_NAME` = `Landline`
5. Enable basic auth (staging): same pattern as Apprend staging
6. Deploy

## Subsequent deploys

```bash
ssh -i keys/vk.pem ubuntu@18.209.12.106
cd /opt/landline
sudo ./deploy/deploy.sh
```

Rollback: `sudo ./deploy/deploy.sh --rollback`

## Resource budget (t3.micro, 1 GB RAM)

| Service         | Expected RAM |
|-----------------|-------------|
| Apprend (existing) | ~300-500 MB |
| Landline API (Docker) | ~150 MB |
| Landline PG + Redis   | ~200 MB |
| Landline Celery worker | ~100 MB |
| **Total**           | **~750-950 MB** |

If you see OOM kills, upgrade to t3.small (2 GB RAM).

## Required secrets for .env.production

| Variable | Source | Required? |
|----------|--------|-----------|
| POSTGRES_PASSWORD | Generate a strong password | Yes |
| ANTHROPIC_API_KEY | console.anthropic.com | Yes (for AI scoring) |
| APOLLO_API_KEY | app.apollo.io | Yes (for enrichment) |
| AWS_ACCESS_KEY_ID | IAM user with SES permissions | Yes (for email) |
| AWS_SECRET_ACCESS_KEY | Same IAM user | Yes |
| CENSUS_API_KEY | api.census.gov | Optional |
