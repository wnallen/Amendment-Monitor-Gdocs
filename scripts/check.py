#!/usr/bin/env python3
"""Deterministic phase of an Amendment Monitor run.

Does everything in the run methodology that needs no judgment, for every
active watch, in one process:

  lint the register -> prune alerts whose GitHub issue is closed -> curl each
  document -> normalize via scripts/normalize.py -> compare the hash with the
  previous snapshot (same pipeline/normalizer/selector/extractor only) ->
  write the snapshot + meta -> write a unified diff for changed watches ->
  update register/watch-state.json -> emit a JSON work list.

The agent then spends its budget only on what the work list reports as
`changed`, `pipeline_changed` or `failed`. An unchanged register costs one
tool call instead of several per watch.

Usage:
    python3 scripts/check.py run [--run-ts TS] [--work-dir DIR] [--no-prune] [--only ID ...]
    python3 scripts/check.py ingest ID --raw FILE --run-ts TS [--pipeline web_fetch] [--format text]

`ingest` pushes a document the agent fetched some other way (the web_fetch
fallback) through the identical normalize/compare/snapshot/state path, so no
bookkeeping is ever done by hand. Both commands print the work list as JSON on
stdout and a one-line summary on stderr. Fetch failures are data (recorded in
the state file and the work list), not exit codes; the exit code is non-zero
only when the run itself cannot proceed (unparseable register, missing tools).

Writes only inside the agent's lane: snapshots/, the work directory, and
register/watch-state.json. Diffs go to the work directory; the agent copies
the ones that merit it into alerts/<id>/<RUN_TS>/.

The work list is the agent's trusted roadmap, so it carries no text taken from
a fetched page: register fields, statuses, hashes, paths and counts only. Page-
derived values (the effective URL after redirects, the page's own "last
updated" line, the headings that changed) live in the snapshot meta and the
diff file, which the agent reads as untrusted data in the phases that need them.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

import normalize
from registerlib import ROOT, load_register
from validate_register import ID_RE, STATUSES, LintReport, lint_report

USER_AGENT = "AmendmentMonitor/1.0"
CURL_TIMEOUT_S = 60
CURL_MAX_BYTES = 25 * 1024 * 1024  # a terms document is KB, not tens of MB; beyond this it is a fetch failure
NORMALIZER_TIMEOUT_S = 120
# RUN_TS is a path segment under snapshots/<id>/ — it must be exactly the skill's
# YYYY-MM-DDTHHMMZ so it can never traverse out of the lane and always sorts chronologically.
RUN_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{4}Z$")
# Meta fields that must agree before two hashes are compared (run skill, Phase 1.4).
COMPARABLE_FIELDS = ("pipeline", "normalizer_version", "selector", "extractor")
WATCH_HEALTH_THRESHOLD = 3
ALERT_LABEL = "amendment-alert"
STATE_DEFAULTS: dict[str, Any] = {
    "last_checked": None,
    "last_hash": None,
    "last_snapshot": None,
    "consecutive_failures": 0,
    "open_alerts": [],
    "closed_alerts": [],
}
# A charset is passed to normalize.py as `--charset NAME`: it must look like a codec
# name (no leading dash, no shell-ish characters) — the header is server-controlled.
_CHARSET_RE = re.compile(r"""charset\s*=\s*["']?([A-Za-z0-9][\w.:-]*)""", re.I)
_CONTENT_TYPE_RE = re.compile(r"^content-type:\s*(.+)$", re.I | re.M)
# Issue URL paths the agent may have recorded: the html form (github.com/o/r/issues/7)
# and the API form (api.github.com/repos/o/r/issues/7). Matched against the URL *path*
# only, so a `?query` or `#issuecomment-…` fragment never hides the number.
_ISSUE_PATH_RE = {
    "github.com": re.compile(r"^/([^/]+)/([^/]+)/issues/(\d+)/?$"),
    "api.github.com": re.compile(r"^/repos/([^/]+)/([^/]+)/issues/(\d+)/?$"),
}
# What the work list may say about a redirect target: a hostname, nothing freer.
_HOSTNAME_RE = re.compile(r"^[a-z0-9.-]{1,253}$")
ERROR_MAX_CHARS = 300
# Best-effort evidence of the page's own posting date, for the clock phase.
_DATE_LINE_RE = re.compile(r"\b(effective|last (?:updated|modified|revised)|updated on|revised on|version)\b", re.I)


@dataclass(frozen=True)
class Paths:
    root: Path

    @property
    def register(self) -> Path:
        return self.root / "register" / "watch-register.yaml"

    @property
    def state(self) -> Path:
        return self.root / "register" / "watch-state.json"

    @property
    def snapshots(self) -> Path:
        return self.root / "snapshots"

    @property
    def normalizer(self) -> Path:
        return self.root / "scripts" / "normalize.py"

    @property
    def dev_requirements(self) -> Path:
        return self.root / "requirements-dev.txt"


@dataclass
class Fetched:
    ok: bool
    effective_url: str | None = None
    charset: str | None = None
    error: str | None = None


# --- small helpers -----------------------------------------------------------


def run_ts_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%MZ")


def check_run_ts(run_ts: str) -> str:
    if not RUN_TS_RE.match(run_ts):
        raise SystemExit(f"--run-ts {run_ts!r} must be YYYY-MM-DDTHHMMZ (UTC), e.g. {run_ts_now()}")
    return run_ts


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def pinned_pypdf(paths: Paths) -> str | None:
    """The pypdf version production snapshots must be built with (single source: requirements-dev.txt)."""
    try:
        text = paths.dev_requirements.read_text(encoding="utf-8")
    except OSError:
        return None
    m = re.search(r"^pypdf==(\S+)$", text, re.M)
    return m.group(1) if m else None


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8") or "{}")
    return data if isinstance(data, dict) else {}


def save_state(path: Path, state: dict[str, Any]) -> None:
    """Atomic replace so a crash mid-run never leaves a truncated state file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".watch-state.", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def state_entry(state: dict[str, Any], wid: str) -> dict[str, Any]:
    entry = state.get(wid)
    if not isinstance(entry, dict):
        entry = {}
    for k, v in STATE_DEFAULTS.items():
        entry.setdefault(k, list(v) if isinstance(v, list) else v)
    state[wid] = entry
    return entry


def repo_slug(root: Path) -> str | None:
    """owner/repo from the origin remote, for the issues API."""
    try:
        url = subprocess.check_output(
            ["git", "-C", str(root), "remote", "get-url", "origin"], text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    m = re.search(r"github\.com[/:]([^/]+)/([^/]+?)(?:\.git)?/?$", url)
    return f"{m.group(1)}/{m.group(2)}".lower() if m else None


def clean_error(msg: str) -> str:
    """One printable line, capped: tool stderr can echo server-controlled text (a reason
    phrase, a Location header), and the work list must stay a roadmap, not a page."""
    flat = " ".join(msg.split())
    flat = "".join(ch for ch in flat if ch.isprintable())
    return flat if len(flat) <= ERROR_MAX_CHARS else flat[: ERROR_MAX_CHARS - 1] + "…"


# --- alert pruning (Phase 0.5) ------------------------------------------------


def closed_issues(slug: str) -> dict[int, str]:
    """{issue number: closed_at} for every closed alert issue — one list call per 100 issues
    instead of one GET per open alert. No Authorization header: the platform's proxy injects it."""
    out: dict[int, str] = {}
    for page in range(1, 6):
        url = f"https://api.github.com/repos/{slug}/issues?labels={ALERT_LABEL}&state=closed&per_page=100&page={page}"
        req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=30) as resp:
            items = json.load(resp)
        for it in items:
            if isinstance(it, dict) and "pull_request" not in it:
                out[int(it["number"])] = str(it.get("closed_at"))
        if len(items) < 100:
            break
    return out


def issue_ref(url: object) -> tuple[str, int] | None:
    """(owner/repo lowercased, issue number) for a GitHub issue URL, else None.

    Accepts the html and API forms, ignores query strings and fragments, and
    rejects anything else (a pull request, another host) rather than guessing.
    """
    try:
        parsed = urlparse(str(url or ""))
    except ValueError:
        return None
    pattern = _ISSUE_PATH_RE.get((parsed.hostname or "").lower())
    m = pattern.match(parsed.path) if pattern else None
    if not m:
        return None
    return f"{m.group(1)}/{m.group(2)}".lower(), int(m.group(3))


def prune_alerts(state: dict[str, Any], closed: dict[int, str], slug: str) -> tuple[list[str], list[dict[str, Any]]]:
    """Move open alerts whose issue in *this* repository is closed to closed_alerts.

    Returns (moved alert ids, alerts whose issue_url this run cannot act on).
    Issue numbers are only meaningful within one repository, so an alert whose
    issue lives elsewhere — or whose issue_url is not an issue URL at all —
    is left open and reported, never matched by number alone. Alerts with no
    issue_url are the documented "open until a human removes them" case and are
    not reported.
    """
    moved: list[str] = []
    unprunable: list[dict[str, Any]] = []
    for wid, entry in state.items():
        if not isinstance(entry, dict):
            continue
        still_open: list[dict[str, Any]] = []
        for alert in entry.get("open_alerts") or []:
            ref = issue_ref(alert.get("issue_url")) if alert.get("issue_url") else None
            if ref is not None and ref[0] == slug.lower() and ref[1] in closed:
                closed_alert = {**alert, "closed_at": closed[ref[1]]}
                entry.setdefault("closed_alerts", []).append(closed_alert)
                moved.append(str(closed_alert.get("id") or wid))
                continue
            if alert.get("issue_url") and (ref is None or ref[0] != slug.lower()):
                unprunable.append(
                    {
                        "id": str(alert.get("id") or wid),
                        "issue_url": str(alert["issue_url"]),
                        "reason": "not a GitHub issue URL" if ref is None else f"issue belongs to {ref[0]}, not {slug}",
                    }
                )
            still_open.append(alert)
        entry["open_alerts"] = still_open
    return moved, unprunable


# --- fetch + normalize (Phase 1.1 / 1.3) -----------------------------------


def fetch(url: str, raw_path: Path, hdr_path: Path) -> Fetched:
    """curl the document to raw_path; retry once. Headers land in hdr_path."""
    cmd = [
        "curl", "-fsSL", "--max-time", str(CURL_TIMEOUT_S), "--max-filesize", str(CURL_MAX_BYTES),
        "--proto-redir", "=https", "-A", USER_AGENT,
        "-D", str(hdr_path), "-w", "%{url_effective}", "-o", str(raw_path), url,
    ]  # fmt: skip
    error = "unknown"
    for _attempt in range(2):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=CURL_TIMEOUT_S + 15, check=False)
        except FileNotFoundError:
            return Fetched(False, error="curl is not installed")
        except subprocess.TimeoutExpired:
            error = "curl timed out"
            continue
        if r.returncode == 0 and raw_path.exists():
            try:
                headers = hdr_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                headers = ""
            return Fetched(True, effective_url=r.stdout.strip() or url, charset=charset_from_headers(headers))
        error = (r.stderr.strip().splitlines() or [f"curl exit {r.returncode}"])[-1]
    return Fetched(False, error=error)


def charset_from_headers(dump: str) -> str | None:
    """Charset of the FINAL response in a `curl -D` dump (one header block per hop, blank-line separated).

    An intermediate redirect's Content-Type must not decide how the body is decoded.
    """
    blocks = [b for b in re.split(r"\r?\n\r?\n", dump.strip()) if b.strip()]
    if not blocks:
        return None
    types = _CONTENT_TYPE_RE.findall(blocks[-1])
    if not types:
        return None
    m = _CHARSET_RE.search(types[-1])
    return m.group(1) if m else None


def run_normalizer(
    paths: Paths, raw: Path, fmt: str, selector: str | None, charset: str | None, norm: Path, meta: Path
) -> str | None:
    """Invoke scripts/normalize.py exactly as the skill does; return an error message or None."""
    cmd = [sys.executable, "-X", "utf8", str(paths.normalizer), str(raw), "--format", fmt, "--meta", str(meta)]
    if selector and fmt == "html":
        cmd += ["--select", selector]
    if charset:
        cmd += ["--charset", charset]
    try:
        with open(norm, "w", encoding="utf-8") as out:
            r = subprocess.run(
                cmd, stdout=out, stderr=subprocess.PIPE, text=True, check=False, timeout=NORMALIZER_TIMEOUT_S
            )
    except subprocess.TimeoutExpired:
        norm.unlink(missing_ok=True)
        meta.unlink(missing_ok=True)
        return f"normalize.py exceeded {NORMALIZER_TIMEOUT_S}s on {raw.name} — treat as a fetch failure"
    if r.returncode != 0:
        norm.unlink(missing_ok=True)
        meta.unlink(missing_ok=True)
        return (r.stderr.strip().splitlines() or [f"normalize.py exit {r.returncode}"])[-1]
    return None


def page_last_updated(text: str) -> str | None:
    for line in text.splitlines():
        if _DATE_LINE_RE.search(line) and re.search(r"\d", line):
            return line.strip()[:200]
    return None


# --- diffing (Phase 2.1) ------------------------------------------------------


def changed_headings(old: str, new: str) -> list[str]:
    """Headings (nearest preceding '# ...' line) of every region that differs, in document order."""
    a, b = old.splitlines(), new.splitlines()

    def heading_before(lines: list[str], idx: int) -> str:
        for i in range(min(idx, len(lines) - 1), -1, -1):
            if lines[i].startswith("#"):
                return lines[i].lstrip("#").strip()
        return "(before first heading)"

    seen: list[str] = []
    # Default autojunk: without it the matcher is quadratic on pages full of repeated lines
    # (table rows, list markers); headings are advisory, so the coarser match is fine.
    for tag, i1, _i2, j1, _j2 in difflib.SequenceMatcher(None, a, b).get_opcodes():
        if tag == "equal":
            continue
        for h in (heading_before(b, j1) if b else None, heading_before(a, i1) if a else None):
            if h and h not in seen:
                seen.append(h)
    return seen


def write_diff(path: Path, wid: str, run_ts: str, old_path: str, new_path: str, old: str, new: str) -> list[str]:
    headings = changed_headings(old, new)
    body = "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), old_path, new_path, n=3))
    lines = [
        f"# Diff — {wid} — {run_ts}",
        "",
        f"Previous: `{old_path}`",
        f"Current:  `{new_path}`",
        "",
        "## Changed sections",
        *(f"- {h}" for h in headings),
        "",
        "## Unified diff",
        "",
        "````diff",
        body.rstrip("\n"),
        "````",
        "",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return headings


# --- per-watch pipeline (Phase 1) ---------------------------------------------


def snapshot_metas(paths: Paths, wid: str) -> list[tuple[str, dict[str, Any]]]:
    out: list[tuple[str, dict[str, Any]]] = []
    for p in sorted((paths.snapshots / wid).glob("*.meta.json")):
        try:
            meta = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(meta, dict):
            out.append((p.name[: -len(".meta.json")], meta))
    return out


def previous_meta(paths: Paths, entry: dict[str, Any]) -> dict[str, Any] | None:
    last = entry.get("last_snapshot")
    if not last:
        return None
    meta_path = paths.root / str(last).replace(".md", ".meta.json")
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return meta if isinstance(meta, dict) else None


def already_alerted(entry: dict[str, Any], from_hash: str | None, to_hash: str) -> bool:
    for alert in (entry.get("open_alerts") or []) + (entry.get("closed_alerts") or []):
        if alert.get("from_hash") == from_hash and alert.get("to_hash") == to_hash:
            return True
    return False


def process_watch(
    paths: Paths,
    watch: dict[str, Any],
    state: dict[str, Any],
    run_ts: str,
    work_dir: Path,
    *,
    raw_path: Path | None = None,
    pipeline: str = "curl",
    fmt_override: str | None = None,
    count_failure: bool = True,
) -> dict[str, Any]:
    """Fetch (unless raw_path is given), normalize, compare, snapshot, and update state for one watch.

    `count_failure=False` (the ingest path) reports a failure without bumping
    consecutive_failures: the `run` that preceded the fallback already counted
    this run's failure once, and the skill defines the counter per run.
    """
    wid = str(watch["id"])
    doc = watch.get("document") or {}
    fmt = fmt_override or str(doc.get("format") or "html")
    selector = doc.get("selector") if fmt == "html" else None
    entry = state_entry(state, wid)
    result: dict[str, Any] = {
        "id": wid,
        "counterparty": watch.get("counterparty"),
        "title": doc.get("title"),
        "document_type": doc.get("type"),
        "url": doc.get("url"),
        "pipeline": pipeline,
        "notes": [],
    }
    now = utc_now()
    entry["last_checked"] = now

    def fail(reason: str, *, fetch_failed: bool = False) -> dict[str, Any]:
        if count_failure:
            entry["consecutive_failures"] = int(entry.get("consecutive_failures") or 0) + 1
        result.update(
            status="failed",
            error=clean_error(reason),
            consecutive_failures=entry["consecutive_failures"],
            watch_health_alert_due=entry["consecutive_failures"] >= WATCH_HEALTH_THRESHOLD,
            # web_fetch is only the remedy for curl itself failing; a selector miss, an empty
            # extraction or a pypdf pin mismatch must not be "fixed" by re-fetching as plain text.
            fallback="web_fetch" if fetch_failed and pipeline == "curl" else None,
        )
        return result

    if fmt == "pdf":
        pin, have = pinned_pypdf(paths), normalize.pdf_version()
        if pin and have != pin:
            return fail(f"pypdf {have or 'missing'} installed but {pin} is pinned — run: pip install pypdf=={pin}")

    effective_url = str(doc.get("url"))
    charset: str | None = None
    if raw_path is None:
        raw_path = work_dir / f"raw-{wid}"
        fetched = fetch(effective_url, raw_path, work_dir / f"hdr-{wid}")
        if not fetched.ok:
            return fail(f"fetch failed: {fetched.error}", fetch_failed=True)
        effective_url, charset = fetched.effective_url or effective_url, fetched.charset
        reg_host = normalize_host(str(doc.get("url")))
        if normalize_host(effective_url) != reg_host:
            # The effective URL is server-controlled (any redirect target, query string
            # included); the work list gets a hostname at most. The full URL is in the meta.
            result["redirected_to_host"] = safe_host(effective_url)
            result["notes"].append(
                f"URL redirected to non-registered host {result['redirected_to_host']} — add it to the register "
                "so the allowlist covers it (full effective_url in the snapshot meta)"
            )

    norm_path, meta_path = work_dir / f"norm-{wid}.md", work_dir / f"meta-{wid}.json"
    err = run_normalizer(paths, raw_path, fmt, selector, charset, norm_path, meta_path)
    if err:
        return fail(err)
    text = norm_path.read_text(encoding="utf-8")
    sidecar = json.loads(meta_path.read_text(encoding="utf-8"))
    new_hash = str(sidecar["sha256"])
    entry["consecutive_failures"] = 0

    title = str(doc.get("title") or "")
    if title and title.lower() not in text.lower():
        result["notes"].append("document.title not found in fetched text — verify it is still the same document")

    dated = page_last_updated(text)
    meta = {
        "url": doc.get("url"),
        "effective_url": effective_url,
        "fetched_at": now,
        "sha256": new_hash,
        "source_format": sidecar.get("source_format"),
        "charset": sidecar.get("charset"),
        "selector": sidecar.get("selector"),
        "normalizer_version": sidecar.get("normalizer_version"),
        "extractor": sidecar.get("extractor"),
        "pipeline": pipeline,
        "page_last_updated": dated,
        "notes": "page_last_updated is a heuristic line match" if dated else None,
    }
    result["sha256"] = new_hash  # page_last_updated stays in the meta: it is page text

    prev_hash = entry.get("last_hash")
    prev = previous_meta(paths, entry)
    if prev is not None:
        prev = {**prev, "pipeline": prev.get("pipeline") or "curl"}  # absent in an old meta means curl
    comparable = prev is None or all(prev.get(k) == meta.get(k) for k in COMPARABLE_FIELDS)
    if prev is not None and not comparable:
        result["pipeline_differs"] = {
            k: [prev.get(k), meta.get(k)] for k in COMPARABLE_FIELDS if prev.get(k) != meta.get(k)
        }

    if prev_hash == new_hash:
        result["status"] = "unchanged"
        return result

    # Baseline or change: write the (append-only) snapshot pair.
    snap_dir = paths.snapshots / wid
    snap_md, snap_meta = snap_dir / f"{run_ts}.md", snap_dir / f"{run_ts}.meta.json"
    if snap_md.exists() or snap_meta.exists():
        # A reused RUN_TS is an operator error, not evidence about the document: no
        # failure count, no fallback hint, and the state keeps pointing at the old snapshot.
        result.update(
            status="conflict",
            error=f"snapshot {snap_md.relative_to(paths.root)} already exists — snapshots are append-only; re-run with a fresh --run-ts",
        )
        return result
    snap_dir.mkdir(parents=True, exist_ok=True)
    snap_md.write_text(text, encoding="utf-8")
    snap_meta.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    rel_snapshot = str(snap_md.relative_to(paths.root))

    if prev_hash is None:
        result["status"] = "baseline"
    else:
        result["status"] = "pipeline_changed" if not comparable else "changed"
        result["from_hash"], result["to_hash"] = prev_hash, new_hash
        result["previous_snapshot"] = entry.get("last_snapshot")
        result["already_alerted"] = already_alerted(entry, prev_hash, new_hash)
        for ts, old_meta in snapshot_metas(paths, wid):
            if ts != run_ts and old_meta.get("sha256") == new_hash:
                result["reverted_to"] = ts
                result["notes"].append(
                    f"content reverted to snapshot {ts} — site may serve variants; recommend setting document.selector"
                )
                break
        old_text = ""
        if entry.get("last_snapshot"):
            try:
                old_text = (paths.root / str(entry["last_snapshot"])).read_text(encoding="utf-8")
            except OSError:
                result["notes"].append("previous snapshot file missing — diff is against empty text")
        diff_path = work_dir / f"diff-{wid}.md"
        # The changed headings are page text, so they go in the diff file, not the work list.
        write_diff(diff_path, wid, run_ts, str(entry.get("last_snapshot") or "(none)"), rel_snapshot, old_text, text)
        result["diff_path"] = str(diff_path)

    result["snapshot"], result["meta"] = rel_snapshot, str(snap_meta.relative_to(paths.root))
    entry["last_hash"], entry["last_snapshot"] = new_hash, rel_snapshot
    return result


def normalize_host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().rstrip(".")


def safe_host(url: str) -> str:
    """The hostname of `url` if it is a plain hostname, else a placeholder — for the work list."""
    host = normalize_host(url)
    return host if _HOSTNAME_RE.match(host) else "(unprintable host)"


# --- register triage (Phase 0) ------------------------------------------------


def triage(
    reg: dict[str, Any], report: LintReport
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Split the register into (active watches to run, invalid watches, skipped watches).

    Lint findings are attributed by watch *index*, never by matching an id against
    the message text, so a watch called `defaults` or `watches` cannot absorb a
    register-level error and let the run proceed on a broken register.
    """
    register_level = report.register_errors()
    if register_level:
        raise SystemExit(
            "register has errors the run cannot work around:\n  " + "\n  ".join(str(e) for e in register_level)
        )
    watches = reg.get("watches") or []
    if not isinstance(watches, list):  # lint reports this as register-level; belt and braces
        raise SystemExit("register: watches must be a list")
    active: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for i, w in enumerate(watches):
        wid = w.get("id") if isinstance(w, dict) else None
        status = w.get("status") if isinstance(w, dict) else None
        # The id is only used as a label once lint has vouched for it; otherwise the index.
        label = wid if isinstance(wid, str) and ID_RE.match(wid) else f"watches[{i}]"
        errors = [str(e) for e in report.watch_errors(i)]
        if errors:
            if status in STATUSES and status != "active":
                skipped.append({"id": label, "index": i, "status": status, "errors": errors})
            else:  # active, or a status so broken it might have meant active
                invalid.append({"id": label, "index": i, "status": "invalid", "errors": errors})
        elif status == "active":
            active.append(w)
        else:
            skipped.append({"id": label, "index": i, "status": status})
    return active, invalid, skipped


def load_and_triage(paths: Paths) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Lint, then load, then triage — aborting on anything no single watch owns."""
    report = lint_report(paths.register)
    # An unparseable or structurally broken register is never loaded: triage aborts on it.
    reg = {} if report.register_errors() else load_register(paths.register)
    return triage(reg, report)


def summarize(
    results: list[dict[str, Any]], invalid: list[dict[str, Any]], skipped: list[dict[str, Any]]
) -> dict[str, int]:
    counts = {
        "checked": len(results),
        "unchanged": 0,
        "baseline": 0,
        "changed": 0,
        "pipeline_changed": 0,
        "failed": 0,
        "conflict": 0,
    }
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    counts["invalid"], counts["skipped"] = len(invalid), len(skipped)
    return counts


# --- commands -----------------------------------------------------------------


def cmd_run(paths: Paths, args: argparse.Namespace) -> dict[str, Any]:
    active, invalid, skipped = load_and_triage(paths)
    if args.only:
        wanted = set(args.only)
        unmatched = sorted(wanted - {w["id"] for w in active})
        if unmatched:
            # A typo must not look like a clean "0 checked" run.
            raise SystemExit(f"--only: not active watches in the register: {', '.join(unmatched)}")
        active = [w for w in active if w["id"] in wanted]

    run_ts = check_run_ts(args.run_ts) if args.run_ts else run_ts_now()
    work_dir = Path(args.work_dir) if args.work_dir else Path(tempfile.gettempdir()) / "amendment-monitor" / run_ts
    work_dir.mkdir(parents=True, exist_ok=True)
    state = load_state(paths.state)

    out: dict[str, Any] = {"run_ts": run_ts, "work_dir": str(work_dir), "repo": repo_slug(paths.root)}
    out["pruned_alerts"], out["unprunable_alerts"], out["prune_error"] = [], [], None
    if not args.no_prune and out["repo"]:
        try:
            out["pruned_alerts"], out["unprunable_alerts"] = prune_alerts(
                state, closed_issues(out["repo"]), out["repo"]
            )
        except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
            out["prune_error"] = clean_error(f"could not list closed issues: {e}")
    elif not args.no_prune:
        out["prune_error"] = "origin remote is not a github.com URL — alerts not pruned"

    results = []
    for w in active:
        results.append(process_watch(paths, w, state, run_ts, work_dir))
        save_state(paths.state, state)  # persist after every watch so a crash loses nothing
    save_state(paths.state, state)
    out.update(watches=results, invalid=invalid, skipped=skipped, summary=summarize(results, invalid, skipped))
    out["worklist_path"] = str(work_dir / "worklist.json")
    Path(out["worklist_path"]).write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    return out


def cmd_ingest(paths: Paths, args: argparse.Namespace) -> dict[str, Any]:
    run_ts = check_run_ts(args.run_ts)
    active, invalid, _skipped = load_and_triage(paths)
    watch = next((w for w in active if w["id"] == args.id), None)
    if watch is None:
        bad = next((v for v in invalid if v["id"] == args.id), None)
        raise SystemExit(f"{args.id}: {'invalid register entry' if bad else 'not an active watch'}")
    raw = Path(args.raw)
    if not raw.is_file():
        raise SystemExit(f"{raw}: not a file")
    work_dir = Path(args.work_dir) if args.work_dir else Path(tempfile.gettempdir()) / "amendment-monitor" / run_ts
    work_dir.mkdir(parents=True, exist_ok=True)
    state = load_state(paths.state)
    fmt = args.format or ("text" if args.pipeline == "web_fetch" else None)
    result = process_watch(
        paths,
        watch,
        state,
        run_ts,
        work_dir,
        raw_path=raw,
        pipeline=args.pipeline,
        fmt_override=fmt,
        count_failure=False,
    )
    save_state(paths.state, state)
    out = {
        "run_ts": run_ts,
        "work_dir": str(work_dir),
        "watches": [result],
        "summary": summarize([result], [], []),
    }
    (work_dir / f"ingest-{args.id}.json").write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=str(ROOT), help=argparse.SUPPRESS)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="check every active watch")
    r.add_argument("--run-ts", help="RUN_TS to stamp this run with (default: now, YYYY-MM-DDTHHMMZ)")
    r.add_argument("--work-dir", help="scratch directory for raw/normalized/diff files")
    r.add_argument("--no-prune", action="store_true", help="skip the closed-issue alert prune")
    r.add_argument("--only", nargs="+", metavar="ID", help="restrict to these active watch ids (each must exist)")
    g = sub.add_parser("ingest", help="process one watch from a file fetched by other means (web_fetch fallback)")
    g.add_argument("id")
    g.add_argument("--raw", required=True, help="file holding the fetched content")
    g.add_argument("--run-ts", required=True, help="the RUN_TS of the run this belongs to")
    g.add_argument("--pipeline", default="web_fetch", choices=["web_fetch", "curl"])
    g.add_argument("--format", choices=["html", "pdf", "text"], help="default: text for web_fetch, else the register's")
    g.add_argument("--work-dir")
    args = ap.parse_args(argv)

    paths = Paths(Path(args.root).resolve())
    out = cmd_run(paths, args) if args.cmd == "run" else cmd_ingest(paths, args)
    s = out["summary"]
    print(
        f"run {out['run_ts']}: {s['checked']} checked, {s['unchanged']} unchanged, {s['baseline']} baseline, "
        f"{s['changed']} changed, {s['pipeline_changed']} pipeline-changed, {s['failed']} failed, {s['conflict']} conflict, "
        f"{s.get('invalid', 0)} invalid, {s.get('skipped', 0)} skipped",
        file=sys.stderr,
    )
    json.dump(out, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
