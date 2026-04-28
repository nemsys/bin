#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["websockets>=12.0", "aiohttp>=3.9"]
# ///
"""
antigravity-logger v2
Captures Antigravity agent conversations via Chrome DevTools Protocol.
Saves clean, structured traces to .agents/log/ — zero extra AI tokens.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SETUP (one-time)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

1. Add bash alias:

alias ag-phoneiep-log='HOME="/home/nemsys/Antigravity_Profiles/phoneiep/app_config" \
     antigravity \
     --user-data-dir="/home/nemsys/Antigravity_Profiles/phoneiep/browser_profile" \
     --remote-debugging-port=9222'

2. Add next function to .bashrc and reload:
   
    # tmux helper — opens split pane with Antigravity + logger side by side
    ag-log() {
        local profile="${1:?usage: ag-log <profile> <project_dir> <task>}"
        local project="${2:?usage: ag-log <profile> <project_dir> <task>}"
        local task="${3:-session}"
        local session="ag-${profile}-${task}"

        # Check if session already exists to avoid errors
        if tmux has-session -t "$session" 2>/dev/null; then
            tmux attach-session -t "$session"
            return
        fi

        # Create session and first pane
        tmux new-session -d -s "$session" -x 220 -y 50
        tmux send-keys -t "$session:0.0" "ag-${profile}-log $project" Enter

        # Split and run the logger in the second pane
        tmux split-window -h -t "$session:0.0"
        tmux send-keys -t "$session:0.1" "cd $project && uv run --script ag-logger.py --task $task" Enter

        # Attach
        tmux attach-session -t "$session"
    }


2. Copy to ~/.local/bin/:   cp ag-logger.py ~/.local/bin/

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
USAGE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  ag-log phoneiep /data/projects/KBWeaver my_task

Options:
  --stabilize N   Require N consecutive identical polls before committing a
                  turn (default: 2 = ~6 s). Prevents logging typing-in-progress
                  and streaming-in-progress states.
  --debug         Append DOM class info to each logged turn (helps tune selectors).

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CALIBRATION
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

If the log is still noisy or missing content, run with --debug.
The _cls field shows which DOM classes each captured element has.
Update NOISE_PATTERNS or EXTRACT_JS based on what you see.
"""

import asyncio
import aiohttp
import hashlib
import websockets
import json
import re
import sys
import argparse
import signal
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime, timezone

CDP_HOST = "localhost"
CDP_PORT = 9222
POLL_INTERVAL = 3.0

ROLE_ICONS = {
    "user":      "👤",
    "thought":   "🧠",
    "tool_call": "🔧",
    "agent":     "🤖",
    "unknown":   "❓",
}

# ─────────────────────────────────────────────────────────────────────────────
# JS injected into Antigravity's renderer via CDP Runtime.evaluate
# ─────────────────────────────────────────────────────────────────────────────
EXTRACT_JS = r"""
(function () {
  "use strict";

  // ── Noise: UI chrome, sidebar history items, partial UI labels ──────────
  const NOISE_EXACT = new Set([
    "Plan", "Send", "mic", "Review Changes", "Accept all", "Reject all",
    "See all", "Attach", "alternate_email", "content_copy", "python", "text",
    "Drag a view here to display.",
  ]);

  const NOISE_RE = [
    /^Ask anything[,.].*@ to mention/is,       // input area placeholder
    /^AI may make mistakes\./i,
    /^Gemini \d/i,                              // model selector label
    /^\d+ Files? With Changes\s*$/,             // diff summary
    /^[+-]\d+\s*$/,                             // diff line count  "+2 -0"
    /^Press desired key/i,                      // keybinding prompt
    /^[A-Z][a-z ]+\n\d+[hm]\s*$/,             // sidebar history: "Title\n1h"
  ];

  function isNoise(text) {
    if (!text || text.length < 4) return true;
    if (NOISE_EXACT.has(text)) return true;
    return NOISE_RE.some((re) => re.test(text));
  }

  // ── Sidebar / nav detection ─────────────────────────────────────────────
  // Walk ancestors: if any has a sidebar-like class, skip the element.
  function isInSidebar(el) {
    let node = el.parentElement;
    while (node && node !== document.body) {
      const cls = (node.className || "").toLowerCase();
      if (/\b(sidebar|activitybar|history|recents|panel-header|nav\b)/.test(cls))
        return true;
      node = node.parentElement;
    }
    return false;
  }

  // ── Role classification ─────────────────────────────────────────────────
  function classifyRole(el, text) {
    // 1. Walk ancestors for explicit data-role or role-bearing class names.
    let node = el.parentElement;
    while (node && node !== document.body) {
      const cls = (node.className || "").toLowerCase();
      const dr  = (node.getAttribute("data-role") || "").toLowerCase();
      if (/\buser-?message\b|\bhuman-?turn\b/.test(cls) || dr === "user")
        return "user";
      if (/\bassistant-?message\b|\bagent-?turn\b|\bbot-?message\b/.test(cls) || dr === "assistant")
        return "agent";
      node = node.parentElement;
    }

    // 2. Tool-call: short imperative line starting with a verb.
    if (
      /^(Ran |Edited \d|Created |Deleted |Read |Wrote |Searched |Moved |Copied |Applied )/
        .test(text) && text.length < 300
    ) return "tool_call";

    // 3. Agent thought: introspective block (no code, starts with Verb-ing / Verb-ed).
    if (
      /^[A-Z][a-zA-Z]+(ing|ed)\b/.test(text) &&
      /\b(I'm|I am|I've|I need|I will|Now |Let me|First |Next |Final|The goal|My plan|Focusing|Prioritiz|Analyz|Refin)\b/.test(text) &&
      !el.querySelector("code, pre")
    ) return "thought";

    // 4. Agent response: starts with first-person or has code block.
    if (el.querySelector("code, pre")) return "agent";
    if (/^(I've |I have |I'll |Here is |Here's |Below |The following |This script |This code |Let me |I created |I wrote )/.test(text))
      return "agent";

    return "unknown";
  }

  // ── Main extraction ─────────────────────────────────────────────────────
  // Try Antigravity-specific selectors, fall back to generic ones.
  const SELECTORS = [
    ".select-text",
    '[class*="message-body"]',
    '[class*="chat-turn"]',
    '[class*="response-content"]',
    '[class*="markdown"]',
  ].join(", ");

  let candidates = Array.from(document.querySelectorAll(SELECTORS));

  // Keep only leaf elements (remove any that contain another candidate).
  const cset = new Set(candidates);
  candidates = candidates.filter((el) => {
    for (const other of cset) {
      if (other !== el && el.contains(other)) return false;
    }
    return true;
  });

  const results = [];
  const seen    = new Set();

  for (const el of candidates) {
    if (isInSidebar(el)) continue;

    const text = (el.innerText || el.textContent || "").trim();
    if (isNoise(text)) continue;

    // Deduplicate within this snapshot by first 250 chars.
    const fp = text.slice(0, 250);
    if (seen.has(fp)) continue;
    seen.add(fp);

    const cls = (el.className || "").trim().split(/\s+/).slice(0, 5).join(" ");
    results.push({ role: classifyRole(el, text), text, _cls: cls });
  }

  // Fallback: nothing matched specific selectors — dump a slice of the page
  // so the caller knows the script ran but selectors need tuning.
  if (results.length === 0) {
    const bodyText = (document.body && document.body.innerText) || "";
    results.push({ role: "raw_page", text: bodyText.slice(0, 6000), _cls: "body" });
  }

  return JSON.stringify(results);
})();
"""


# ─────────────────────────────────────────────────────────────────────────────
# CDP client (unchanged from v1)
# ─────────────────────────────────────────────────────────────────────────────
class CDPClient:
    def __init__(self, ws_url: str):
        self.ws_url = ws_url
        self.ws = None
        self._id = 0
        self._pending: dict[int, asyncio.Future] = {}

    async def connect(self):
        self.ws = await websockets.connect(self.ws_url, max_size=10 * 1024 * 1024)
        asyncio.create_task(self._recv_loop())

    async def _recv_loop(self):
        async for raw in self.ws:
            msg = json.loads(raw)
            if "id" in msg and msg["id"] in self._pending:
                self._pending.pop(msg["id"]).set_result(msg)

    async def send(self, method: str, params: dict = None) -> dict:
        self._id += 1
        msg_id = self._id
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[msg_id] = fut
        payload = {"id": msg_id, "method": method, "params": params or {}}
        await self.ws.send(json.dumps(payload))
        return await asyncio.wait_for(fut, timeout=10.0)

    async def close(self):
        if self.ws:
            await self.ws.close()


async def find_antigravity_target(host: str, port: int) -> dict | None:
    url = f"http://{host}:{port}/json"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                targets = await resp.json(content_type=None)
    except Exception:
        return None

    preferred = [
        t for t in targets
        if t.get("type") == "page"
        and "webviewkey" not in t.get("url", "")
        and t.get("webSocketDebuggerUrl")
    ]
    if not preferred:
        preferred = [
            t for t in targets
            if t.get("type") in ("page", "other") and t.get("webSocketDebuggerUrl")
        ]
    if not preferred:
        return None

    preferred.sort(key=lambda t: len(t.get("title", "")), reverse=True)
    return preferred[0]


async def eval_js(cdp: CDPClient, js: str) -> str | None:
    try:
        result = await cdp.send("Runtime.evaluate", {
            "expression": js,
            "returnByValue": True,
            "awaitPromise": False,
        })
        val = result.get("result", {}).get("result", {})
        return val.get("value")
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Session logger with stabilization buffer
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class _Pending:
    turn: dict
    count: int = 0


class SessionLogger:
    """
    Stabilization logic
    ───────────────────
    Each DOM snapshot is a set of (fingerprint → turn) pairs.
    A turn is only committed to the log once its fingerprint has appeared
    in `stabilize` consecutive polls unchanged.

    This automatically handles:
      - Typing-in-progress:  text changes every poll → never stabilises → purged
      - Streaming responses: text grows every poll  → never stabilises → purged
                             once the agent finishes → stabilises → committed ✓
    """

    def __init__(self, project: Path, task: str, stabilize: int = 2, debug: bool = False):
        log_dir = project / ".agents" / "log"
        log_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.path    = log_dir / f"{ts}_{task}_trace.md"
        self.stabilize = stabilize
        self.debug   = debug
        self._seen:    set[str]          = set()
        self._turns:   list[dict]        = []
        self._pending: dict[str, _Pending] = {}
        self._write_header(task, project)
        print(f"[✓] Logging to: {self.path}")
        if debug:
            print(f"[!] Debug mode on — _cls info included in log")

    # ── header ───────────────────────────────────────────────────────────────
    def _write_header(self, task: str, project: Path):
        header = "\n".join([
            "# Antigravity Session Trace",
            f"**Started:** {datetime.now(timezone.utc).isoformat()}",
            f"**Project:** `{project.resolve()}`",
            f"**Task:** {task}",
            "",
            "---",
            "",
        ])
        self.path.write_text(header, encoding="utf-8")

    # ── fingerprint ──────────────────────────────────────────────────────────
    @staticmethod
    def _fp(text: str) -> str:
        return hashlib.md5(text.encode("utf-8")).hexdigest()

    # ── ingest a snapshot ────────────────────────────────────────────────────
    def ingest(self, turns: list[dict]):
        # Build the set of fingerprints present in this snapshot.
        active: set[str] = set()
        for t in turns:
            text = (t.get("text") or "").strip()
            if text:
                active.add(self._fp(text))

        # Purge pending entries that have disappeared (text changed / user kept typing).
        for fp in list(self._pending.keys()):
            if fp not in active:
                del self._pending[fp]

        newly_committed = False
        for turn in turns:
            text = (turn.get("text") or "").strip()
            if not text:
                continue
            fp = self._fp(text)
            if fp in self._seen:
                continue

            if fp not in self._pending:
                self._pending[fp] = _Pending(
                    turn={**turn, "captured_at": datetime.now(timezone.utc).isoformat()}
                )
            else:
                self._pending[fp].count += 1
                if self._pending[fp].count >= self.stabilize:
                    committed = self._pending.pop(fp)
                    self._seen.add(fp)
                    self._turns.append(committed.turn)
                    newly_committed = True
                    role = committed.turn.get("role", "?")
                    snippet = committed.turn["text"][:60].replace("\n", " ")
                    print(f"  [+] {ROLE_ICONS.get(role, '❓')} {role.upper()}: {snippet}…")

        if newly_committed:
            self._flush()

    # ── write the log ────────────────────────────────────────────────────────
    def _flush(self):
        lines: list[str] = [
            "# Antigravity Session Trace",
            "",
        ]
        for t in self._turns:
            role  = t.get("role", "unknown")
            icon  = ROLE_ICONS.get(role, "❓")
            label = role.upper().replace("_", " ")
            ts    = t.get("captured_at", "")

            lines.append(f"### {icon} {label}  `{ts}`")
            lines.append("")

            if self.debug and t.get("_cls"):
                lines.append(f"> *_cls: `{t['_cls']}`*")
                lines.append("")

            lines.append(t["text"].strip())
            lines.append("")
            lines.append("---")
            lines.append("")

        self.path.write_text("\n".join(lines), encoding="utf-8")

    # ── finalise ─────────────────────────────────────────────────────────────
    def finalise(self):
        self._flush()
        print(f"\n[✓] Saved {len(self._turns)} turn(s) → {self.path}")


# ─────────────────────────────────────────────────────────────────────────────
# Main loop
# ─────────────────────────────────────────────────────────────────────────────
async def run(args):
    project = Path(args.project).resolve()
    logger  = SessionLogger(
        project,
        args.task,
        stabilize=args.stabilize,
        debug=args.debug,
    )

    stop_event = asyncio.Event()
    loop = asyncio.get_event_loop()

    def _sig_handler():
        print("\n[*] Stopping…")
        stop_event.set()

    loop.add_signal_handler(signal.SIGINT,  _sig_handler)
    loop.add_signal_handler(signal.SIGTERM, _sig_handler)

    print(f"[*] Connecting to CDP on {CDP_HOST}:{args.port}…")
    target = None
    for attempt in range(30):
        target = await find_antigravity_target(CDP_HOST, args.port)
        if target:
            break
        if attempt == 0:
            print("    (waiting for Antigravity to start…)")
        await asyncio.sleep(2)

    if not target:
        print(f"[error] No Antigravity CDP target on port {args.port}.")
        print(f"        Launch with --remote-debugging-port={args.port}")
        sys.exit(1)

    print(f"[*] Attached: {target.get('title','?')}  ({target.get('url','?')[:70]})")
    print(f"[*] Stabilize: {args.stabilize} polls ({args.stabilize * POLL_INTERVAL:.0f}s)")

    cdp = CDPClient(target["webSocketDebuggerUrl"])
    await cdp.connect()
    await cdp.send("Runtime.enable")

    consecutive_errors = 0
    while not stop_event.is_set():
        try:
            raw = await eval_js(cdp, EXTRACT_JS)
            if raw:
                turns = json.loads(raw)
                if turns:
                    logger.ingest(turns)
                    consecutive_errors = 0
            await asyncio.wait_for(
                asyncio.shield(stop_event.wait()),
                timeout=POLL_INTERVAL,
            )
        except asyncio.TimeoutError:
            pass
        except Exception as e:
            consecutive_errors += 1
            if consecutive_errors > 5:
                print(f"[warn] Repeated DOM errors: {e}")
                consecutive_errors = 0

    await cdp.close()
    logger.finalise()


# ─────────────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Log Antigravity agent sessions via CDP → .agents/log/",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--project", type=Path, default=Path.cwd(),
        help="Project root where .agents/log/ will be written (default: cwd)",
    )
    parser.add_argument(
        "--task", default="session",
        help="Task name suffix for the log file (default: session)",
    )
    parser.add_argument(
        "--port", type=int, default=CDP_PORT,
        help=f"CDP debug port (default: {CDP_PORT})",
    )
    parser.add_argument(
        "--stabilize", type=int, default=2,
        metavar="N",
        help=(
            "Require N consecutive identical polls before committing a turn "
            f"(default: 2 = ~{2*POLL_INTERVAL:.0f}s). Raise if streaming "
            "responses are still being captured mid-stream."
        ),
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="Include DOM _cls info in log (helps tune selectors when output is wrong)",
    )
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()