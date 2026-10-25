# Secret history audit

Scope: all 191 commits on all refs, searched for file names `.env`, `.env.production`, `.netrc`, `.pypirc` and `keys/`.

## Findings
These paths exist in git history (none are in the current tree):

| Path | Content in history |
| --- | --- |
| `.env`, `.env.production` | Provider API key variables |
| `.netrc` | Hosts, logins and passwords for GitHub and an internal GitLab |
| `.pypirc` | PyPI upload configuration |
| `keys/gpg_private.txt`, `keys/ssh_private.txt` | Private key blocks (PGP and OpenSSH) |

Values are deliberately not reproduced here. Whether each value is real or a placeholder was not verified; treat all of them as compromised.

## Required actions (repository owner)
1. Rotate every provider API key, the GitHub and GitLab credentials, any PyPI token, and the PGP and SSH keys. Remove the old public keys from any service that trusts them.
2. Purge the paths from history with `git filter-repo --path .env --path .env.production --path .netrc --path .pypirc --path keys --invert-paths`, then force-push all refs. Forks and clones keep the old data, so rotation matters more than the purge.
3. Ask GitHub support to clear cached views of the old commits.
4. Add a `.gitignore` entry for each path (check it is present) and keep gitleaks in CI.

Rewriting history and rotating credentials were not done here: both need the owner's accounts.
