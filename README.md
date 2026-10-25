# Amendment Monitor

A managed agent that watches the terms documents your executed agreements incorporate by
reference — DPAs, AUPs, subprocessor lists, SLAs, security addenda, AI/model terms — and
catches unilateral amendments before the notice or objection window expires silently.
It runs on Anthropic's Managed Agents platform on a weekly schedule: each run fetches
every watched URL, snapshots and diffs it, classifies what changed, maps which executed
agreements are exposed, computes the contractual clocks (30-day subprocessor objections,
30–60-day material-change windows, and whatever else the register records), and raises a
GitHub issue with recommended next steps. All state — the watch register, snapshots,
diffs, alerts, run reports — lives in this repository as plain YAML/JSON/Markdown:
portable and user-held.

## Important

Every output from this tool is a draft for attorney review — not legal advice, not a legal conclusion, not a substitute for a lawyer. This tool may make review faster; it does not replace it.

This tool does not represent the creator's legal positions: it is a tool. Where a tool includes a checklist item, a suggested framework, a risk flag, or a characterization of case law or regulatory guidance, that is an aid to the reviewing attorney's own analysis, not a statement of the author's view of the law. The law in many of these areas is unsettled and evolving. The attorney using the tool — not the tool, and not the author — is responsible for the legal positions taken in their work product.

## How it works

```
agent/*.yaml  ──setup.py──▶  Anthropic Managed Agents
                              ├─ Agent      (persona + hard rules, versioned)
                              ├─ Environment (cloud sandbox template)
                              └─ Scheduled deployment (weekly cron + $ budget)
                                     │ fires a fresh session per run
                                     ▼
                              Session mounts THIS repo
                              ├─ loads .claude/skills/amendment-monitor-run
                              ├─ scripts/check.py: curl each active watch
                              │    (egress allowlisted to register hosts) →
                              │    scripts/normalize.py (versioned, selector-
                              │    scoped) → sha256 → snapshot → diff → work
                              │    list; model judgment only on what changed
                              ├─ classify → exposure → clocks
                              ├─ files GitHub issues for actionable changes
                              └─ commits + pushes snapshots/state/alerts/report
```

Three design choices worth knowing:

- **The repo is the system of record.** The agent platform holds only the agent
  config and the schedule. Clone the repo and you hold every snapshot, alert, and
  clock computation. Nothing to export, nothing lost if you leave the platform.
- **The methodology is a repo skill.** `.claude/skills/amendment-monitor-run/SKILL.md`
  is loaded by each session at start, so the detection/classification/clock rules are
  version-controlled and reviewable next to the register — edit them with a PR, and
  the next run picks them up.
- **Each firing is rubric-graded.** The deployment kicks off with an outcome
  definition (`agent/deployment.yaml`), so the platform grades each run against
  acceptance criteria (every active watch checked, every change classified, clocks
  computed, results pushed) and makes the agent fix shortfalls before finishing.

## Repository layout

| Path | Owner | Contents |
|---|---|---|
| `register/watch-register.yaml` | **you** | The watch register — one entry per watched URL, with exposure mapping and notice/objection rights. See `register/README.md`. |
| `register/watch-state.json` | agent | Machine state: last hashes, check times, open alerts. |
| `snapshots/<id>/` | agent | Append-only normalized snapshots + metadata (sha256, fetch time, page-displayed dates). |
| `alerts/<id>/<run>/` | agent | Diff + analysis for each detected change. |
| `runs/` | agent | One report per run. |
| `.claude/skills/amendment-monitor-run/` | you (via PR) | The run methodology the agent follows. |
| `agent/` | you | Agent, environment, and deployment definitions (control plane). |
| `scripts/` | you | `setup.py` (provision/sync), `run_now.py` (manual run), `validate_register.py` (register lint), `check.py` (the deterministic phase of a run: fetch, normalize, compare, snapshot, diff, state — emits a JSON work list so the agent only reasons about changed watches), `normalize.py` (deterministic document normalizer used in-run), `registerlib.py` (shared register helpers). |
| `tests/` | you | Regression tests for the normalizer, register lint and allowlist derivation — run by CI. |
| `docs/` | you | `hardening.md` — security controls and GitHub-side setup. |
| `config/anthropic.json` | setup.py | Resource IDs (not secrets) — commit it. |

## Setup

Prerequisites: Python 3.10+, an Anthropic API key with Managed Agents (beta) access,
and a GitHub fine-grained PAT for this repository with **Contents: Read and write**
and **Issues: Read and write**.

```bash
pip install -r requirements.txt

export ANTHROPIC_API_KEY=sk-ant-...
export GITHUB_PAT=github_pat_...        # stored server-side on the deployment, never echoed

python scripts/setup.py                 # creates environment + agent + weekly deployment
```

Then add real watches:

1. Edit `register/watch-register.yaml` — copy the example entry, fill in the document
   URL, the agreements that incorporate it, and the notice/objection rights (clause,
   window, what starts the clock, remedy). Set `status: active`. Set
   `document.selector` to the element that holds the document body (`main`,
   `#terms`, `div.legal-body`): the normalizer then hashes only that element, so
   cookie banners, "related links" and copyright years outside it can't trigger
   false changes. Without a selector the page's `<main>`/`<article>` is used, or
   the whole page if it has neither.
2. `python scripts/validate_register.py` — rejects unknown keys (typos), bad dates,
   bad selectors, and anything the agent can't act on safely.
3. Commit and push, then **re-run `python scripts/setup.py`** — the container egress
   allowlist and the `web_fetch` domain pin are derived from the register's hosts, so
   a new counterparty is unreachable until setup re-syncs them.
4. The next scheduled run picks it up; the first run per watch captures a baseline
   snapshot without alerting.

Test immediately without waiting for the cron:

```bash
python scripts/run_now.py               # fires a manual run and follows it live
```

Re-run `scripts/setup.py` any time you change the YAML under `agent/` — it updates the
existing resources in place (agent updates create a new version; running sessions are
unaffected).

## What an alert looks like

Material and substantive changes produce a GitHub issue (label `amendment-alert`) plus
a committed `alerts/<id>/<run>/analysis.md` containing:

- **What changed** — summary with the decisive before/after excerpts, linked diff.
- **Severity & categories** — e.g. `material` / `subprocessor_change`,
  `data_use_ai_training`.
- **Exposure** — which executed agreements incorporate the document, on which side of
  the table, via which clause, who owns each, and any upcoming renewal (leverage).
- **Clocks** — for each applicable right: what starts the window (`notice` vs
  `posting`), a **conservative deadline** (earliest plausible trigger + window) and a
  latest-plausible deadline, the remedy verbatim, and the caveats (e.g. "confirm
  whether formal notice was received by email — if notice predates detection, the
  clock may already be running").
- **Recommended next steps** — ordered: review diff against the executed agreement,
  pin down the actual trigger date, calendar the conservative deadline, draft the
  objection if warranted, notify the owner.

Administrative/cosmetic changes are snapshotted and noted in the run report without an
issue. Repeated fetch failures raise a watch-health alert — a terms URL that quietly
moves is itself a change-management red flag.

## Clock semantics (read this once)

Detection by this monitor is **not** contractual notice. Windows that run from
*notice* may already be running when a change is detected; windows that run from
*posting* are dated from the best available evidence (the page's own effective date,
the counterparty changelog, else the interval between snapshots). Alerts therefore
always show a conservative deadline and state the assumption behind it. Calendar the
conservative one.

## Operations

```bash
python scripts/run_now.py                       # manual run (works while paused)
python scripts/validate_register.py             # lint the register
pip install -r requirements-dev.txt
python -m unittest discover tests               # pipeline + register-lint regression tests
ruff check scripts tests && ruff format --check scripts tests && mypy
```

CI (`.github/workflows/ci.yml`) runs the register lint, the tests on Python 3.10 and
3.13, ruff and mypy on every push and pull request. Because each scheduled run
mounts the repository's **default branch**, treat a red default branch as an
outage: the next run will use whatever is there. Keep the default branch a stable,
protected branch (currently `claude/main`) rather than a working branch.

### Snapshot comparability

Every snapshot's `.meta.json` records how its text was produced: the normalizer
version, the charset, the selector, the fetch pipeline (`curl` or `web_fetch`) and,
for PDFs, the pinned `pypdf` version. Two hashes are only compared when all of those
match; otherwise the run re-baselines after confirming the text is substantively the
same. So you can change `scripts/normalize.py` (bump `NORMALIZER_VERSION`), add a
selector, or bump `pypdf` (in both `requirements-dev.txt` and the run skill) without
every watch lighting up as amended.

Pause / resume / audit via the `ant` CLI or SDK with the IDs in `config/anthropic.json`:
`client.beta.deployments.pause(id)` / `.unpause(id)`; every firing (success or failure)
is a deployment-run record — `client.beta.deployment_runs.list(deployment_id=...)`.
Each session also has a live Console trace at
`https://platform.claude.com/workspaces/<workspace>/sessions/<session_id>`.

Costs are capped per run by the deployment budget (`agent/deployment.yaml`,
default $15/run); a session that hits the cap pauses rather than overspending.

## Security model

Enforced out of the box: deny-by-default container egress allowlisted to the
register's hostnames plus GitHub; `web_fetch` (Anthropic-side fallback fetcher)
pinned to the same hosts and `web_search` disabled; PAT injected at egress by
Anthropic's proxy and never present in the sandbox; deterministic normalization so
fetched pages can't fake or hide changes through the model; a hard dollar budget per
run; and a tripwire workflow that opens a security issue and fails the push check
if a monitor-attributed commit touches paths outside the agent's write lane
(`snapshots/`, `alerts/`, `runs/`, `watch-state.json`).

One control needs ~10 minutes of GitHub-side setup and is strongly recommended:
issue the PAT from a **dedicated machine account** and add a **push ruleset**
restricting that account from `.claude/**`, `agent/**`, `scripts/**` and the other
human-owned paths — server-side enforcement against a prompt-injected agent
persisting changes into its own methodology. Full walkthrough, plan-availability
notes, and the Slack-alerting recipe: **`docs/hardening.md`**.
