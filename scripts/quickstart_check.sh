#!/usr/bin/env bash
# The README quickstart, executed: install, set one key, run, send one curl.
# Used by CI. Needs a Python environment with the package installed.
set -euo pipefail
export SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
export OPENAI_API_KEY="sk-quickstart-dummy"
export ENVIRONMENT=test
PORT="${PORT:-8743}"
uvicorn api.server:app --port "$PORT" >/tmp/caips-quickstart.log 2>&1 &
PID=$!
trap 'kill $PID 2>/dev/null || true' EXIT
for _ in $(seq 1 30); do
  curl -fs "localhost:$PORT/health" >/dev/null && break
  sleep 1
done
curl -fs "localhost:$PORT/health"
TOKEN=$(python -c "from api.auth import create_access_token; print(create_access_token('user:me', ['completions:read', 'completions:write']))")
# A dummy key cannot reach OpenAI, so we only require an authenticated,
# well-formed answer (not a 401/404): models list works offline.
curl -fs -H "Authorization: Bearer $TOKEN" "localhost:$PORT/v1/models" | python -c "import json,sys; assert json.load(sys.stdin)['object']=='list'"
echo "quickstart ok"
