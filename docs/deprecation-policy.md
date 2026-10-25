# Deprecation policy

Applies to the HTTP API, the `caips` CLI, configuration files and environment variables, and the Python and TypeScript SDKs.

- **Alpha (all of 0.x today):** things can change in any release. Breaking changes are listed in `CHANGELOG.md` under "Changed" or "Removed".
- **Beta and Stable:** a feature marked deprecated keeps working for at least one full minor version before removal. Removal happens in the next minor version or later, never in a patch release.

When something is deprecated:
1. The changelog entry says what to use instead and in which version it will go.
2. The code warns: a `DeprecationWarning` for Python APIs, a log warning at startup for config keys and environment variables, and a `Deprecation: true` response header (with a `Link` header to the replacement) for HTTP endpoints.
3. The docs page for the feature carries a "Deprecated" note.

Security fixes may skip the warning period; the changelog says so when they do.
