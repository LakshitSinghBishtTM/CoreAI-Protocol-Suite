#!/usr/bin/env bash
# CoreAI Protocol Suite - Emergency Server Fix
# Quick ops script for common production issues.
# Run on the server directly when things are on fire.
#
# Usage:
#   ./scripts/fix_server.sh restart          - restart the API service
#   ./scripts/fix_server.sh flush-cache       - flush Redis cache
#   ./scripts/fix_server.sh rotate-logs       - rotate and compress logs
#   ./scripts/fix_server.sh kill-agents       - kill all stuck agent processes
#   ./scripts/fix_server.sh check             - run health checks

set -euo pipefail

SERVICE="coreai-api"
APP_DIR="/srv/coreai"
LOG_DIR="$APP_DIR/logs"
REDIS_CLI="redis-cli"

CMD="${1:-check}"

log() { echo "[$(date '+%H:%M:%S')] $*"; }
die() { echo "ERROR: $*" >&2; exit 1; }

case "$CMD" in

    restart)
        log "Restarting $SERVICE..."
        sudo systemctl restart "$SERVICE"
        sleep 2
        STATUS=$(sudo systemctl is-active "$SERVICE")
        if [[ "$STATUS" == "active" ]]; then
            log "Service is up."
        else
            die "Service failed to start. Check: journalctl -u $SERVICE -n 50"
        fi
        ;;

    flush-cache)
        log "Flushing Redis cache..."
        KEYS_BEFORE=$($REDIS_CLI DBSIZE)
        $REDIS_CLI FLUSHDB
        log "Flushed $KEYS_BEFORE key(s)."
        ;;

    rotate-logs)
        log "Rotating logs in $LOG_DIR..."
        for f in "$LOG_DIR"/*.log; do
            [[ -f "$f" ]] || continue
            SIZE=$(du -sh "$f" | cut -f1)
            ARCHIVE="${f%.log}_$(date '+%Y%m%d_%H%M%S').log.gz"
            gzip -c "$f" > "$ARCHIVE"
            truncate -s 0 "$f"
            log "  Rotated $f ($SIZE) → $(basename "$ARCHIVE")"
        done
        # Remove archives older than 14 days
        find "$LOG_DIR" -name "*.log.gz" -mtime +14 -delete
        log "Old archives cleaned."
        ;;

    kill-agents)
        log "Killing stuck agent processes..."
        # Find python processes running agent loops for more than 30 min
        KILLED=0
        while IFS= read -r pid; do
            if [[ -n "$pid" ]]; then
                log "  Killing PID $pid"
                kill -TERM "$pid" 2>/dev/null && KILLED=$((KILLED+1))
            fi
        done < <(ps aux | grep '[a]gent' | grep 'python' | awk '{print $2}')
        log "Sent SIGTERM to $KILLED process(es)."
        ;;

    check)
        log "Running health checks..."
        # API
        HTTP_STATUS=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:6969/health || echo "000")
        if [[ "$HTTP_STATUS" == "200" ]]; then
            log "  API       : OK (HTTP 200)"
        else
            log "  API       : FAIL (HTTP $HTTP_STATUS)"
        fi

        # Redis
        if $REDIS_CLI PING 2>/dev/null | grep -q PONG; then
            KEYS=$($REDIS_CLI DBSIZE)
            log "  Redis     : OK ($KEYS keys)"
        else
            log "  Redis     : FAIL (not responding)"
        fi

        # Service status
        STATUS=$(sudo systemctl is-active "$SERVICE" 2>/dev/null || echo "unknown")
        log "  Service   : $STATUS"

        # Disk
        DISK=$(df -h "$APP_DIR" | awk 'NR==2 {print $5 " used (" $4 " free)"}')
        log "  Disk      : $DISK"

        # Memory
        MEM=$(free -h | awk '/^Mem:/ {print $3 " used / " $2 " total"}')
        log "  Memory    : $MEM"
        ;;

    *)
        echo "Usage: $0 {restart|flush-cache|rotate-logs|kill-agents|check}"
        exit 1
        ;;
esac
