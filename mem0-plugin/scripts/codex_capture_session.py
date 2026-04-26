#!/usr/bin/env python3
"""Capture a small Codex session-end memory through the Mem0 REST API.

Uses documented Stop hook input fields only. The hook exits successfully even
when Mem0 is unavailable so it never blocks Codex completion.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import urllib.error
import urllib.request

log = logging.getLogger("mem0-codex-capture")
log.setLevel(logging.DEBUG)
handler = logging.StreamHandler(sys.stderr)
handler.setFormatter(logging.Formatter("[mem0-codex-capture] %(message)s"))
log.addHandler(handler)

API_URL = os.environ.get("MEM0_LOCAL_URL", "http://localhost:8888")
MAX_ASSISTANT_TEXT = 10000


def build_content(hook_input: dict) -> str:
    parts = ["## Codex Session State (session-end)\n"]

    cwd = hook_input.get("cwd")
    if cwd:
        parts.append("### Working directory")
        parts.append(f"`{cwd}`\n")

    session_id = hook_input.get("session_id")
    if session_id:
        parts.append("### Session")
        parts.append(f"`{session_id}`\n")

    last_assistant_message = hook_input.get("last_assistant_message")
    if last_assistant_message:
        text = str(last_assistant_message)[:MAX_ASSISTANT_TEXT]
        parts.append("### Last assistant message")
        parts.append(text)
        parts.append("")

    return "\n".join(parts).strip()


def store_memory(api_key: str, content: str, user_id: str) -> None:
    body = {
        "messages": [{"role": "user", "content": content}],
        "user_id": user_id,
        "metadata": {
            "type": "session_state",
            "source": "codex-stop",
        },
    }

    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"{API_URL}/memories",
        data=data,
        headers={
            "Content-Type": "application/json",
            "X-API-Key": api_key,
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status in (200, 201):
                log.info("Codex session state stored successfully")
            else:
                log.warning("API returned status %d", resp.status)
    except urllib.error.URLError as exc:
        log.debug("API call failed: %s", exc)


def main() -> int:
    api_key = os.environ.get("MEM0_API_KEY", "")
    if not api_key:
        return 0

    try:
        hook_input = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return 0

    content = build_content(hook_input)
    if "### Last assistant message" not in content:
        return 0

    user_id = os.environ.get("MEM0_USER_ID", os.environ.get("USER", "default"))
    store_memory(api_key, content, user_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
