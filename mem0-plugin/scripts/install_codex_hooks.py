#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = [
#   "tomli; python_version < '3.11'",
# ]
# ///
"""Install Mem0 hooks into ~/.codex/config.toml.

The script keeps the edit small and reversible:
  - validates existing TOML before editing
  - enables [features].codex_hooks
  - appends an idempotent, marker-delimited Mem0 hooks block
  - validates the final TOML before writing
  - writes atomically and creates a timestamped backup
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import tempfile
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11 fallback
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:
        tomllib = None  # type: ignore[assignment]


START_MARKER = "# >>> mem0 codex hooks >>>"
END_MARKER = "# <<< mem0 codex hooks <<<"
FEATURES_HEADER_RE = re.compile(r"^\s*\[features\]\s*(?:#.*)?$")
ANY_HEADER_RE = re.compile(r"^\s*\[")
CODEX_HOOKS_RE = re.compile(r"^(\s*codex_hooks\s*=\s*)(.+?)(\s*(?:#.*)?)$")
PLUGIN_ROOT_PLACEHOLDER = "__MEM0_PLUGIN_ROOT__"


def parse_args() -> argparse.Namespace:
    codex_home = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()
    default_config = codex_home / "config.toml"
    default_plugin_root = Path(__file__).resolve().parents[1]

    parser = argparse.ArgumentParser(description="Install Mem0 lifecycle hooks for Codex.")
    parser.add_argument("--config", type=Path, default=default_config, help="Path to Codex config.toml")
    parser.add_argument(
        "--plugin-root",
        type=Path,
        default=default_plugin_root,
        help="Path to the mem0-plugin root. Defaults to the parent of this script directory.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print the updated config without writing it.")
    parser.add_argument("--uninstall", action="store_true", help="Remove the Mem0 hooks block. Leaves codex_hooks enabled.")
    parser.add_argument("--no-backup", action="store_true", help="Do not create a .bak.mem0-* backup before writing.")
    return parser.parse_args()


def require_toml_parser() -> None:
    if tomllib is None:
        raise SystemExit("Python 3.11+ or the 'tomli' package is required to validate TOML safely.")


def validate_toml(content: str, path: Path) -> None:
    require_toml_parser()
    try:
        tomllib.loads(content)
    except Exception as exc:
        raise SystemExit(f"Refusing to edit invalid TOML in {path}: {exc}") from exc


def strip_managed_block(content: str) -> str:
    lines = content.splitlines()
    output: list[str] = []
    in_block = False

    for line in lines:
        if line.strip() == START_MARKER:
            in_block = True
            continue
        if line.strip() == END_MARKER:
            in_block = False
            continue
        if not in_block:
            output.append(line)

    return "\n".join(output).rstrip() + ("\n" if output else "")


def enable_codex_hooks(content: str) -> str:
    lines = content.splitlines()

    for index, line in enumerate(lines):
        if not FEATURES_HEADER_RE.match(line):
            continue

        section_end = len(lines)
        for scan in range(index + 1, len(lines)):
            if ANY_HEADER_RE.match(lines[scan]):
                section_end = scan
                break

        for setting_index in range(index + 1, section_end):
            match = CODEX_HOOKS_RE.match(lines[setting_index])
            if match:
                lines[setting_index] = f"{match.group(1)}true{match.group(3)}"
                return "\n".join(lines).rstrip() + "\n"

        lines.insert(index + 1, "codex_hooks = true")
        return "\n".join(lines).rstrip() + "\n"

    prefix = "[features]\ncodex_hooks = true\n"
    if content.strip():
        return prefix + "\n" + content.rstrip() + "\n"
    return prefix


def build_hooks_block(plugin_root: Path) -> str:
    plugin_root = plugin_root.resolve()
    scripts = plugin_root.resolve() / "scripts"
    session_start = scripts / "codex_session_start.sh"
    block_memory_write = scripts / "codex_block_memory_write.sh"
    user_prompt = scripts / "codex_user_prompt.sh"
    stop_hook = scripts / "codex_stop_hook.py"
    template = plugin_root / "hooks" / "codex-hooks.toml"

    missing = [
        path
        for path in (session_start, block_memory_write, user_prompt, stop_hook, template)
        if not path.exists()
    ]
    if missing:
        missing_list = "\n".join(f"  - {path}" for path in missing)
        raise SystemExit(f"Missing hook script(s):\n{missing_list}")

    content = template.read_text(encoding="utf-8")
    if START_MARKER not in content or END_MARKER not in content:
        raise SystemExit(f"{template} must include the Mem0 managed block markers.")
    return content.replace(PLUGIN_ROOT_PLACEHOLDER, plugin_root.as_posix()).rstrip() + "\n"


def write_atomic(path: Path, content: str, create_backup: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    if create_backup and path.exists():
        timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = path.with_name(f"{path.name}.bak.mem0-{timestamp}")
        backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"Backup written: {backup}")

    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as tmp:
        tmp.write(content)
        tmp_path = Path(tmp.name)

    os.replace(tmp_path, path)


def main() -> int:
    args = parse_args()
    config_path = args.config.expanduser()
    plugin_root = args.plugin_root.expanduser()

    current = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    if current.strip():
        validate_toml(current, config_path)

    updated = strip_managed_block(current)
    if not args.uninstall:
        updated = enable_codex_hooks(updated)
        updated = updated.rstrip() + "\n\n" + build_hooks_block(plugin_root)

    validate_toml(updated, config_path)

    if args.dry_run:
        print(updated, end="")
        return 0

    if updated == current:
        print(f"No changes needed: {config_path}")
        return 0

    write_atomic(config_path, updated, create_backup=not args.no_backup)
    action = "Removed Mem0 hooks from" if args.uninstall else "Installed Mem0 hooks into"
    print(f"{action}: {config_path}")
    if not args.uninstall:
        print("Codex hooks are enabled with [features].codex_hooks = true")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
