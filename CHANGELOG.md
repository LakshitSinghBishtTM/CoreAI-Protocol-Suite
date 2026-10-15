# Changelog

## 0.1.0 (unreleased)

The previous `1.0.0` label did not match the project's state; versions now start at 0.1.0 (alpha).

### Changed
- Runtime dependencies cut from 50+ to 15 (only what the code imports). Dev tools are the `dev` extra; Postgres support is the `postgres` extra.
- One `pyproject.toml` replaces `setup.py`, `setup.cfg` and `pytest.ini`. Wheels now include all packages (`agents`, `neural`, `protocols` and `database` were previously missing from non-editable installs).
- CI runs on Python 3.11 and 3.12, builds and imports the wheel, and no longer contains hardcoded keys.
- README rewritten to describe what the code does today.

### Removed
- `neural/consciousness_detection.py` and its wiring in `AgentManager` (memory leak, never exposed by the API).
- Tracked logs, database dumps, Terraform state, shell history and two ad-hoc ops scripts (see `docs/operations.md`).

### Added
- `.env.example`, `SECURITY.md`, status notes in `agents/`, `protocols/` and `neural/`, and smoke tests for `AgentManager`.
