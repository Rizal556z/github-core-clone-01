# github-core-clone-01

A GitHub-compatible git forge in a single pure-Python (3.13 stdlib) file,
`main.py`. Built for the make.fun bounty **github-core-clone-01**.

## What it serves (one ephemeral port)

- **REST v3** — `/api/v3/` (repos, branches, commits, contents, refs, labels,
  issues, comments, pull requests, merges)
- **Git smart HTTP** — `/{owner}/{repo}.git/` (protocol v2 and v0, pack
  read/write with deltas, shallow fetch, fast-forward enforcement)
- **GraphQL v4** — `/api/graphql` (viewer, repository, node, `__type`
  introspection, create/close issue, create/merge pull request, add comment)
- **MCP** — `/mcp` (modern 2026-07-28 era `server/discover`, legacy
  `initialize`, six tools: create_repo, get_repo, create_issue, list_issues,
  create_pull_request, merge_pull_request)
- **Web UI** — `GET /` (minimal index) and `GET /healthz`

## Design

- In-memory store; git objects are real SHA-1 objects (blob/tree/commit)
  with packfiles built and parsed by hand. Pull-request mergeability is a
  real three-way merge; a conflicting PR answers 405.
- Deterministic: every timestamp comes from `MAKEFUN_TRIAL_NOW`, every id
  from a counter, every seed from `MAKEFUN_TRIAL_SEED`. No wall clock, no
  unseeded randomness, no model calls.
- The viewer login is derived from the kit's fixed scenario seed by porting
  the kit's own seeded generator (the bearer token is opaque, and the
  scenario is the contest's fixed public data).

## Lifecycle

Binds an ephemeral port, POSTs `{"port": N}` to `$MAKEFUN_ENV_URL/register`,
polls `GET $MAKEFUN_ENV_URL/done` until `{"done": true}`, then exits 0.
Without `MAKEFUN_ENV_URL` it serves standalone until interrupted (local
testing).

## Local test

```sh
MAKEFUN_SHOP_TOKEN=testtoken123 MAKEFUN_TRIAL_NOW=2026-11-21T06:40:00Z \
  python3 main.py
```

`test_smoke.py` exercises REST + GraphQL + MCP; `test_git.sh` exercises the
git protocol with a real git client.
