#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["websockets>=12.0", "aiohttp>=3.9"]
# ///
"""
antigravity-logger v3
Captures Antigravity agent conversations via Chrome DevTools Protocol.
Saves clean, structured traces to .agents/log/ — zero extra AI tokens.

Self-contained: one script handles both tmux orchestration (launch mode)
and CDP capture (logger mode). No shell wrappers needed.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SETUP (one-time)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

1. Copy to ~/bin/:
     cp ag-logger.py ~/bin/ag-logger.py

2. Edit the PROFILES dict near the bottom of this file to add/adjust
   your Antigravity profiles (home dir, browser profile dir).

That's it. No aliases, no separate shell scripts required.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
USAGE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  uv run --script ~/bin/ag-logger.py [task] [profile] [project_dir]

All arguments are optional (defaults: session / phoneiep / cwd).

Examples:
  # from inside the project dir, default profile
  cd /data/projects/KBWeaver && uv run --script ~/bin/ag-logger.py my_task

  # explicit profile and project
  uv run --script ~/bin/ag-logger.py my_task phoneiep /data/projects/KBWeaver

This opens a tmux session (ag-<profile>-<task>) with two panes:
  left  — Antigravity with --remote-debugging-port open
  right — CDP logger writing to <project>/.agents/log/<ts>_<task>_trace.md

Re-running the same command re-attaches to the existing session.

Stop logging : Ctrl+C in the right pane
Detach       : Ctrl+B then D
Re-attach    : uv run --script ~/bin/ag-logger.py <task> <profile> <project>

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
OPTIONS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  --port N        CDP debug port (default: 9222)
  --stabilize N   Polls required before committing a turn (default: 2 = ~6s).
                  Prevents capturing typing-in-progress and mid-stream responses.
                  Raise to 3-4 if agent responses are still captured mid-stream.
  --debug         Include DOM _cls info in each log entry (selector calibration).

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CALIBRATION
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

If the log is noisy or missing content, run with --debug.
The _cls field shows which DOM classes each captured element has.
Tune NOISE_EXACT / NOISE_RE / SELECTORS inside EXTRACT_JS accordingly,
or report the _cls values and they can be refined.

If _scoped: false appears in debug output, the chat root finder fell back
to document.body — share the _cls values to get the right selector added.
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

  // ── Step 1: Find the chat panel root ─────────────────────────────────────
  function containerOf(el) {
    let node = el.parentElement;
    for (let i = 0; i < 25 && node && node !== document.body; i++) {
      const r = node.getBoundingClientRect();
      if (r.height > 350 && r.width > 250) return node;
      node = node.parentElement;
    }
    return document.body;
  }

  function findChatRoot() {
    const byAttr = document.querySelector(
      '[placeholder*="Ask anything"], [aria-placeholder*="Ask anything"], [aria-label*="Ask anything"]'
    );
    if (byAttr) return containerOf(byAttr);
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT);
    let node;
    while ((node = walker.nextNode())) {
      const t = (node.innerText || "").trim();
      if (t === "Ask anything, @ to mention, / for workflows" && !node.children.length)
        return containerOf(node);
    }
    for (const sel of [".interactive-session", '[class*="aichat"]', '[class*="chat-widget"]']) {
      const el = document.querySelector(sel);
      if (el) return el;
    }
    return document.body;
  }

  const chatRoot     = findChatRoot();
  const scopedToBody = chatRoot === document.body;

  // ── Step 2: Helpers ───────────────────────────────────────────────────────

  // Extract visible text, stripping <style>/<script> content that innerText
  // unfortunately includes (the leading-relaxed div has an embedded <style>
  // block for markdown-alert CSS).
  function cleanText(el) {
    const clone = el.cloneNode(true);
    for (const tag of clone.querySelectorAll("style, script")) tag.remove();
    return (clone.innerText || clone.textContent || "").trim();
  }

  // Walk ancestors up to (but not including) stopEl.
  // Return the first ancestor whose immediately preceding sibling is a
  // button/div whose text matches `pattern`.
  function ancestorPrecededBy(el, pattern, stopEl) {
    let node = el.parentElement;
    while (node && node !== stopEl) {
      let sib = node.previousElementSibling;
      while (sib) {
        const txt = (sib.innerText || sib.textContent || "").trim();
        if (pattern.test(txt)) return node;
        sib = sib.previousElementSibling;
      }
      node = node.parentElement;
    }
    return null;
  }

  // ── Step 3: Noise filter ─────────────────────────────────────────────────
  const NOISE_EXACT = new Set([
    "Plan", "Send", "mic", "Review Changes", "Accept all", "Reject all",
    "See all", "Attach", "alternate_email", "content_copy", "python", "text",
    "Markdown", "Go Live", "Antigravity - Settings",
    "Drag a view here to display.",
  ]);
  const NOISE_RE = [
    /^Ask anything[,.].*@ to mention/is,
    /^AI may make mistakes\./i,
    /^Gemini \d/i,
    /^(Worked|Thought) for \d/i,               // collapsible headers
    /^Explored \d/i,                            // tool summary "Explored 1 folder"
    /^Analyzed /i,                              // tool summary "Analyzed /path"
    /^\d+ Files? With Changes\s*$/,
    /^[+-]\d+\s*$/,
    /^\+\d+ -\d+$/,
    /^Press desired key/i,
    /^[A-Z][a-zA-Z ]+\n\d+[hm]\s*$/,
    /^F-\d+\s+LF\s+/,
    /^AG:\s*\d+-\d+%/,
    /^\w[\w.-]*\.(py|js|ts|md|json|yaml|sh|txt)\s*$/,
    /^\w[\w.-]*\.(py|js|ts|md|json|yaml|sh|txt)\s+\+\d+-\d+$/,
    /^\/\*.*?alert\.css/s,                      // the embedded markdown-alert CSS block
  ];
  function isNoise(text, minLen) {
    if (!text || text.length < (minLen || 2)) return true;
    if (NOISE_EXACT.has(text)) return true;
    return NOISE_RE.some((re) => re.test(text));
  }

  // ── Step 4: Role-specific extraction ─────────────────────────────────────
  //
  // Real DOM structure (confirmed from DevTools export):
  //
  //  USER turn
  //    [data-testid="user-input-step"]
  //      .whitespace-pre-wrap          ← user text
  //
  //  AGENT turn
  //    button "Worked for Xs"          ← outer collapsible toggle
  //    div (outer collapsible body)
  //      ...tool summaries...
  //      div                           ← inner collapsible container
  //        button "Thought for Xs"     ← inner thought toggle
  //        div.px-2.py-1
  //          div.leading-relaxed.select-text   ← THOUGHT content
  //      div                           ← another inner collapsible
  //        button "Thought for Xs"
  //        div.px-2.py-1
  //          div.leading-relaxed.select-text   ← THOUGHT content
  //    div.px-2.py-1                   ← AGENT response (sibling of "Worked for" btn)
  //      div.leading-relaxed.select-text       ← AGENT response content

  const results = [];
  const seenFp  = new Set();

  function push(role, text) {
    if (isNoise(text, role === "user" ? 1 : 4)) return;
    const fp = text.slice(0, 300);
    if (seenFp.has(fp)) return;
    seenFp.add(fp);
    results.push({ role, text, _scoped: !scopedToBody });
  }

  // 4a. USER messages
  for (const step of chatRoot.querySelectorAll('[data-testid="user-input-step"]')) {
    const textEl = step.querySelector(".whitespace-pre-wrap");
    if (textEl) push("user", cleanText(textEl));
  }

  // 4b. AGENT/THOUGHT content — all leading-relaxed.select-text blocks
  const THOUGHT_FOR_RE  = /^Thought for \d/i;
  const WORKED_FOR_RE   = /^Worked for \d/i;

  for (const el of chatRoot.querySelectorAll("div.leading-relaxed.select-text")) {
    const text = cleanText(el);
    if (!text) continue;

    // Is this block inside a "Thought for Xs" sub-collapsible?
    // Walk ancestors looking for a sibling button matching "Thought for".
    const insideThought = ancestorPrecededBy(el, THOUGHT_FOR_RE, chatRoot);

    // Is this block at the top level of a "Worked for Xs" outer collapsible?
    // i.e. preceded by a "Worked for" button at some ancestor level, but NOT
    // inside a nested "Thought for" sub-collapsible.
    const insideWorked  = ancestorPrecededBy(el, WORKED_FOR_RE, chatRoot);

    if (insideThought) {
      push("thought", text);
    } else if (insideWorked) {
      push("agent", text);
    } else {
      // No recognisable collapsible ancestor — treat as agent response
      push("agent", text);
    }
  }

  // 4c. TOOL calls — "Explored N folder/files", "Analyzed /path", inline refs
  for (const el of chatRoot.querySelectorAll(
    ".inline-flex.break-all.leading-tight.select-text, " +
    ".inline-flex.leading-tight.select-text"
  )) {
    push("tool_call", cleanText(el));
  }

  if (results.length === 0) {
    const snippet = (document.body && document.body.innerText || "").slice(0, 3000);
    results.push({ role: "raw_page", text: snippet, _scoped: false });
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
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                if "id" in msg and msg["id"] in self._pending:
                    self._pending.pop(msg["id"]).set_result(msg)
        except Exception:
            pass  # connection closed (e.g. Antigravity quit) — not an error

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

                    # Superset dedup: if this turn's text contains a previously
                    # committed shorter turn (e.g. a thought heading logged before
                    # the full block stabilised), replace it rather than appending.
                    replaced = False
                    for i, existing in enumerate(self._turns):
                        existing_text = existing["text"].strip()
                        if existing_text and existing_text in text and existing_text != text:
                            self._turns[i] = committed.turn
                            replaced = True
                            role = committed.turn.get("role", "?")
                            snippet = committed.turn["text"][:60].replace("\n", " ")
                            print(f"  [↺] {ROLE_ICONS.get(role, '❓')} {role.upper()} (replaced partial): {snippet}…")
                            break

                    if not replaced:
                        self._turns.append(committed.turn)
                        role = committed.turn.get("role", "?")
                        snippet = committed.turn["text"][:60].replace("\n", " ")
                        print(f"  [+] {ROLE_ICONS.get(role, '❓')} {role.upper()}: {snippet}…")

                    newly_committed = True

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
# Profiles — maps short name → (HOME override, extra antigravity flags)
# Add entries here for each Antigravity profile you use.
# ─────────────────────────────────────────────────────────────────────────────
PROFILES: dict[str, dict] = {
    "phoneiep": {
        "home":    "/home/nemsys/Antigravity_Profiles/phoneiep/app_config",
        "userdir": "/home/nemsys/Antigravity_Profiles/phoneiep/browser_profile",
    },
    "progressbg.ml.course": {
        "home":    "/home/nemsys/Antigravity_Profiles/progressbg.ml.course/app_config",
        "userdir": "/home/nemsys/Antigravity_Profiles/progressbg.ml.course/browser_profile",
    },
    # Add more profiles here:
    # "myprofile": { "home": "...", "userdir": "..." },
}


def tmux_launch(args) -> None:
    """
    Launcher mode — runs when we are NOT already inside the target tmux pane.

    Creates (or attaches to) a tmux session with two panes side-by-side:
      left  — Antigravity with CDP port open
      right — this script in logger mode (--_logger flag)
    """
    import subprocess, shlex, os

    profile = PROFILES.get(args.profile)
    if profile is None:
        known = ", ".join(PROFILES)
        print(f"[error] Unknown profile '{args.profile}'. Known: {known}")
        sys.exit(1)

    session = f"ag-{args.profile}-{args.task}"
    project = Path(args.project).resolve()

    # ── reattach if session already running ──────────────────────────────
    result = subprocess.run(["tmux", "has-session", "-t", session],
                            capture_output=True)
    if result.returncode == 0:
        print(f"[*] Attaching to existing session: {session}")
        os.execlp("tmux", "tmux", "attach-session", "-t", session)
        return  # unreachable

    # ── build the Antigravity launch command ─────────────────────────────
    ag_env   = f'HOME="{profile["home"]}"'
    ag_flags = (
        f'--user-data-dir="{profile["userdir"]}" '
        f'--remote-debugging-port={args.port}'
    )
    ag_cmd = f'{ag_env} antigravity {ag_flags} "{project}"'

    # ── build the logger re-invocation command ───────────────────────────
    script   = Path(__file__).resolve()
    log_args = (
        f"{shlex.quote(args.task)} "
        f"{shlex.quote(args.profile)} "
        f"{shlex.quote(str(project))} "
        f"--_logger "
        f"--port {args.port} "
        f"--stabilize {args.stabilize} "
        + ("--debug " if args.debug else "")
    )
    log_cmd = f"cd {shlex.quote(str(project))} && uv run --script {script} {log_args}"

    print(f"[*] Starting tmux session: {session}")
    print(f"    left  → {ag_cmd[:80]}…")
    print(f"    right → uv run ag-logger.py {log_args[:60]}…")

    subprocess.run(["tmux", "new-session",  "-d", "-s", session, "-x", "220", "-y", "50"])
    subprocess.run(["tmux", "send-keys", "-t", f"{session}:0.0", ag_cmd, "Enter"])
    subprocess.run(["tmux", "split-window", "-h", "-t", f"{session}:0.0"])
    subprocess.run(["tmux", "send-keys", "-t", f"{session}:0.1", log_cmd, "Enter"])
    os.execlp("tmux", "tmux", "attach-session", "-t", session)


# ─────────────────────────────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Antigravity session logger: launch mode OR logger mode.\n\n"
            "  Launch mode (default): ag-logger.py [task] [profile] [project]\n"
            "    Creates a tmux session with Antigravity + logger side-by-side.\n\n"
            "  Logger mode (internal): ag-logger.py --_logger ...\n"
            "    Runs the CDP capture loop. Called automatically inside tmux."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # ── positional-style shortcuts for launch mode ──────────────────────
    parser.add_argument("task",    nargs="?", default="session",
        help="Task label used in the log filename (default: session)")
    parser.add_argument("profile", nargs="?", default="phoneiep",
        help="Antigravity profile name, must exist in PROFILES dict (default: phoneiep)")
    parser.add_argument("project", nargs="?", default=None,
        help="Project directory (default: cwd)")

    # ── named flags ────────────────────────────────────────────────────
    parser.add_argument("--port", type=int, default=CDP_PORT,
        help=f"CDP debug port (default: {CDP_PORT})")
    parser.add_argument("--stabilize", type=int, default=2, metavar="N",
        help=(
            f"Polls required before committing a turn (default: 2 = ~{2*POLL_INTERVAL:.0f}s). "
            "Raise if responses are captured mid-stream."
        ))
    parser.add_argument("--debug", action="store_true",
        help="Include DOM _cls info in log output (selector calibration)")

    # ── internal flag — set automatically when re-invoked inside tmux ──
    parser.add_argument("--_logger", action="store_true",
        help=argparse.SUPPRESS)

    return parser


def main():
    parser = build_parser()
    args   = parser.parse_args()

    if args.project is None:
        args.project = Path.cwd()

    if args._logger:
        # ── logger mode: CDP capture loop ─────────────────────────────
        asyncio.run(run(args))
    else:
        # ── launch mode: tmux orchestration ───────────────────────────
        tmux_launch(args)


if __name__ == "__main__":
    main()