#!/bin/bash
set -euo pipefail

# =============================================================================
# Landline Backend — EC2 Setup Script
# =============================================================================
# Run this ONCE on a fresh or existing EC2 to install all Landline dependencies.
# After running this, use deploy.sh for subsequent deploys.
#
# Designed to coexist with existing Apprend services on the same t3.micro.
# Uses Docker Compose to keep Landline isolated from the host system.
#
# Usage:  chmod +x ec2-setup.sh && sudo ./ec2-setup.sh
# =============================================================================

echo "========================================"
echo "  Landline Backend — EC2 Setup"
echo "========================================"

# Check we're root or sudo
if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: Run with sudo"
    exit 1
fi

LANDLINE_DIR="/opt/landline"
LANDLINE_USER="ubuntu"

# --- 1. System updates ---
echo ""
echo "[1/6] Updating system packages..."
apt-get update -qq
apt-get upgrade -y -qq

# --- 2. Install Docker (if not present) ---
echo ""
echo "[2/6] Setting up Docker..."
if command -v docker &>/dev/null; then
    echo "  Docker already installed: $(docker --version)"
else
    curl -fsSL https://get.docker.com | sh
    usermod -aG docker "$LANDLINE_USER"
    systemctl enable docker
    systemctl start docker
    echo "  Docker installed: $(docker --version)"
fi

# Install Docker Compose plugin (if not present)
if docker compose version &>/dev/null; then
    echo "  Docker Compose already installed: $(docker compose version)"
else
    apt-get install -y -qq docker-compose-plugin
    echo "  Docker Compose installed: $(docker compose version)"
fi

# --- 3. Install Git (if not present) ---
echo ""
echo "[3/6] Checking Git..."
if command -v git &>/dev/null; then
    echo "  Git already installed: $(git --version)"
else
    apt-get install -y -qq git
    echo "  Git installed: $(git --version)"
fi

# --- 4. Create Landline directory structure ---
echo ""
echo "[4/6] Setting up directory structure..."
mkdir -p "$LANDLINE_DIR"
mkdir -p "$LANDLINE_DIR/logs"
mkdir -p "$LANDLINE_DIR/backups"
chown -R "$LANDLINE_USER:$LANDLINE_USER" "$LANDLINE_DIR"

# --- 5. Set up Nginx for Landline (if nginx exists) ---
echo ""
echo "[5/6] Configuring Nginx..."
if command -v nginx &>/dev/null; then
    # Generate self-signed SSL cert for staging
    mkdir -p /etc/nginx/ssl
    if [ ! -f /etc/nginx/ssl/landline-staging.crt ]; then
        openssl req -x509 -nodes -days 365 \
            -newkey rsa:2048 \
            -keyout /etc/nginx/ssl/landline-staging.key \
            -out /etc/nginx/ssl/landline-staging.crt \
            -subj "/C=US/ST=NY/L=NYC/O=Apprend/CN=landline-staging"
        echo "  Self-signed SSL cert generated"
    fi
    echo "  Nginx config will be set up during deploy"
else
    echo "  Nginx not found — installing..."
    apt-get install -y -qq nginx
    mkdir -p /etc/nginx/ssl
    openssl req -x509 -nodes -days 365 \
        -newkey rsa:2048 \
        -keyout /etc/nginx/ssl/landline-staging.key \
        -out /etc/nginx/ssl/landline-staging.crt \
        -subj "/C=US/ST=NY/L=NYC/O=Apprend/CN=landline-staging"
    systemctl enable nginx
    echo "  Nginx installed and SSL cert generated"
fi

# --- 6. Set up firewall rule (allow 8443 for Landline API) ---
echo ""
echo "[6/6] Firewall check..."
if command -v ufw &>/dev/null; then
    ufw allow 8443/tcp comment "Landline API HTTPS" 2>/dev/null || true
    echo "  Port 8443 allowed through UFW"
else
    echo "  No UFW — ensure AWS security group allows port 8443"
fi

echo ""
echo "========================================"
echo "  Setup complete!"
echo "========================================"
echo ""
echo "Next steps:"
echo "  1. Clone the prospect-backend repo into $LANDLINE_DIR"
echo "  2. Copy .env.example to .env.production and fill in secrets"
echo "  3. Run: cd $LANDLINE_DIR && docker compose -f docker-compose.prod.yml up -d"
echo "  4. Run migrations: docker exec landline-api alembic upgrade head"
echo ""
echo "Memory note: t3.micro has 1 GB RAM."
echo "  Landline containers use ~400 MB. If Apprend uses >500 MB, upgrade to t3.small."
echo ""
