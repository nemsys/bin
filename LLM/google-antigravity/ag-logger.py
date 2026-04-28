#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["websockets>=12.0", "aiohttp>=3.9"]
# ///
"""
antigravity-logger
Captures Antigravity agent conversations in real time via Chrome DevTools Protocol
and saves them to .agents/log/ — zero extra AI tokens.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SETUP (one-time)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

1. Add these to ~/.bashrc.d/aliases.sh and reload (source ~/.bashrc):

   # Antigravity launcher with CDP debug port
   alias ag-phoneiep-log='HOME="/home/nemsys/Antigravity_Profiles/phoneiep/app_config" \
     antigravity \
     --user-data-dir="/home/nemsys/Antigravity_Profiles/phoneiep/browser_profile" \
     --remote-debugging-port=9222'

   # tmux helper — opens split pane with Antigravity + logger side by side
   ag-log() {
     local profile="${1:?usage: ag-log <profile> <project_dir> <task>}"
     local project="${2:?usage: ag-log <profile> <project_dir> <task>}"
     local task="${3:-session}"
     local session="ag-${profile}-${task}"
     tmux new-session -d -s "$session" -x 220 -y 50
     tmux send-keys -t "$session" "ag-${profile}-log $project" Enter
     tmux split-window -h -t "$session"
     tmux send-keys -t "$session" \
       "cd $project && uv run ~/bin/ag-logger.py --task $task" Enter
     tmux attach-session -t "$session"
   }

2. Copy this script to ~/bin/:
   cp ag-logger.py ~/bin/

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
USAGE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  ag-log phoneiep /data/projects/KBWeaver my_task

Opens a tmux session with two panes:
  Left  — Antigravity (with CDP port 9222 open)
  Right — this logger (polls every 3s, writes to .agents/log/)

Stop logging : Ctrl+C in right pane
Detach       : Ctrl+B then D
Re-attach    : tmux attach -t ag-phoneiep-my_task

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FIRST RUN — calibration
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
After the first session check the log:

  cat <project>/.agents/log/<timestamp>_<task>_trace.md

If captured correctly — done.
If empty or shows raw_panel dumps — the EXTRACT_JS selectors need tuning
for Antigravity's exact DOM. Report back and they will be refined.
"""

import asyncio
import aiohttp
import websockets
import json
import re
import sys
import argparse
import signal
from pathlib import Path
from datetime import datetime, timezone

CDP_HOST = "localhost"
CDP_PORT = 9222

# How often to poll the DOM for new messages (seconds)
POLL_INTERVAL = 3.0

# JS that extracts conversation turns from Antigravity's rendered DOM.
# Antigravity renders chat in a tree of elements — this tries several
# selector strategies and returns whatever it finds.
EXTRACT_JS = r"""
(function() {
  const results = [];
  
  function getMessagesFromRoot(root) {
      if (!root) return;
      
      // Antigravity's Tailwind UI uses .select-text for chat bubbles
      // We also look for standard VS Code classes just in case
      const candidates = Array.from(root.querySelectorAll(
          '.select-text, .interactive-item-container, .chat-list-item, [class*="message"]'
      ));
      
      // Filter out containers that hold other candidates to avoid duplicate text
      const filtered = candidates.filter(el => {
          for (const child of candidates) {
              if (child !== el && el.contains(child)) return false;
          }
          return true;
      });

      for (const el of filtered) {
          const text = el.innerText || el.textContent || '';
          if (text.trim().length > 1) {
              let role = 'turn';
              if (text.includes('Thought for ') || text.includes('Ran ') || el.querySelector('code')) {
                  role = 'agent';
              } else {
                  role = 'user';
              }
              results.push({ role: role, text: text.trim() });
          }
      }
      
      // Traverse iframes (often used for webviews in electron if accessible)
      const iframes = root.querySelectorAll('iframe');
      for (const frame of iframes) {
          try {
              if (frame.contentDocument) getMessagesFromRoot(frame.contentDocument);
          } catch(e) {}
      }
  }

  getMessagesFromRoot(document);
  
  // If we found specific message blocks, return them!
  if (results.length > 0) {
      return JSON.stringify(results);
  }

  // Fallback: If we couldn't find exact message blocks, find the deepest element 
  // that contains 'Thought for' or 'Ask anything' to avoid grabbing the whole sidebar
  let bestNode = null;
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT);
  let node;
  while (node = walker.nextNode()) {
      const text = node.innerText || '';
      if (text.includes('Thought for') || text.includes('How can I help')) {
          bestNode = node;
      }
  }
  
  if (bestNode) {
      return JSON.stringify([{ role: 'raw_chat', text: bestNode.innerText.trim() }]);
  }

  return JSON.stringify([]);
})();
"""

# Simpler fallback: grab the full innerText of the document body
FALLBACK_JS = "JSON.stringify([{role:'full_page', text: document.body.innerText}])"


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
    """Find the main Antigravity renderer target via CDP /json endpoint."""
    url = f"http://{host}:{port}/json"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                targets = await resp.json(content_type=None)
    except Exception as e:
        return None

    # Prefer targets that look like the main editor window
    preferred = [
        t for t in targets
        if t.get("type") == "page"
        and "webviewkey" not in t.get("url", "")
        and t.get("webSocketDebuggerUrl")
    ]
    # Fall back to any page target
    if not preferred:
        preferred = [
            t for t in targets
            if t.get("type") in ("page", "other")
            and t.get("webSocketDebuggerUrl")
        ]

    if not preferred:
        return None

    # Pick the one with the most content (heuristic)
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


def normalise_text(t: str) -> str:
    return re.sub(r'\s+', ' ', t).strip()


class SessionLogger:
    def __init__(self, project: Path, task: str):
        log_dir = project / ".agents" / "log"
        log_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.path = log_dir / f"{ts}_{task}_trace.md"
        self._seen: set[str] = set()
        self._turns: list[dict] = []
        self._write_header(task, project)
        print(f"[✓] Logging to: {self.path}")

    def _write_header(self, task: str, project: Path):
        header = "\n".join([
            "# Antigravity Session Trace (CDP)",
            f"**Started:** {datetime.now(timezone.utc).isoformat()}",
            f"**Project:** `{project.resolve()}`",
            f"**Task:** {task}",
            "",
            "---",
            "",
        ])
        self.path.write_text(header, encoding="utf-8")

    def ingest(self, turns: list[dict]):
        """Add new turns that haven't been seen before."""
        new_found = False
        for turn in turns:
            key = normalise_text(turn.get("text", ""))[:200]
            if not key or key in self._seen:
                continue
            self._seen.add(key)
            self._turns.append({
                "role": turn.get("role", "?"),
                "text": turn.get("text", ""),
                "captured_at": datetime.now(timezone.utc).isoformat(),
            })
            new_found = True

        if new_found:
            self._flush()

    def _flush(self):
        lines = ["# Antigravity Session Trace (CDP)", ""]
        for t in self._turns:
            role = t["role"].upper()
            ts = t["captured_at"]
            lines.append(f"### [{role}]  `{ts}`")
            lines.append(t["text"].strip())
            lines.append("")
        self.path.write_text("\n".join(lines), encoding="utf-8")

    def finalise(self):
        self._flush()
        print(f"\n[✓] Saved {len(self._turns)} turn(s) → {self.path}")


async def run(args):
    project = Path(args.project).resolve()
    logger = SessionLogger(project, args.task)

    stop_event = asyncio.Event()
    loop = asyncio.get_event_loop()

    def _sig_handler():
        print("\n[*] Stopping…")
        stop_event.set()

    loop.add_signal_handler(signal.SIGINT, _sig_handler)
    loop.add_signal_handler(signal.SIGTERM, _sig_handler)

    # Wait for Antigravity to be available
    print(f"[*] Connecting to CDP on {CDP_HOST}:{args.port}…")
    target = None
    for attempt in range(30):
        target = await find_antigravity_target(CDP_HOST, args.port)
        if target:
            break
        if attempt == 0:
            print(f"    (waiting for Antigravity to start…)")
        await asyncio.sleep(2)

    if not target:
        print(f"[error] Could not find Antigravity CDP target on port {args.port}")
        print(f"        Make sure Antigravity is running with --remote-debugging-port={args.port}")
        sys.exit(1)

    print(f"[*] Attached to: {target.get('title', '?')} ({target.get('url', '?')[:60]})")

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
            pass  # normal — just means stop wasn't set yet
        except Exception as e:
            consecutive_errors += 1
            if consecutive_errors > 5:
                print(f"[warn] Repeated errors extracting DOM: {e}")
                consecutive_errors = 0

    await cdp.close()
    logger.finalise()


def main():
    parser = argparse.ArgumentParser(
        description="Log Antigravity agent sessions via CDP → .agents/log/",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--project", type=Path, default=Path.cwd(),
        help="Project root where .agents/log/ will be written (default: cwd)")
    parser.add_argument("--task", default="session",
        help="Task name suffix for the log file (default: session)")
    parser.add_argument("--port", type=int, default=CDP_PORT,
        help=f"CDP debug port (default: {CDP_PORT})")
    args = parser.parse_args()

    asyncio.run(run(args))


if __name__ == "__main__":
    main()