#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["websockets>=12.0", "aiohttp>=3.9"]
# ///
"""
ag-log: Unified Antigravity Orchestrator
Manages tmux sessions, launches the Antigravity browser, and logs traces.

Usage:
  ag-log                  # Defaults: phoneiep, current dir, 'session'
  ag-log task1            # Defaults for profile/dir, task set to 'task1'
  ag-log -p alt -t task2  # Explicit overrides
"""

import asyncio
import aiohttp
import hashlib
import websockets
import json
import os
import sys
import argparse
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime, timezone

# --- CONFIGURATION ---
CDP_HOST = "localhost"
CDP_PORT = 9222
POLL_INTERVAL = 3.0
PROFILES_BASE = Path("/home/nemsys/Antigravity_Profiles")

ROLE_ICONS = {
    "user":      "👤",
    "thought":   "🧠",
    "tool_call": "🔧",
    "agent":     "🤖",
    "unknown":   "❓",
}

# --- JS EXTRACTION LOGIC ---
EXTRACT_JS = r"""
(function () {
  "use strict";
  const results = [];
  const seen = new Set();

  // Select all potential containers for messages, thoughts, and tools
  const elements = document.querySelectorAll(
    '[data-testid="user-input-step"], ' +
    '.select-text.leading-relaxed, ' +
    'button, ' +
    '.flex.items-baseline, ' +
    '.truncate .flex.flex-row.items-center.gap-1'
  );

  for (const el of Array.from(elements)) {
    let role = "unknown";
    let text = (el.innerText || "").trim();

    if (!text) continue;

    // 1. USER MESSAGES
    if (el.matches('[data-testid="user-input-step"]')) {
       const inner = el.querySelector(".whitespace-pre-wrap");
       if (inner) text = inner.innerText.trim();
       role = "user";
    }
    // 2. AGENT MESSAGES & THOUGHT BODIES
    else if (el.classList.contains("leading-relaxed") && el.classList.contains("select-text")) {
       // Thoughts have a specific opacity class or are hidden in accordions
       if (el.className.includes("opacity-70") || el.closest('.overflow-hidden')) {
           role = "thought";
       } else {
           role = "agent";
       }
    }
    // 3. META TIMERS & EXPLORATION LABELS
    else if (el.tagName === "BUTTON") {
        const flatText = text.replace(/\n/g, " ").trim();
        if (/^(Thought for|Worked for|Explored)/i.test(flatText)) {
            role = "thought";
            text = `[ ${flatText} ]`;
        } else {
            continue; // Ignore generic UI buttons (e.g., "Plan", "Gemini 3.1 Pro")
        }
    }
    // 4. TOOL CALLS (Commands run)
    else if (el.classList.contains("items-baseline")) {
        const flatText = text.replace(/\n/g, " ").trim();
        if (flatText.startsWith("Ran ")) {
            role = "tool_call";
            text = `> ${flatText}`;
        } else {
            continue;
        }
    }
    // 5. TOOL CALLS (Folders Analyzed)
    else if (text.startsWith("Analyzed ")) {
         const flatText = text.replace(/\n/g, " ").trim();
         role = "tool_call";
         text = `> ${flatText}`;
    }
    else {
        continue;
    }

    // Clean up VSCode icon fonts that render as raw text in the DOM
    text = text.replace(/alternate_email\s*content_copy/g, "").trim();
    text = text.replace(/chevron_right/g, "").trim();

    if (!text || text.length < 2) continue;

    // Deduplication (prevents logging the same element multiple times during polls)
    const fp = role + ":" + text.slice(0, 150);
    if (seen.has(fp)) continue;
    seen.add(fp);

    results.push({ role, text, _cls: el.className.split(/\s+/).slice(0, 3).join(" ") });
  }

  // Fallback if the UI changes drastically in the future
  if (results.length === 0 && document.body) {
    results.push({ role: "raw_page", text: document.body.innerText.slice(0, 500), _cls: "body" });
  }

  return JSON.stringify(results);
})();
"""

# --- CDP & LOGGING CLASSES ---
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
        fut = asyncio.get_event_loop().create_future()
        self._pending[msg_id] = fut
        await self.ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        return await asyncio.wait_for(fut, timeout=10.0)

    async def close(self):
        if self.ws: await self.ws.close()

@dataclass
class _Pending:
    turn: dict
    count: int = 0

class SessionLogger:
    def __init__(self, project: Path, task: str, stabilize: int = 2, debug: bool = False):
        log_dir = project / ".agents" / "log"
        log_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.path = log_dir / f"{ts}_{task}_trace.md"
        self.stabilize, self.debug = stabilize, debug
        self._seen, self._turns, self._pending = set(), [], {}
        self._write_header(task, project)
        print(f"[✓] Logging to: {self.path}")

    def _write_header(self, task: str, project: Path):
        header = f"# Antigravity Session Trace\n**Started:** {datetime.now(timezone.utc).isoformat()}\n**Project:** `{project.resolve()}`\n**Task:** {task}\n\n---\n\n"
        self.path.write_text(header, encoding="utf-8")

    def ingest(self, turns: list[dict]):
        active = {hashlib.md5(t["text"].strip().encode()).hexdigest() for t in turns if t.get("text")}
        for fp in list(self._pending.keys()):
            if fp not in active: del self._pending[fp]

        newly_committed = False
        for turn in turns:
            text = turn.get("text", "").strip()
            if not text: continue
            fp = hashlib.md5(text.encode()).hexdigest()
            if fp in self._seen: continue

            if fp not in self._pending:
                self._pending[fp] = _Pending(turn={**turn, "captured_at": datetime.now(timezone.utc).isoformat()})
            else:
                self._pending[fp].count += 1
                if self._pending[fp].count >= self.stabilize:
                    committed = self._pending.pop(fp)
                    self._seen.add(fp); self._turns.append(committed.turn)
                    newly_committed = True
                    print(f"  [+] {ROLE_ICONS.get(committed.turn['role'], '❓')} {committed.turn['role'].upper()}: {text[:60]}…")

        if newly_committed: self._flush()

    def _flush(self):
        lines = ["# Antigravity Session Trace", ""]
        for t in self._turns:
            lines.extend([f"### {ROLE_ICONS.get(t['role'], '❓')} {t['role'].upper().replace('_', ' ')}  `{t['captured_at']}`", ""])
            if self.debug and t.get("_cls"): lines.extend([f"> *_cls: `{t['_cls']}`*", ""])
            lines.extend([t["text"].strip(), "", "---", ""])
        self.path.write_text("\n".join(lines), encoding="utf-8")

# --- ORCHESTRATION & RUNTIME ---
def orchestrate_tmux(args):
    session = f"ag-{args.profile}-{args.task}"
    
    if subprocess.run(["tmux", "has-session", "-t", session], capture_output=True).returncode == 0:
        print(f"[*] Attaching to: {session}")
        os.execvp("tmux", ["tmux", "attach-session", "-t", session])

    prof_dir = PROFILES_BASE / args.profile
    app_config = prof_dir / "app_config"
    browser_profile = prof_dir / "browser_profile"

    # Browser Command
    # Browser Command
    browser_cmd = (
        f"HOME='{app_config}' antigravity "
        f"--user-data-dir='{browser_profile}' "
        f"--remote-debugging-port={args.port} "
        f"'{args.project}'"
    )

    # Worker Command - Explicit Flags to match parser exactly
    worker_cmd = (
        f"cd {args.project} && {os.path.abspath(__file__)} --worker "
        f"-t {args.task} -p {args.profile} -d {args.project} "
        f"--port {args.port} --stabilize {args.stabilize}"
    )
    if args.debug: worker_cmd += " --debug"

    print(f"[*] Starting tmux session: {session}")
    subprocess.run(["tmux", "new-session", "-d", "-s", session, "-x", "220", "-y", "50"])
    subprocess.run(["tmux", "send-keys", "-t", f"{session}:0.0", browser_cmd, "C-m"])
    subprocess.run(["tmux", "split-window", "-h", "-t", f"{session}:0.0"])
    subprocess.run(["tmux", "send-keys", "-t", f"{session}:0.1", worker_cmd, "C-m"])
    
    os.execvp("tmux", ["tmux", "attach-session", "-t", session])

async def run_worker(args):
    project = Path(args.project).resolve()
    logger = SessionLogger(project, args.task, stabilize=args.stabilize, debug=args.debug)
    stop_event = asyncio.Event()
    
    loop = asyncio.get_event_loop()
    loop.add_signal_handler(signal.SIGINT, stop_event.set)
    loop.add_signal_handler(signal.SIGTERM, stop_event.set)

    print(f"[*] Connecting to CDP on {args.port}...")
    target = None
    for _ in range(30):
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(f"http://{CDP_HOST}:{args.port}/json") as r:
                    targets = await r.json(content_type=None)
                    target = next((t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")), None)
                    if target: break
        except: pass
        await asyncio.sleep(2)

    if not target: sys.exit("[error] No Antigravity target found.")

    cdp = CDPClient(target["webSocketDebuggerUrl"])
    await cdp.connect()
    await cdp.send("Runtime.enable")

    while not stop_event.is_set():
        try:
            raw = await cdp.send("Runtime.evaluate", {"expression": EXTRACT_JS, "returnByValue": True})
            val = raw.get("result", {}).get("result", {}).get("value")
            if val: logger.ingest(json.loads(val))
            await asyncio.wait_for(stop_event.wait(), timeout=POLL_INTERVAL)
            
        except asyncio.TimeoutError:
            pass
            
        except websockets.exceptions.ConnectionClosed:
            print("\n[*] Browser disconnected (WebSocket closed). Exiting logger...")
            break
            
        except Exception as e:
            # Fallback catch for abruptly dropped sockets
            if "close frame" in str(e) or "Connection closed" in str(e):
                print("\n[*] Target disconnected. Exiting logger...")
                break
            
            print(f"[!] Error: {e}")
            await asyncio.sleep(2)  # Prevent rapid-fire console spam on transient errors

    await cdp.close()
    logger._flush()

def main():
    parser = argparse.ArgumentParser(description="Antigravity Orchestrator & Logger")
    
    # Task as positional (with default) for 'ag-log task1'
    parser.add_argument("task_pos", nargs="?", default=None, help="Task name (positional shortcut)")
    
    # Flags for everything else
    parser.add_argument("-t", "--task", default="session")
    parser.add_argument("-p", "--profile", default="phoneiep")
    parser.add_argument("-d", "--project", default=os.getcwd())
    parser.add_argument("--port", type=int, default=CDP_PORT)
    parser.add_argument("--stabilize", type=int, default=2)
    parser.add_argument("--debug", action="store_true")
    
    # Internal worker flag
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    
    args = parser.parse_args()

    # If the positional shortcut was used, override the task flag
    if args.task_pos:
        args.task = args.task_pos

    if args.worker:
        asyncio.run(run_worker(args))
    else:
        orchestrate_tmux(args)

if __name__ == "__main__":
    main()