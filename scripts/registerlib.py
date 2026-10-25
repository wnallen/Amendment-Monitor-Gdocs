"""Shared, dependency-light helpers for reading register/watch-register.yaml.

Used by setup.py (host allowlist derivation) and validate_register.py (lint),
and importable from tests without the Anthropic SDK installed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

ROOT = Path(__file__).resolve().parent.parent
REGISTER_PATH = ROOT / "register" / "watch-register.yaml"

# Statuses whose hosts must stay reachable: active is checked every run, paused
# is kept warm so un-pausing never needs a setup re-sync.
ALLOWLISTED_STATUSES = ("active", "paused")
# Managed Agents caps environment allowed_hosts / web_fetch allowed_domains.
MAX_ALLOWLIST_HOSTS = 64


def load_register(path: Path = REGISTER_PATH) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        reg = yaml.safe_load(f)
    return reg if isinstance(reg, dict) else {}


def allowlist_host(url: str | None) -> str | None:
    """Bare lowercase hostname suitable for an allowlist, or None if not allowlistable."""
    if not url:
        return None
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    if not host or "." not in host or host.replace(".", "").isdigit():
        return None  # empty, single-label, or IPv4 — not allowlistable
    if ":" in host:
        return None  # IPv6 literal
    return host


def register_hosts(reg: dict[str, Any]) -> list[str]:
    """Hostnames of every active/paused watch — the only sites the monitor may reach.

    Returned sorted, as bare lowercase hostnames (the format both the
    environment allowed_hosts and the web_fetch allowed_domains expect). The
    two lists differ on subdomains: allowed_hosts matches the exact hostname,
    while a web_fetch allowed_domains entry also covers its subdomains. Every
    registered hostname is therefore listed verbatim (so allowed_hosts always
    covers it), and for `www.` hosts the bare parent is added alongside so the
    tool-side list covers both spellings. Raises ValueError when the
    platform's allowlist cap is exceeded.
    """
    hosts: set[str] = set()
    for w in reg.get("watches") or []:
        if not isinstance(w, dict) or w.get("status") not in ALLOWLISTED_STATUSES:
            continue
        doc = w.get("document") or {}
        if not isinstance(doc, dict):
            continue
        for url in (doc.get("url"), doc.get("change_log_url")):
            host = allowlist_host(url)
            if host is None:
                continue
            hosts.add(host)
            if host.startswith("www."):
                hosts.add(host[4:])
    out = sorted(hosts)
    if len(out) > MAX_ALLOWLIST_HOSTS:
        raise ValueError(
            f"{len(out)} watch hosts exceed the {MAX_ALLOWLIST_HOSTS}-domain allowlist limit — split the register."
        )
    return out
