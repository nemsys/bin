#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""
antigravity-session-extractor
Extracts agent session data from Antigravity's local SQLite storage
and saves it to .agents/log/ in your project.

Usage:
    # Default profile (~/.config/Antigravity):
    uv run ag-extractor.py

    # Named profile (~/Antigravity_Profiles/<n>/app_config):
    uv run ag-extractor.py --profile phoneiep

    # Explicit profile config dir:
    uv run ag-extractor.py --profile-dir ~/Antigravity_Profiles/phoneiep/app_config

    # With task name:
    uv run ag-extractor.py --profile phoneiep --task migrate_to_uv

    # Discovery — print DB structure, no files written:
    uv run ag-extractor.py --profile phoneiep --discover

    # Scan all workspace DBs in the profile:
    uv run ag-extractor.py --profile phoneiep --all-workspaces
"""

import sqlite3
import json
import sys
import argparse
from pathlib import Path
from datetime import datetime, timezone


# ── Profile resolution ───────────────────────────────────────────────────────

PROFILES_ROOT  = Path.home() / "Antigravity_Profiles"
DEFAULT_CONFIG = Path.home() / ".config" / "Antigravity" / "User"
STATE_DB       = "state.vscdb"

# Keys confirmed from discovery output
KNOWN_AGENT_KEYS = {
    "history.entries",
    "antigravity.agentViewContainerId.state",
    "antigravity.agentViewContainerId.numberOfVisibleViews",
    "chat.customModes",
}

# Fallback pattern match
AGENT_KEY_PATTERNS = [
    "history", "agent", "conversation", "session", "chat",
    "task", "artifact", "message", "playground", "mission", "tool",
]


def resolve_config_dir(profile: str | None, profile_dir: Path | None) -> Path:
    if profile_dir:
        return Path(profile_dir).expanduser()
    if profile:
        # app_config is a fake home dir — Antigravity stores its data inside it
        # like a real home: app_config/.config/Antigravity/User/
        fake_home = PROFILES_ROOT / profile / "app_config"
        return fake_home / ".config" / "Antigravity" / "User"
    return DEFAULT_CONFIG


# ── DB discovery ─────────────────────────────────────────────────────────────

def find_workspace_db(config_dir: Path, project_path: Path) -> Path | None:
    ws_storage = config_dir / "workspaceStorage"
    if not ws_storage.exists():
        return None

    project_str = str(project_path.resolve())
    candidates = []

    for db_path in ws_storage.rglob(STATE_DB):
        try:
            con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            cur = con.cursor()
            cur.execute(
                "SELECT value FROM ItemTable "
                "WHERE key LIKE '%folder%' OR key LIKE '%workspace%' LIMIT 20"
            )
            rows = cur.fetchall()
            con.close()
            for (val,) in rows:
                if val and project_str in str(val):
                    return db_path
            candidates.append((db_path.stat().st_mtime, db_path))
        except Exception:
            continue

    if candidates:
        candidates.sort(reverse=True)
        return candidates[0][1]
    return None


def open_db(db_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def is_agent_key(key: str) -> bool:
    if key in KNOWN_AGENT_KEYS:
        return True
    key_lower = (key or "").lower()
    return any(p in key_lower for p in AGENT_KEY_PATTERNS)


# ── Formatters ───────────────────────────────────────────────────────────────

def try_parse_json(val: str) -> object | None:
    try:
        return json.loads(val)
    except Exception:
        return None


def format_history_entries(entries: list) -> str:
    """Render history.entries — the main conversation turns."""
    lines = []
    for entry in entries:
        if not isinstance(entry, dict):
            lines.append(str(entry))
            continue

        role = (
            entry.get("role")
            or entry.get("type")
            or entry.get("sender")
            or entry.get("kind")
            or "?"
        ).upper()

        ts = entry.get("timestamp") or entry.get("created_at") or entry.get("ts") or ""
        lines.append(f"### [{role}]" + (f"  `{ts}`" if ts else ""))

        # Content
        content = (
            entry.get("content")
            or entry.get("text")
            or entry.get("message")
            or entry.get("body")
            or ""
        )
        if isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, dict):
                    part_type = part.get("type", "")
                    if "tool_use" in part_type or part_type == "tool_use":
                        name = part.get("name", "?")
                        inp = part.get("input") or part.get("arguments") or {}
                        parts.append(
                            f"**Tool call:** `{name}`\n"
                            f"```json\n{json.dumps(inp, indent=2, ensure_ascii=False)}\n```"
                        )
                    elif "tool_result" in part_type:
                        result = part.get("content") or part.get("output") or ""
                        if isinstance(result, list):
                            result = "\n".join(r.get("text", str(r)) for r in result)
                        parts.append(f"**Tool result:**\n```\n{str(result)[:3000]}\n```")
                    else:
                        t = part.get("text") or part.get("content") or ""
                        if t:
                            parts.append(t)
                        else:
                            parts.append(json.dumps(part, ensure_ascii=False))
                else:
                    parts.append(str(part))
            content = "\n\n".join(p for p in parts if p)

        lines.append(str(content).strip())
        lines.append("")

        # OpenAI-style tool_calls list
        for tc in entry.get("tool_calls") or entry.get("toolCalls") or []:
            name = tc.get("name") or (tc.get("function") or {}).get("name", "?")
            args = (
                tc.get("arguments")
                or tc.get("input")
                or (tc.get("function") or {}).get("arguments")
                or {}
            )
            if isinstance(args, str):
                args = try_parse_json(args) or args
            lines.append(f"**Tool call:** `{name}`")
            lines.append("```json")
            lines.append(
                json.dumps(args, indent=2, ensure_ascii=False)
                if isinstance(args, dict) else str(args)
            )
            lines.append("```")
            lines.append("")

        # Tool result on same entry
        result = (
            entry.get("tool_result")
            or entry.get("toolResult")
            or entry.get("output")
        )
        if result:
            lines.append("**Result:**\n```")
            lines.append(str(result)[:3000])
            lines.append("```")
            lines.append("")

    return "\n".join(lines)


def render_value(key: str, raw: str) -> str:
    parsed = try_parse_json(raw)
    if parsed is None:
        return raw or "*(empty)*"
    if key == "history.entries" and isinstance(parsed, list):
        return format_history_entries(parsed)
    return json.dumps(parsed, indent=2, ensure_ascii=False)


# ── Discovery ─────────────────────────────────────────────────────────────────

def discover(db_path: Path) -> None:
    print(f"\n{'='*60}")
    print(f"DB: {db_path}")
    print(f"{'='*60}")
    con = open_db(db_path)
    tables = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()]
    print(f"Tables: {tables}\n")
    for table in tables:
        try:
            keys = [r[0] for r in con.execute(f"SELECT key FROM {table}").fetchall()]
        except Exception:
            keys = []
        print(f"  [{table}] — {len(keys)} keys")
        agent_keys = [k for k in keys if is_agent_key(k)]
        other_keys = [k for k in keys if not is_agent_key(k)]
        if agent_keys:
            print("    ★ Agent keys:")
            for k in agent_keys:
                print(f"      • {k}")
        print("    Other keys (sample):")
        for k in other_keys[:6]:
            print(f"      {k}")
        print()
    con.close()


# ── Extraction ────────────────────────────────────────────────────────────────

def extract(project_path: Path, db_path: Path, task_name: str) -> Path:
    log_dir = project_path / ".agents" / "log"
    log_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = log_dir / f"{timestamp}_{task_name}_trace.md"

    con = open_db(db_path)
    tables = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()]

    sections = [
        "# Antigravity Session Trace",
        f"**Extracted:** {datetime.now(timezone.utc).isoformat()}",
        f"**Project:** `{project_path.resolve()}`",
        f"**Source DB:** `{db_path}`",
        f"**Task:** {task_name}",
        "",
    ]

    found = False
    for table in tables:
        try:
            rows = [
                (k, v)
                for k, v in con.execute(f"SELECT key, value FROM {table}").fetchall()
                if is_agent_key(k)
            ]
        except Exception:
            continue
        if not rows:
            continue
        found = True
        sections.append(f"---\n## `{table}`\n")
        for key, val in rows:
            sections.append(f"### `{key}`\n")
            sections.append(render_value(key, val or ""))
            sections.append("")

    con.close()

    if not found:
        sections.append("> No agent-related keys found.")
        sections.append("> Run with `--discover` to inspect the DB structure.")

    out_path.write_text("\n".join(sections), encoding="utf-8")
    return out_path


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract Antigravity agent session data to .agents/log/",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--profile", default=None,
        help="Profile name under ~/Antigravity_Profiles/ (e.g. phoneiep)"
    )
    parser.add_argument(
        "--profile-dir", type=Path, default=None,
        help="Explicit path to profile User dir (overrides --profile)"
    )
    parser.add_argument(
        "--project", type=Path, default=Path.cwd(),
        help="Project root where .agents/log/ will be written (default: cwd)"
    )
    parser.add_argument(
        "--db", type=Path, default=None,
        help="Use this state.vscdb directly (skips auto-discovery)"
    )
    parser.add_argument(
        "--task", default="session",
        help="Task name suffix for the log file (default: session)"
    )
    parser.add_argument(
        "--discover", action="store_true",
        help="Print DB structure and exit — no files written"
    )
    parser.add_argument(
        "--all-workspaces", action="store_true",
        help="Discover ALL workspace DBs in the profile"
    )
    args = parser.parse_args()

    config_dir = resolve_config_dir(args.profile, args.profile_dir)
    ws_storage = config_dir / "workspaceStorage"

    print(f"[*] Config dir : {config_dir}")
    print(f"[*] Project    : {args.project.resolve()}")

    # All-workspaces discovery
    if args.all_workspaces:
        # Collect candidate DB files from multiple locations
        search_roots: list[Path] = []
        if ws_storage.exists():
            search_roots.append(ws_storage)

        # Also check .antigravity dir inside the profile fake-home
        if args.profile:
            fake_home = PROFILES_ROOT / args.profile / "app_config"
            ag_dir = fake_home / ".antigravity"
            if ag_dir.exists():
                search_roots.append(ag_dir)
                print(f"[*] Also scanning  : {ag_dir}")

        all_dbs: set[Path] = set()
        for root in search_roots:
            all_dbs.update(root.rglob(STATE_DB))
            for pat in ("*.db", "*.sqlite", "*.sqlite3"):
                all_dbs.update(root.rglob(pat))

        dbs = sorted(all_dbs, key=lambda p: p.stat().st_mtime, reverse=True)

        if not dbs:
            print(f"[error] No DB files found.")
            print(f"        Searched: {search_roots}")
            # Debug: show what IS in the config dir
            if config_dir.exists():
                print(f"\n[debug] Contents of {config_dir}:")
                for p in sorted(config_dir.rglob("*"))[:40]:
                    print(f"  {p}")
            elif args.profile:
                fake_home = PROFILES_ROOT / args.profile / "app_config"
                print(f"\n[debug] Contents of {fake_home}:")
                for p in sorted(fake_home.rglob("*"))[:40]:
                    print(f"  {p}")
            sys.exit(1)

        print(f"[*] Found {len(dbs)} DB file(s)\n")
        for db in dbs:
            discover(db)
        return

    # Find DB
    db_path = args.db
    if db_path is None:
        db_path = find_workspace_db(config_dir, args.project)
        if db_path is None:
            print(f"[error] No matching workspace DB found under {ws_storage}")
            print(
                f"        Try: uv run ag-extractor.py "
                f"--profile {args.profile or 'phoneiep'} --all-workspaces"
            )
            sys.exit(1)
        print(f"[*] DB         : {db_path}")

    if args.discover:
        discover(db_path)
        return

    out = extract(args.project, db_path, args.task)
    print(f"[✓] Saved: {out}")


if __name__ == "__main__":
    main()