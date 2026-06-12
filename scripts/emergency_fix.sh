#!/usr/bin/env bash
# CoreAI Protocol Suite - Emergency Fix Script
# Nuclear options for when fix_server.sh isn't enough.
# Run only when the service is completely unresponsive.
#
# Usage:
#   ./scripts/emergency_fix.sh hard-restart     - kill -9 and restart from scratch
#   ./scripts/emergency_fix.sh rollback         - roll back to last known good deploy
#   ./scripts/emergency_fix.sh disable-provider - take a provider offline at runtime
#   ./scripts/emergency_fix.sh clear-db-locks   - kill hung DB connections
#   ./scripts/emergency_fix.sh full-reset       - full state wipe and cold start

set -euo pipefail

SERVICE="coreai-api"
APP_DIR="/srv/coreai"
BACKUP_DIR="/srv/coreai-backups"
REDIS_CLI="redis-cli"
DB_HOST="${DB_HOST:-localhost}"
DB_USER="${DB_USER:-coreai_prod_user}"
DB_NAME="${DB_NAME:-coreai_production}"

CMD="${1:-}"
ARG2="${2:-}"

log()  { echo "[$(date '+%H:%M:%S')] $*"; }
warn() { echo "[$(date '+%H:%M:%S')] WARNING: $*" >&2; }
die()  { echo "[$(date '+%H:%M:%S')] ERROR: $*" >&2; exit 1; }

confirm() {
    read -rp "$1 [y/N] " ans
    [[ "$ans" =~ ^[Yy]$ ]] || die "Aborted."
}

case "$CMD" in

    hard-restart)
        log "Hard restart initiated..."
        confirm "This will kill all CoreAI processes. Continue?"

        # Kill everything — including stuck uvicorn workers
        log "  Killing all coreai processes..."
        pkill -9 -f "uvicorn api.server" 2>/dev/null && log "  Killed uvicorn workers." || log "  No uvicorn workers found."
        pkill -9 -f "python -m api.server" 2>/dev/null || true
        sleep 1

        # Clear any leftover PID files
        rm -f /tmp/coreai*.pid

        # Cold start via systemd
        log "  Starting $SERVICE via systemd..."
        sudo systemctl reset-failed "$SERVICE" 2>/dev/null || true
        sudo systemctl start "$SERVICE"
        sleep 3

        STATUS=$(sudo systemctl is-active "$SERVICE")
        if [[ "$STATUS" == "active" ]]; then
            log "Service back online."
        else
            die "Service failed to come back. Run: journalctl -u $SERVICE -n 100"
        fi
        ;;

    rollback)
        log "Rolling back to previous deploy..."

        DEPLOYS=$(ls -t "$BACKUP_DIR" 2>/dev/null | head -5)
        if [[ -z "$DEPLOYS" ]]; then
            die "No backups found in $BACKUP_DIR"
        fi

        log "Available backups:"
        echo "$DEPLOYS" | nl -ba

        read -rp "Select backup number to restore: " SEL
        TARGET=$(echo "$DEPLOYS" | sed -n "${SEL}p")
        [[ -n "$TARGET" ]] || die "Invalid selection."

        confirm "Restore from $TARGET?"

        log "  Stopping service..."
        sudo systemctl stop "$SERVICE"

        log "  Restoring $TARGET..."
        rsync -a --delete "$BACKUP_DIR/$TARGET/" "$APP_DIR/"

        log "  Running migrations (safe — up only)..."
        cd "$APP_DIR"
        source .venv/bin/activate
        python -m database.migrations up

        log "  Starting service..."
        sudo systemctl start "$SERVICE"
        sleep 2

        STATUS=$(sudo systemctl is-active "$SERVICE")
        log "Rollback complete. Service: $STATUS"
        ;;

    disable-provider)
        PROVIDER="${ARG2:-}"
        [[ -n "$PROVIDER" ]] || die "Usage: $0 disable-provider <provider_name>"

        log "Disabling provider: $PROVIDER"
        confirm "This will update routing.yml and hot-reload. Continue?"

        ROUTING_FILE="$APP_DIR/config/routing.yml"
        [[ -f "$ROUTING_FILE" ]] || die "routing.yml not found at $ROUTING_FILE"

        # Set enabled: false for the provider
        sed -i "/^  $PROVIDER:/,/enabled:/ s/enabled: true/enabled: false/" "$ROUTING_FILE"
        log "  Updated routing.yml — $PROVIDER disabled."

        # Signal uvicorn to reload config (SIGHUP triggers graceful reload)
        PID=$(pgrep -f "uvicorn api.server" | head -1)
        if [[ -n "$PID" ]]; then
            kill -HUP "$PID"
            log "  Sent SIGHUP to uvicorn (PID $PID) — config reloading."
        else
            warn "Uvicorn PID not found — restart required to apply change."
        fi
        ;;

    clear-db-locks)
        log "Clearing hung database connections..."
        confirm "This will terminate idle DB connections older than 10 minutes. Continue?"

        QUERY="SELECT pg_terminate_backend(pid) FROM pg_stat_activity
               WHERE datname = '$DB_NAME'
               AND state = 'idle'
               AND query_start < NOW() - INTERVAL '10 minutes'
               AND pid <> pg_backend_pid();"

        COUNT=$(psql -h "$DB_HOST" -U "$DB_USER" -d "$DB_NAME" -tAc "$QUERY" 2>/dev/null | grep -c 't' || echo 0)
        log "Terminated $COUNT idle connection(s)."
        ;;

    full-reset)
        warn "FULL RESET will wipe in-memory state, flush cache, and cold-restart."
        confirm "This is destructive. Are you absolutely sure?"
        confirm "Last chance — this affects production. Really continue?"

        log "Step 1/4 — Stopping service..."
        sudo systemctl stop "$SERVICE"

        log "Step 2/4 — Flushing Redis..."
        KEYS=$($REDIS_CLI DBSIZE 2>/dev/null || echo "?")
        $REDIS_CLI FLUSHDB 2>/dev/null && log "  Flushed $KEYS Redis key(s)." || warn "Redis flush failed."

        log "Step 3/4 — Clearing temp files and stale locks..."
        rm -f /tmp/coreai*.pid /tmp/coreai*.lock
        find "$APP_DIR/logs" -name "*.log" -exec truncate -s 0 {} \;

        log "Step 4/4 — Cold starting service..."
        sudo systemctl reset-failed "$SERVICE" 2>/dev/null || true
        sudo systemctl start "$SERVICE"
        sleep 3

        STATUS=$(sudo systemctl is-active "$SERVICE")
        log "Full reset complete. Service: $STATUS"
        ;;

    *)
        echo "Usage: $0 {hard-restart|rollback|disable-provider|clear-db-locks|full-reset}"
        echo ""
        echo "  hard-restart               Kill -9 all workers, cold start via systemd"
        echo "  rollback                   Restore previous deploy from backup"
        echo "  disable-provider <name>    Take a provider offline at runtime"
        echo "  clear-db-locks             Kill idle DB connections older than 10 min"
        echo "  full-reset                 Wipe state, flush cache, cold restart"
        exit 1
        ;;
esac
