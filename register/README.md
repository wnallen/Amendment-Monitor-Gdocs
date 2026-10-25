# Watch register — field reference

`watch-register.yaml` is the single human-owned input to the Amendment Monitor. The
agent never edits it; everything the agent learns goes to `watch-state.json`
(machine-owned — don't hand-edit it while a run may be in flight).

`scripts/validate_register.py` enforces everything below. Unknown keys are errors, so
a typo such as `window_day` fails the lint instead of silently leaving a clock
uncomputed. Keys documented here are the only ones the agent reads.

## Top level

| Field | Meaning |
|---|---|
| `version` | Schema version. Currently `1`. |
| `defaults.timezone` | IANA timezone used when presenting deadlines. |
| `watches` | List of watch entries, one per document URL. |

## Watch entry

| Field | Required | Meaning |
|---|---|---|
| `id` | yes | Unique slug. Used as the directory name under `snapshots/` and `alerts/` — don't rename an id with history behind it (retire it and add a new one). |
| `status` | yes | `active` (checked every run), `paused` (kept, not checked), `retired` (kept for history), `example` (never checked). |
| `counterparty` | yes | Legal name of the party that controls the document. |
| `document.title` | yes | Human title; also used to detect the URL serving a different document. |
| `document.type` | yes | `dpa`, `subprocessor_list`, `aup`, `sla`, `security_addendum`, `ai_terms`, `privacy_policy`, `other`. `subprocessor_list` gets entity-level diffing. |
| `document.url` | yes | The publicly fetchable URL that the executed agreement incorporates. |
| `document.format` | no | `html` (default) or `pdf`. |
| `document.selector` | no | Element holding the document body, enforced mechanically by `scripts/normalize.py`: `tag`, `#id`, `.class`, or `tag#id.class` (e.g. `main`, `#terms`, `div.legal-body`). Only that element's text is hashed, so chrome outside it can never register as a change. If the selector stops matching, the run treats it as a fetch failure ("page restructured") rather than silently re-baselining. Omit to fall back to `<main>`/`<article>`, else the whole page. Ignored for `pdf`. |
| `document.selector_hint` | no | Free-text note for the reviewing human/agent about which part of the page matters. Advisory only — it does not affect hashing; use `document.selector` for that. |
| `document.change_log_url` | no | Counterparty's own changelog, used as posting-date evidence. |
| `notes` | no | Anything the reviewer should know. |

## `incorporated_by` — exposure mapping

One entry per **executed agreement** that incorporates the document. This is what lets
an alert say *who is exposed and through which contract*.

| Field | Meaning |
|---|---|
| `agreement` | Name of the executed agreement. |
| `executed` | Execution date (`YYYY-MM-DD`). |
| `our_side` | `customer` or `vendor` — which side of the table the org is on. |
| `incorporation_clause` | The clause doing the incorporating (quote the "as updated from time to time" language if present). |
| `renewal_date` | Next renewal, if known — a nearby renewal is negotiating leverage worth surfacing in alerts. |
| `owner` | Person to name in alerts for this agreement. |

## `rights` — what starts the clock

Named rights blocks; use whichever apply (`amendment_notice`, `subprocessor_objection`,
or add others with the same shape).

| Field | Meaning |
|---|---|
| `required` | Whether the contract obliges the counterparty to notify at all. |
| `method` | How notice arrives: `email`, `portal`, `posting_only`, `none`. Alerts remind you to confirm receipt on this channel when `runs_from: notice`. |
| `clause` | Clause reference for the right. |
| `window_days` | Length of the objection/response window. |
| `runs_from` | `notice` (clock starts at counterparty notice — detection by this monitor is **not** notice) or `posting` (clock starts at publication). |
| `remedy` | What the right actually gives you, verbatim from the contract summary — objection, termination for affected services, etc. |

**Why `runs_from` matters:** the monitor detects changes on its own cadence. When a
window runs from *notice*, the counterparty's email may predate detection and the clock
may already be running — alerts compute a conservative deadline (earliest plausible
trigger + `window_days`) and a latest plausible deadline, and tell you which
assumptions produced each.

## Portability

The register, `watch-state.json`, `snapshots/`, and `alerts/` are plain YAML/JSON/
Markdown in this repository. Clone the repo and you hold the entire monitoring state —
nothing lives only in the agent platform.

## `watch-state.json` — machine state (agent-owned)

One object keyed by watch `id`, plus nothing else at the top level. Fields the run
skill writes:

| Field | Meaning |
|---|---|
| `last_checked` | ISO-8601 UTC time of the last attempt (success or failure). |
| `last_hash` | sha256 of the most recent snapshot's normalized text. |
| `last_snapshot` | `snapshots/<id>/<RUN_TS>.md` path of that snapshot. |
| `consecutive_failures` | Fetch/normalize failures in a row; a watch-health alert is raised at 3. |
| `open_alerts` | List of `{id, from_hash, to_hash, severity, issue_url, conservative_deadline, raised_at}`; `id` is `<watch-id>/<RUN_TS>`. Used for dedup: a `from_hash → to_hash` transition is alerted once. |
| `closed_alerts` | Same shape plus `closed_at`. The run moves an entry here once its GitHub issue is closed, so resolved alerts stop being re-checked. |

Each snapshot also has a `<RUN_TS>.meta.json` beside it recording `url`,
`effective_url`, `fetched_at`, `sha256`, `source_format`, `charset`, `selector`,
`normalizer_version`, `extractor` (pypdf version, PDFs only), `pipeline` (`curl` or
`web_fetch`), `page_last_updated`, and `notes`. Hashes are compared only between
snapshots whose `pipeline`, `normalizer_version`, `selector` and `extractor` agree;
otherwise the run re-baselines after confirming the text is substantively unchanged.
