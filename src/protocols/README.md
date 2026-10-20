# protocols/

**Status: experimental. Not used by the gateway's request path.**

- `distributed_agent.py`: message envelope and in-process coordination for agents. `AgentManager` uses it for registration and heartbeats.
- `secure_protocol.py`: a custom authenticated, encrypted transport (handshake, session keys, framing, replay protection) for connections between nodes.

## Security note

`secure_protocol.py` is hand-written cryptographic protocol code. It has **not been audited** and nothing in the server depends on it. Do not use it to protect real traffic. Use TLS (or mTLS) between nodes instead.

## Plan

Where a cross-node protocol is needed, prefer established standards (HTTP/TLS, MCP) over custom wire formats. These modules will be reused only where the new design needs them and replaced otherwise.
