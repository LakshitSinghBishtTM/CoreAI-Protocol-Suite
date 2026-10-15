# Security policy

CaiPS is alpha software. Run it on a trusted network behind TLS, and do not expose it directly to the internet without a reverse proxy and your own access controls.

## Reporting a vulnerability

Please **do not open a public issue** for security problems. Use GitHub's private reporting instead:

1. Go to the repository's **Security** tab.
2. Choose **Report a vulnerability** and describe the issue, how to reproduce it, and the impact.

(If that button is not visible, the maintainer has not enabled private vulnerability reporting yet. It is under Settings, Code security.)

## Scope

In scope: the API server, authentication and authorization, provider key handling, and anything that could leak keys, prompts or responses.

Known not production-ready, so reports about them are low priority:

- `protocols/secure_protocol.py` is unaudited custom cryptography and is not used by the server.
- Agents have no tool sandboxing because they have no tools yet.

## Secrets

Never commit `.env` files or keys. Copy `.env.example` to `.env` locally; `.gitignore` excludes it. If you find a credential in the repository or its history, treat it as compromised and rotate it.
