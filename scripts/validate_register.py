#!/usr/bin/env python3
"""Lint register/watch-register.yaml before committing changes.

Purely local — no API calls. Exits non-zero on errors; warnings don't fail.

Usage: python scripts/validate_register.py [PATH]
"""

from __future__ import annotations

import dataclasses
import re
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

from registerlib import REGISTER_PATH, allowlist_host, load_register, register_hosts

SCHEMA_VERSION = 1
STATUSES = {"active", "paused", "retired", "example"}
DOC_TYPES = {
    "dpa",
    "subprocessor_list",
    "aup",
    "sla",
    "security_addendum",
    "ai_terms",
    "privacy_policy",
    "other",
}
FORMATS = {"html", "pdf"}
RUNS_FROM = {"notice", "posting"}
METHODS = {"email", "portal", "posting_only", "none"}
SIDES = {"customer", "vendor"}

# Every key the agent and the docs know about. Anything else is almost
# certainly a typo (`window_day`, `renewal_dat`) that would otherwise be
# silently ignored and leave a clock or an owner missing from alerts.
TOP_KEYS = {"version", "defaults", "watches"}
DEFAULTS_KEYS = {"timezone"}
WATCH_KEYS = {"id", "status", "counterparty", "document", "incorporated_by", "rights", "notes"}
DOC_KEYS = {"title", "type", "url", "format", "selector", "selector_hint", "change_log_url"}
INC_KEYS = {"agreement", "executed", "our_side", "incorporation_clause", "renewal_date", "owner"}
RIGHT_KEYS = {"required", "method", "clause", "window_days", "runs_from", "remedy"}

# ids become directory names under snapshots/ and alerts/ — slugs only, so an id
# can never traverse out of the agent's write lane.
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
# URLs are interpolated, double-quoted, into the run's curl command — reject
# whitespace and every character still live inside double quotes.
URL_UNSAFE = re.compile(r"""[\s"'`$\\]""")
# document.selector is passed to normalize.py --select; same grammar as there.
SELECTOR_RE = re.compile(r"^([a-zA-Z][\w-]*)?(#[\w-]+)?((?:\.[\w-]+)*)$")


@dataclass(frozen=True)
class Issue:
    """One lint finding.

    `watch` is the index into `watches` the finding belongs to, or None for a
    register-level finding (top level, `defaults`, the `watches` list itself, the
    host-cap check). Attribution is structural, carried alongside the text rather
    than parsed back out of it, so a watch whose id happens to be `defaults` or
    `watches` can never claim a register-level error as its own.
    """

    message: str
    watch: int | None = None

    def __str__(self) -> str:
        return self.message


@dataclass
class LintReport:
    errors: list[Issue] = dataclasses.field(default_factory=list)
    warnings: list[Issue] = dataclasses.field(default_factory=list)
    watch_count: int = 0
    active_count: int = 0

    def register_errors(self) -> list[Issue]:
        """Errors no single watch can be blamed for — a run cannot work around these."""
        return [e for e in self.errors if e.watch is None]

    def watch_errors(self, index: int) -> list[Issue]:
        return [e for e in self.errors if e.watch == index]


def check_url(url: str) -> bool:
    parsed = urlparse(url)
    return (
        parsed.scheme == "https"
        and bool(parsed.netloc)
        and allowlist_host(url) is not None
        and not URL_UNSAFE.search(url)
    )


def check_date(value: object) -> bool:
    if isinstance(value, date):
        return True  # YAML already parsed an unquoted YYYY-MM-DD
    if isinstance(value, str):
        try:
            date.fromisoformat(value)
            return True
        except ValueError:
            return False
    return False


def lint_report(path: Path = REGISTER_PATH) -> LintReport:
    """Lint the register at `path`; every finding carries the watch index it belongs to."""
    report = LintReport()

    try:
        reg = load_register(path)
    except (yaml.YAMLError, ValueError, OSError) as e:
        # PyYAML constructs unquoted dates eagerly, so `executed: 2024-13-40`
        # raises here rather than reaching the field checks below.
        report.errors.append(Issue(f"cannot parse {path.name}: {str(e).splitlines()[0]}"))
        return report

    # Every finding below goes through these two, with the watch index it is about
    # (None = register-level). The label inside the message is for humans only.
    def error(msg: str, watch: int | None = None) -> None:
        report.errors.append(Issue(msg, watch))

    def warn(msg: str, watch: int | None = None) -> None:
        report.warnings.append(Issue(msg, watch))

    def unknown(keys: set[str], mapping: dict, label: str, watch: int | None = None) -> None:
        extra = sorted(set(mapping) - keys)
        if extra:
            error(f"{label}: unknown key(s) {extra} — typo? see register/README.md", watch)

    unknown(TOP_KEYS, reg, "top level")
    if reg.get("version") != SCHEMA_VERSION:
        error(f"top level: version must be {SCHEMA_VERSION}")
    defaults = reg.get("defaults") or {}
    if not isinstance(defaults, dict):
        error("defaults: must be a mapping")
        defaults = {}
    unknown(DEFAULTS_KEYS, defaults, "defaults")
    tz = defaults.get("timezone")
    if tz is None:
        warn("defaults.timezone not set — deadlines will be presented in UTC")
    else:
        try:
            ZoneInfo(str(tz))
        except (ZoneInfoNotFoundError, ValueError):
            error(f"defaults.timezone: {tz!r} is not an IANA timezone")

    watches = reg.get("watches") or []
    if not isinstance(watches, list):
        error("watches: must be a list")
        watches = []
    seen_ids: dict[str, int] = {}

    for i, w in enumerate(watches):
        if not isinstance(w, dict):
            error(f"watches[{i}]: must be a mapping", i)
            continue
        label = str(w.get("id") or f"watches[{i}]")
        unknown(WATCH_KEYS, w, label, i)

        wid = w.get("id")
        if not wid:
            error(f"{label}: missing id", i)
        elif not isinstance(wid, str) or not ID_RE.match(wid):
            error(f"{label}: id must be a lowercase slug (a-z, 0-9, hyphens)", i)
        elif wid in seen_ids:
            # Both entries would write to snapshots/<id>/, so neither may run: the
            # earlier one is flagged too, not just the one that came second.
            first = seen_ids[wid]
            error(f"{label}: duplicate id (also watches[{first}])", i)
            error(f"{label}: duplicate id (also watches[{i}])", first)
        else:
            seen_ids[wid] = i

        if w.get("status") not in STATUSES:
            error(f"{label}: status must be one of {sorted(STATUSES)}", i)
        if not w.get("counterparty"):
            error(f"{label}: counterparty required", i)

        doc = w.get("document") or {}
        if not isinstance(doc, dict):
            error(f"{label}: document must be a mapping", i)
            doc = {}
        unknown(DOC_KEYS, doc, f"{label}.document", i)
        if not doc.get("title"):
            error(f"{label}: document.title required", i)
        if doc.get("type") not in DOC_TYPES:
            error(f"{label}: document.type must be one of {sorted(DOC_TYPES)}", i)
        if not check_url(str(doc.get("url") or "")):
            error(
                f"{label}: document.url must be an https URL to a hostname, with no spaces, quotes or shell metacharacters",
                i,
            )
        if doc.get("change_log_url") and not check_url(str(doc["change_log_url"])):
            error(
                f"{label}: document.change_log_url must be an https URL to a hostname, with no spaces, quotes or shell metacharacters",
                i,
            )
        if "format" in doc and doc["format"] not in FORMATS:
            error(f"{label}: document.format must be one of {sorted(FORMATS)}", i)
        sel = doc.get("selector")
        if sel is not None:
            if not isinstance(sel, str) or not sel.strip() or not SELECTOR_RE.match(sel.strip()):
                error(f"{label}: document.selector must be a simple selector (tag, #id, .class, tag#id.class)", i)
            elif doc.get("format") == "pdf":
                warn(f"{label}: document.selector is ignored for pdf documents", i)
        elif doc.get("selector_hint") and doc.get("format", "html") == "html" and w.get("status") == "active":
            warn(f"{label}: selector_hint is advisory only — set document.selector so the normalizer can enforce it", i)

        if not w.get("incorporated_by"):
            warn(f"{label}: no incorporated_by entries — alerts can't map exposure", i)
        incs = w.get("incorporated_by") or []
        if not isinstance(incs, list):
            error(f"{label}: incorporated_by must be a list", i)
            incs = []
        for j, inc in enumerate(incs):
            ilabel = f"{label}.incorporated_by[{j}]"
            if not isinstance(inc, dict):
                error(f"{ilabel}: must be a mapping", i)
                continue
            unknown(INC_KEYS, inc, ilabel, i)
            if inc.get("our_side") not in SIDES:
                error(f"{ilabel}: our_side must be customer|vendor", i)
            if not inc.get("agreement"):
                error(f"{ilabel}: agreement required", i)
            if not inc.get("owner"):
                warn(f"{ilabel}: no owner — alerts have no one to name", i)
            for field in ("executed", "renewal_date"):
                if inc.get(field) is not None and not check_date(inc[field]):
                    error(f"{ilabel}: {field} must be a YYYY-MM-DD date", i)

        rights = w.get("rights") or {}
        if not isinstance(rights, dict):
            error(f"{label}: rights must be a mapping of named rights", i)
            rights = {}
        if not rights and w.get("status") == "active":
            warn(f"{label}: no rights recorded — clocks can't be computed", i)
        for rname, r in rights.items():
            rlabel = f"{label}.rights.{rname}"
            if not isinstance(r, dict):
                error(f"{rlabel}: must be a mapping", i)
                continue
            unknown(RIGHT_KEYS, r, rlabel, i)
            wd = r.get("window_days")
            if not isinstance(wd, int) or isinstance(wd, bool) or wd <= 0:
                error(f"{rlabel}: window_days must be a positive integer", i)
            if r.get("runs_from") not in RUNS_FROM:
                error(f"{rlabel}: runs_from must be notice|posting", i)
            if r.get("method") not in METHODS:
                error(f"{rlabel}: method must be one of {sorted(METHODS)}", i)
            if "required" in r and not isinstance(r["required"], bool):
                error(f"{rlabel}: required must be true or false", i)
            if not r.get("remedy"):
                warn(f"{rlabel}: no remedy recorded", i)
            if not r.get("clause"):
                warn(f"{rlabel}: no clause reference", i)

    try:
        register_hosts(reg)
    except ValueError as e:
        error(str(e))

    report.watch_count = len(watches)
    report.active_count = sum(1 for w in watches if isinstance(w, dict) and w.get("status") == "active")
    return report


def lint(path: Path = REGISTER_PATH) -> tuple[list[str], list[str], int, int]:
    """Return (errors, warnings, watch_count, active_count) as plain strings — the CLI view."""
    r = lint_report(path)
    return [str(e) for e in r.errors], [str(w) for w in r.warnings], r.watch_count, r.active_count


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    path = Path(args[0]) if args else REGISTER_PATH
    errors, warnings, total, active = lint(path)
    for msg in warnings:
        print(f"WARN  {msg}")
    for msg in errors:
        print(f"ERROR {msg}")
    print(f"\n{total} watches ({active} active), {len(errors)} errors, {len(warnings)} warnings")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
