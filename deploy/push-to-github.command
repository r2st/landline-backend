#!/bin/bash
set -euo pipefail

# =============================================================================
# Push landline-backend to GitHub under Apprend-Technologies org
# Double-click this file in Finder to run in Terminal
# =============================================================================

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
ORG="Apprend-Technologies"
REPO_NAME="landline-backend"

cd "$REPO_DIR"

echo "========================================"
echo "  Push landline-backend to GitHub"
echo "========================================"
echo ""

# Check if gh CLI is available
if ! command -v gh &>/dev/null; then
    echo "ERROR: GitHub CLI (gh) not installed."
    echo "Install: brew install gh"
    echo ""
    echo "Press any key to close..."
    read -n1
    exit 1
fi

# Check auth
if ! gh auth status &>/dev/null 2>&1; then
    echo "Not logged in to GitHub CLI. Running: gh auth login"
    gh auth login
fi

# Initialize git if needed
if [ ! -d .git ]; then
    echo "[1] Initializing git repo..."
    git init
    git add -A
    git commit -m "Initial commit — Landline backend MVP

B2B lead generation API for manufacturing companies (US/Canada).
FastAPI + SQLAlchemy 2.0 + Celery + PostgreSQL + Redis.

Co-Authored-By: Claude <noreply@anthropic.com>"
else
    echo "[1] Git repo already initialized"
    # Stage any unstaged files
    if [ -n "$(git status --porcelain)" ]; then
        git add -A
        git commit -m "Pre-push: stage all files"
    fi
fi

# Create GitHub repo (private)
echo ""
echo "[2] Creating private repo: $ORG/$REPO_NAME"
if gh repo view "$ORG/$REPO_NAME" &>/dev/null 2>&1; then
    echo "  Repo already exists on GitHub"
else
    gh repo create "$ORG/$REPO_NAME" --private --source=. --description "B2B lead generation backend — FastAPI, PostgreSQL, Celery"
    echo "  Created: https://github.com/$ORG/$REPO_NAME"
fi

# Push
echo ""
echo "[3] Pushing to GitHub..."
git remote get-url origin &>/dev/null 2>&1 || git remote add origin "https://github.com/$ORG/$REPO_NAME.git"
git push -u origin main 2>/dev/null || git push -u origin "$(git branch --show-current)"

echo ""
echo "========================================"
echo "  Done! https://github.com/$ORG/$REPO_NAME"
echo "========================================"
echo ""
echo "Press any key to close..."
read -n1
