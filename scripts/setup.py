#!/usr/bin/env python3
"""Provision (or re-sync) the Amendment Monitor managed agent.

Control plane, applied declaratively from the YAML in agent/:
  1. Environment  (agent/environment.yaml)
  2. Agent        (agent/amendment-monitor.agent.yaml)  — updates create a new version
  3. Scheduled deployment (agent/deployment.yaml)       — weekly cron, budget, repo mount

Resource IDs are stored in config/anthropic.json; re-running this script updates
the existing resources in place instead of creating duplicates. Commit that file.

Required environment:
  ANTHROPIC_API_KEY   (or an `ant auth login` profile)
  GITHUB_PAT          fine-grained PAT for this repository:
                      Contents: Read and write, Issues: Read and write
Optional:
  AMENDMENT_MONITOR_REPO_URL   override the repo URL (default: origin remote)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import anthropic
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

from registerlib import ROOT, load_register, register_hosts

CONFIG_PATH = ROOT / "config" / "anthropic.json"


def load_yaml(rel: str) -> dict:
    with open(ROOT / rel) as f:
        return yaml.safe_load(f)


def load_ids() -> dict:
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text())
    return {}


def save_ids(ids: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(ids, indent=2) + "\n")


def repo_url() -> str:
    url = os.environ.get("AMENDMENT_MONITOR_REPO_URL")
    if url:
        return url
    url = subprocess.check_output(["git", "-C", str(ROOT), "remote", "get-url", "origin"], text=True).strip()
    # Normalize ssh remotes to https for the github_repository resource.
    if url.startswith("git@github.com:"):
        url = "https://github.com/" + url.removeprefix("git@github.com:")
    return url.removesuffix(".git")


def watch_hosts() -> list[str]:
    """Allowlist hosts derived from the committed register (see registerlib)."""
    try:
        return register_hosts(load_register())
    except ValueError as e:
        sys.exit(str(e))


def sync_environment(client: anthropic.Anthropic, ids: dict) -> str:
    spec = load_yaml("agent/environment.yaml")
    net = spec["config"].get("networking") or {}
    if net.get("type") == "limited":
        base = net.get("allowed_hosts") or []
        net["allowed_hosts"] = sorted(set(base) | set(watch_hosts()))
        spec["config"]["networking"] = net
    if ids.get("environment_id"):
        env = client.beta.environments.update(ids["environment_id"], config=spec["config"])
        print(f"environment updated: {env.id}")
        return env.id
    try:
        env = client.beta.environments.create(name=spec["name"], config=spec["config"])
    except anthropic.APIStatusError as e:
        if e.status_code != 409:
            raise
        # Name already exists (config/anthropic.json was lost) — adopt it.
        env = next(e for e in client.beta.environments.list() if e.name == spec["name"])
        client.beta.environments.update(env.id, config=spec["config"])
        print(f"environment adopted by name: {env.id}")
        return env.id
    print(f"environment created: {env.id}")
    return env.id


def sync_agent(client: anthropic.Anthropic, ids: dict) -> tuple[str, int]:
    spec = load_yaml("agent/amendment-monitor.agent.yaml")
    fields = {k: spec[k] for k in ("name", "description", "model", "system", "tools")}
    # Pin the fallback fetcher (web_fetch runs Anthropic-side, so the
    # environment's egress policy does not cover it) to the register's hosts.
    hosts = watch_hosts()
    for tool in fields["tools"]:
        if tool.get("type") != "agent_toolset_20260401":
            continue
        for cfg in tool.get("configs") or []:
            if cfg.get("name") == "web_fetch":
                if hosts:
                    cfg["allowed_domains"] = hosts
                else:
                    cfg.pop("allowed_domains", None)
                    print(
                        "WARNING: no active/paused watches in the register — "
                        "web_fetch has no allowed_domains pin until you add watches and re-run setup "
                        "(the environment's limited-networking allowed_hosts still blocks it)."
                    )
    if ids.get("agent_id"):
        # Declarative apply: omit `version` so the update applies unconditionally.
        agent = client.beta.agents.update(ids["agent_id"], **fields)
        print(f"agent updated: {agent.id} (version {agent.version})")
    else:
        agent = client.beta.agents.create(**fields)
        print(f"agent created: {agent.id} (version {agent.version})")
    return agent.id, agent.version


def deployment_params(agent_id: str, environment_id: str, pat: str) -> dict:
    spec = load_yaml("agent/deployment.yaml")
    outcome = spec["outcome"]
    return {
        "name": spec["name"],
        "agent": agent_id,  # string form: each firing uses the agent's latest version
        "environment_id": environment_id,
        "schedule": spec["schedule"],
        "budget": {
            "type": "limit",
            "max_list_cost": {"amount": spec["budget_cents"], "currency": "USD"},
        },
        "resources": [
            {
                "type": "github_repository",
                "url": repo_url(),
                "authorization_token": pat,
                # Default branch checkout — the register/skill state as of each run.
            }
        ],
        "initial_events": [
            {
                "type": "user.define_outcome",
                "description": outcome["description"],
                "rubric": {"type": "text", "content": outcome["rubric"]},
                "max_iterations": outcome["max_iterations"],
            }
        ],
    }


def sync_deployment(client: anthropic.Anthropic, ids: dict, params: dict) -> str:
    if ids.get("deployment_id"):
        dep = client.beta.deployments.update(ids["deployment_id"], **params)
        print(f"deployment updated: {dep.id}")
    else:
        dep = client.beta.deployments.create(**params)
        print(f"deployment created: {dep.id}")
    upcoming = getattr(dep.schedule, "upcoming_runs_at", None)
    if upcoming:
        print("next scheduled runs (UTC):", ", ".join(str(t) for t in upcoming[:3]))
    return dep.id


def main() -> None:
    pat = os.environ.get("GITHUB_PAT")
    if not pat:
        sys.exit(
            "GITHUB_PAT is required (fine-grained PAT for this repo: "
            "Contents read/write + Issues read/write). It is stored server-side "
            "on the deployment and never echoed back."
        )

    lint = subprocess.run([sys.executable, str(ROOT / "scripts" / "validate_register.py")], check=False)
    if lint.returncode != 0:
        sys.exit("register has errors — fix them (see above) before provisioning.")

    client = anthropic.Anthropic()
    ids = load_ids()

    ids["environment_id"] = sync_environment(client, ids)
    ids["agent_id"], ids["agent_version"] = sync_agent(client, ids)
    ids["deployment_id"] = sync_deployment(client, ids, deployment_params(ids["agent_id"], ids["environment_id"], pat))
    save_ids(ids)

    print(f"\nIDs written to {CONFIG_PATH.relative_to(ROOT)} — commit this file.")
    print("Test a run now without waiting for the schedule:  python scripts/run_now.py")


if __name__ == "__main__":
    main()
