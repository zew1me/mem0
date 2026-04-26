#!/usr/bin/env python3
"""Codex-compatible Stop hook for Mem0."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def main() -> int:
    raw_input = sys.stdin.read()
    try:
        hook_input = json.loads(raw_input or "{}")
    except json.JSONDecodeError:
        hook_input = {}

    if hook_input.get("stop_hook_active") is True:
        print(json.dumps({"continue": True}))
        return 0

    script_dir = Path(__file__).resolve().parent
    capture_script = script_dir / "codex_capture_session.py"

    try:
        proc = subprocess.Popen(
            [sys.executable, str(capture_script)],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        if proc.stdin is not None:
            proc.stdin.write(raw_input.encode("utf-8"))
            proc.stdin.close()
    except OSError:
        pass

    print(json.dumps({"continue": True}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
