# Operations

This is a stub for the incident runbook (roadmap Phase 3). It replaces two ad-hoc
shell scripts that were removed from `scripts/` in Phase 0: `fix_server.sh` and
`emergency_fix.sh`. They remain in git history if you need to see what they did.

The operations they covered, to be rewritten as documented, tested procedures:

| Operation | Old script | Planned replacement |
| --- | --- | --- |
| Restart the API service | fix_server.sh `restart` | Documented systemd/Docker commands |
| Flush the Redis cache | fix_server.sh `flush-cache` | Admin API endpoint with audit log |
| Rotate and compress logs | fix_server.sh `rotate-logs` | Built-in log rotation with size caps |
| Kill stuck agent processes | fix_server.sh `kill-agents` | Agent kill switch (Phase 7) |
| Health checks | fix_server.sh `check` | `/healthz` and `/readyz` endpoints (Phase 3) |
| Hard restart | emergency_fix.sh `hard-restart` | Drain-then-restart procedure |
| Roll back a deploy | emergency_fix.sh `rollback` | Versioned releases and documented rollback |
| Disable a provider at runtime | emergency_fix.sh `disable-provider` | Routing policy / admin API |
| Clear hung DB connections | emergency_fix.sh `clear-db-locks` | Connection pool timeouts |
| Full state reset | emergency_fix.sh `full-reset` | Documented, confirmed, backed-up procedure |

## To write

- Provider outage
- High latency
- Cost spike
- Memory growth
- Local model process crash (after the local runtime lands)
