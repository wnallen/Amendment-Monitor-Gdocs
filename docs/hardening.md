# Hardening guide

The monitor autonomously ingests untrusted third-party content (counterparty legal
pages) into a session that can run bash and push to this repository. The controls
below assume that content is hostile until proven boring.

## What is already enforced (after `scripts/setup.py`)

| Layer | Control |
|---|---|
| Credentials | GitHub PAT stored server-side on the deployment, injected at egress by Anthropic's git proxy — never present in the sandbox, unreadable even by hijacked agent code. |
| Container egress | `limited` deny-by-default networking; `allowed_hosts` = github.com, api.github.com, plus exactly the hostnames of active/paused watches (synced from the register on every `setup.py` run). |
| Anthropic-side fetching | `web_fetch` (the fallback fetcher, which runs on Anthropic's servers) is restricted at two layers: under `limited` networking the environment's `allowed_hosts` applies to it, and `setup.py` also pins the tool's own `allowed_domains` to the same register-derived hosts. `web_search` is disabled entirely. |
| Tool permissions | The agent toolset runs with `permission_policy: always_allow`. This is deliberate: a scheduled run has no client attached, so an `auto` or `always_ask` pause would stall the run until someone noticed. The compensating controls are the egress allowlist, the per-run budget, the write-lane rules in the prompt and skill, and the tripwire below. Switch to `auto` only if you also run a webhook handler that answers `session.status_idle` pauses. |
| Determinism | All normalization via `scripts/normalize.py` — the model cannot "re-normalize" its way into fake diffs, and script/nonce churn can't fake changes. `document.selector` scopes the hash to the document body; the normalizer version, charset, selector and pinned `pypdf` version are recorded in each snapshot's meta so a tooling change re-baselines instead of alerting. |
| Spend | Hard dollar budget per fired session ($15 default). |
| Instructions | Agent system prompt + run skill declare fetched content untrusted-data-only and confine writes to `snapshots/`, `alerts/`, `runs/`, `watch-state.json`. Useful, but prompts are not a security boundary — hence the next section. |
| Tripwire | `.github/workflows/protected-paths-guard.yml` opens a security issue and fails the push check when a monitor-attributed commit touches protected paths. It walks the full `before..after` range through the API (the push payload lists at most 20 commits). Detection only: commit authorship is spoofable. |
| Code gate | `.github/workflows/ci.yml` runs the register lint, tests, ruff and mypy on every push; the scheduled agent mounts the default branch, so this is what keeps a broken normalizer out of production. |

## The remaining gap: write-path enforcement

The PAT can push to the default branch — the same branch each run mounts, including
`.claude/skills/` (the agent's own instructions). Injected instructions that survive
to a push could persist themselves into the next run. Closing this properly requires
GitHub-side (server-side) enforcement:

### Recommended: machine account + push ruleset (~10 minutes)

1. **Create a machine account** (a separate GitHub user, e.g. `yourorg-amendment-bot`)
   and invite it as a collaborator on this repo with Write access. Issue the
   fine-grained PAT (Contents R/W + Issues R/W, this repo only) **from that account**
   and re-run `GITHUB_PAT=... python scripts/setup.py`.
   This alone is worth doing: pushes and issues are now attributable to a
   server-verified identity, and revoking the monitor's access never touches your own.
2. **Add a ruleset** (Repo → Settings → Rules → Rulesets → *New branch ruleset*):
   - Target: default branch. Enforcement: Active.
   - Rule: **Restrict file paths** — block changes to `.claude/**`, `agent/**`,
     `scripts/**`, `tests/**`, `docs/**`, `.github/**`, `config/**`,
     `register/watch-register.yaml`, `register/README.md`, `README.md`,
     `requirements.txt`, `requirements-dev.txt`, `pyproject.toml`.
   - Bypass list: your own account (and admins) — so humans edit methodology freely
     while the machine account physically cannot.
3. Put the machine account's login into `MONITOR_ACTORS` in
   `.github/workflows/protected-paths-guard.yml` so the tripwire keys on the
   server-verified pusher instead of spoofable commit metadata.

Availability note: rulesets are enforceable on public repos on all plans; on private
repos they require Team/Enterprise. If this repo is private on a free plan, the
machine account (step 1) plus the tripwire workflow is the practical fallback — and
review any `security`-labeled issue before the next scheduled run.

## Egress

If a manual run (`python scripts/run_now.py`) shows `git push` or issue-filing
failing under `limited` networking, the git proxy route isn't covered by the current
allowlist in your project's configuration — temporarily set
`agent/environment.yaml` networking back to `type: unrestricted`, re-run setup,
and verify; then re-tighten and confirm which host was missing. Keep
`allow_package_managers: true` (needed for `pypdf` on PDF watches — the skill
installs the exact version pinned in `requirements-dev.txt`; bump both together).

After **adding watches**, always re-run `python scripts/setup.py` — otherwise the new
counterparty host is blocked at both layers and the run reports fetch failures.

## Alert-channel notes

- Issue bodies quote counterparty text. If any downstream automation reads
  `amendment-alert` issues, treat their content as untrusted data.
- For Slack alerts, use a vault `environment_variable` credential holding a bot token
  (header injection only, `allowed_hosts: ["slack.com"]`) and `chat.postMessage`.
  Incoming-webhook URLs cannot be vaulted — path-embedded secrets are not substituted
  at egress.
