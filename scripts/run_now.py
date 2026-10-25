#!/usr/bin/env python3
"""Fire a manual Amendment Monitor run immediately (works even while paused).

Creates a deployment run outside the cron schedule, prints the session ID and
Console trace URL, and follows the session's event stream until it goes idle or
terminates. Ctrl+C detaches without stopping the run.

Usage: python scripts/run_now.py [--no-follow]

Optional: ANTHROPIC_WORKSPACE_ID — workspace slug for the printed Console trace URL.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import anthropic

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "anthropic.json"


def main() -> None:
    if not CONFIG_PATH.exists():
        sys.exit("config/anthropic.json not found — run scripts/setup.py first.")
    ids = json.loads(CONFIG_PATH.read_text())

    client = anthropic.Anthropic()
    run = client.beta.deployments.run(ids["deployment_id"])
    if not run.session_id:
        err = getattr(run, "error", None)
        sys.exit(f"run failed to create a session: {err.type if err else 'unknown'}")

    print(f"session: {run.session_id}")
    # Set ANTHROPIC_WORKSPACE_ID if the API key isn't in the Default workspace.
    workspace = os.environ.get("ANTHROPIC_WORKSPACE_ID", "default")
    print(f"trace:   https://platform.claude.com/workspaces/{workspace}/sessions/{run.session_id}")

    if "--no-follow" in sys.argv[1:]:
        return

    print("--- following events (Ctrl+C to detach; the run continues) ---")
    try:
        with client.beta.sessions.events.stream(session_id=run.session_id) as stream:
            for event in stream:
                if event.type == "agent.message":
                    for block in event.content:
                        if block.type == "text":
                            print(block.text, end="", flush=True)
                elif event.type == "agent.tool_use":
                    print(f"\n[tool: {event.name}]", flush=True)
                elif event.type == "session.error":
                    print(f"\n[error: {event.error.message}]", flush=True)
                elif event.type == "session.status_idle":
                    print("\n--- run finished (idle) ---")
                    break
                elif event.type == "session.status_terminated":
                    print("\n--- session terminated ---")
                    break
    except KeyboardInterrupt:
        print("\ndetached; the run continues server-side.")


if __name__ == "__main__":
    main()
