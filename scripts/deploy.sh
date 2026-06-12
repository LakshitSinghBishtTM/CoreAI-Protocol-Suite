#!/usr/bin/env bash
# CoreAI Protocol Suite - Deploy Script
# Deploys to production server over SSH, runs migrations, restarts service.
# Usage: ./scripts/deploy.sh [--env staging|production] [--skip-migrate]

set -euo pipefail

ENV="production"
SKIP_MIGRATE=false
BRANCH="main"

# ---- arg parsing ----
while [[ $# -gt 0 ]]; do
    case $1 in
        --env) ENV="$2"; shift 2 ;;
        --skip-migrate) SKIP_MIGRATE=true; shift ;;
        --branch) BRANCH="$2"; shift 2 ;;
        *) echo "Unknown arg: $1"; exit 1 ;;
    esac
done

# ---- config ----
if [[ "$ENV" == "production" ]]; then
    REMOTE_HOST="prod-api.coreai.internal"
    REMOTE_USER="deploy"
    REMOTE_DIR="/srv/coreai"
    SERVICE_NAME="coreai-api"
elif [[ "$ENV" == "staging" ]]; then
    REMOTE_HOST="staging-api.coreai.internal"
    REMOTE_USER="deploy"
    REMOTE_DIR="/srv/coreai-staging"
    SERVICE_NAME="coreai-api-staging"
else
    echo "Unknown environment: $ENV"
    exit 1
fi

echo "========================================"
echo " CoreAI Deploy"
echo " Env     : $ENV"
echo " Branch  : $BRANCH"
echo " Host    : $REMOTE_HOST"
echo "========================================"

# ---- pre-deploy checks ----
echo "[1/5] Pre-deploy checks..."
if ! git diff --quiet; then
    echo "  WARNING: uncommitted changes in working tree"
fi

CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)
if [[ "$CURRENT_BRANCH" != "$BRANCH" ]]; then
    echo "  WARNING: deploying from branch '$CURRENT_BRANCH', expected '$BRANCH'"
fi

COMMIT=$(git rev-parse --short HEAD)
echo "  Deploying commit: $COMMIT"

# ---- push to remote ----
echo "[2/5] Syncing files to $REMOTE_HOST..."
rsync -az --exclude='.git' \
          --exclude='__pycache__' \
          --exclude='*.pyc' \
          --exclude='.env' \
          --exclude='temp/' \
          --exclude='logs/' \
          ./ "$REMOTE_USER@$REMOTE_HOST:$REMOTE_DIR/"

# ---- install deps ----
echo "[3/5] Installing dependencies..."
ssh "$REMOTE_USER@$REMOTE_HOST" "
    cd $REMOTE_DIR
    source .venv/bin/activate
    pip install -r requirements.txt -q
"

# ---- migrations ----
if [[ "$SKIP_MIGRATE" == false ]]; then
    echo "[4/5] Running migrations..."
    ssh "$REMOTE_USER@$REMOTE_HOST" "
        cd $REMOTE_DIR
        source .venv/bin/activate
        python -m database.migrations up
    "
else
    echo "[4/5] Skipping migrations (--skip-migrate)"
fi

# ---- restart service ----
echo "[5/5] Restarting $SERVICE_NAME..."
ssh "$REMOTE_USER@$REMOTE_HOST" "sudo systemctl restart $SERVICE_NAME"
sleep 2
ssh "$REMOTE_USER@$REMOTE_HOST" "sudo systemctl is-active $SERVICE_NAME"

echo ""
echo "Deploy complete. Commit $COMMIT live on $ENV."
