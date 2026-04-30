#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["websockets>=12.0", "aiohttp>=3.9"]
# ///
"""
ag-logger-opus: Antigravity session logger v4
Captures all agent chat messages (user, thoughts, tool calls, agent responses)
via Chrome DevTools Protocol. Saves structured traces to .agents/log/.

Fixes from previous versions:
  - No CSS/style contamination in output
  - Correct role classification (thought vs agent)
  - Reads hidden collapsible content without clicking
  - Better dedup, ordered output
  - Skips non-chat pages (project picker, etc.)

Usage:
  uv run --script ag-logger-opus.py [task] [profile] [project_dir]
  uv run --script ag-logger-opus.py my_task phoneiep /data/projects/MyProject

Flags:
  --port N        CDP debug port (default: 9222)
  --stabilize N   Polls before committing a turn (default: 2)
  --debug         Include DOM class info in output
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

CDP_HOST = "localhost"
CDP_PORT = 9222
POLL_INTERVAL = 3.0
PROFILES_BASE = Path("/home/nemsys/Antigravity_Profiles")

ROLE_ICONS = {
    "user":      "\U0001f464",
    "thought":   "\U0001f9e0",
    "tool_call": "\U0001f527",
    "agent":     "\U0001f916",
    "unknown":   "\u2753",
}

# ---------------------------------------------------------------------------
# JavaScript injected via CDP to extract chat turns
# ---------------------------------------------------------------------------
EXTRACT_JS = r"""
(function () {
  "use strict";

  // ── Find chat panel root ──────────────────────────────────────────────
  function containerOf(el) {
    let n = el.parentElement;
    for (let i = 0; i < 30 && n && n !== document.body; i++) {
      const r = n.getBoundingClientRect();
      if (r.height > 350 && r.width > 250) return n;
      n = n.parentElement;
    }
    return null;
  }

  function findChatRoot() {
    for (const sel of [
      '[placeholder*="Ask anything"]',
      '[aria-placeholder*="Ask anything"]',
      '[aria-label*="Ask anything"]'
    ]) {
      const el = document.querySelector(sel);
      if (el) { const c = containerOf(el); if (c) return c; }
    }
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT);
    let node;
    while ((node = walker.nextNode())) {
      const t = (node.innerText || "").trim();
      if (t === "Ask anything, @ to mention, / for workflows" && !node.children.length) {
        const c = containerOf(node);
        if (c) return c;
      }
    }
    return null;
  }

  const chatRoot = findChatRoot();
  if (!chatRoot) {
    return JSON.stringify([{"role":"_no_chat","text":"Chat panel not found"}]);
  }

  // ── Helpers ────────────────────────────────────────────────────────────
  function cleanText(el) {
    const clone = el.cloneNode(true);
    clone.querySelectorAll("style, script, link").forEach(t => t.remove());
    let txt = (clone.innerText || clone.textContent || "").trim();
    txt = txt.replace(/\/\*[\s\S]*?\*\//g, "");
    txt = txt.replace(/@media\s*\([^)]*\)\s*\{[\s\S]*?\}\s*\}/g, "");
    txt = txt.replace(/\.[a-zA-Z_-]+\s*\{[^}]*\}/g, "");
    txt = txt.replace(/div:has\([^)]*\)[\s\S]*?(?=\n\n|\n[A-Z]|$)/g, "");
    return txt.trim();
  }

  function deepText(el) {
    const clone = el.cloneNode(true);
    clone.querySelectorAll("style, script, link").forEach(t => t.remove());
    clone.style.cssText = "display:block!important;visibility:visible!important;overflow:visible!important;height:auto!important;max-height:none!important;";
    let txt = (clone.textContent || "").trim();
    txt = txt.replace(/\/\*[\s\S]*?\*\//g, "");
    txt = txt.replace(/@media\s*\([^)]*\)\s*\{[\s\S]*?\}\s*\}/g, "");
    txt = txt.replace(/\.[a-zA-Z_-]+\s*\{[^}]*\}/g, "");
    return txt.trim();
  }

  // ── Noise filter ───────────────────────────────────────────────────────
  const NOISE_EXACT = new Set([
    "Plan", "Send", "mic", "Review Changes", "Accept all", "Reject all",
    "See all", "Attach", "alternate_email", "content_copy", "python", "text",
    "Markdown", "Go Live", "Antigravity - Settings", "chevron_right",
    "Drag a view here to display.", "add", "more_vert",
  ]);
  const NOISE_RE = [
    /^Ask anything[,.].*@ to mention/is,
    /^AI may make mistakes/i,
    /^Gemini \d/i,
    /^Claude /i,
    /^(Worked|Thought) for \d/i,
    /^\d+ Files? With Changes\s*$/,
    /^[+-]\d+\s*$/,
    /^\+\d+ -\d+$/,
    /^Press desired key/i,
    /^[A-Z][a-zA-Z ]+\n\d+[hm]\s*$/,
    /^F-\d+\s+LF\s+/,
    /^AG:\s*\d+-\d+%/,
    /^\w[\w.-]*\.(py|js|ts|md|json|yaml|sh|txt|css)\s*$/,
    /^\w[\w.-]*\.(py|js|ts|md|json|yaml|sh|txt|css)\s+\+\d+/,
    /^border-/,
    /^margin-/,
    /^padding-/,
    /^\{[\s\S]*:\s*[\s\S]*\}/,
    /^alternate_email\s*content_copy/,
  ];

  function isNoise(text, minLen) {
    if (!text || text.length < (minLen || 2)) return true;
    if (NOISE_EXACT.has(text)) return true;
    if (NOISE_RE.some(re => re.test(text))) return true;
    if (/\{[^}]*(?:border|margin|padding|color|font|display)\s*:/i.test(text)) return true;
    return false;
  }

  // ── Extract turns in document order ────────────────────────────────────
  //
  // Single unified walk: collect ALL interesting elements, sort by DOM
  // position, then classify. This guarantees correct ordering.
  //
  // Role classification uses the DOM structure confirmed from exports:
  //
  //   THOUGHT divs have class "opacity-70" on their leading-relaxed element
  //   AGENT response divs do NOT have "opacity-70"
  //
  //   Thought:  div.isolate > button"Thought for Xs"
  //                         > div.overflow-hidden > ... > div.leading-relaxed.select-text.opacity-70
  //   Agent:    div.px-2.py-1 > div.leading-relaxed.select-text (no opacity-70)
  //
  //   Both can be siblings inside the same flex-col container.

  const items = [];  // {el, role, text}
  const seenFp = new Set();

  function addItem(role, text, el, cls, skipNoise) {
    text = text.replace(/alternate_email\s*content_copy/g, "").trim();
    text = text.replace(/chevron_right/g, "").trim();
    if (!skipNoise && isNoise(text, role === "user" ? 1 : 4)) return;
    const fp = role + ":" + text.slice(0, 300);
    if (seenFp.has(fp)) return;
    seenFp.add(fp);
    items.push({ el, role, text, _cls: cls || "" });
  }

  // 1. USER messages
  for (const step of chatRoot.querySelectorAll('[data-testid="user-input-step"]')) {
    const inner = step.querySelector(".whitespace-pre-wrap");
    if (inner) addItem("user", cleanText(inner), step, "user-input");
  }

  // 2. THOUGHT / AGENT — all leading-relaxed.select-text blocks
  //    Discriminator: thoughts have "opacity-70" in their classList
  for (const el of chatRoot.querySelectorAll("div.leading-relaxed.select-text")) {
    const text = cleanText(el);
    if (!text) continue;

    const hasOpacity70 = el.classList.contains("opacity-70") ||
                         el.className.includes("opacity-70");

    if (hasOpacity70) {
      addItem("thought", text, el, "thought");
    } else {
      addItem("agent", text, el, "agent");
    }
  }

  // 3. Hidden/collapsed thoughts (max-h-0 opacity-0)
  //    Read via textContent even when visually hidden
  const THOUGHT_RE = /^Thought for \d/i;
  for (const btn of chatRoot.querySelectorAll("button")) {
    const btnText = (btn.innerText || "").trim().replace(/\n/g, " ");
    if (!THOUGHT_RE.test(btnText)) continue;
    const isolate = btn.closest(".isolate");
    if (!isolate) continue;
    const hiddenDiv = isolate.querySelector(".overflow-hidden");
    if (!hiddenDiv) continue;
    // Check if it's actually collapsed (max-h-0 or opacity-0)
    const cs = hiddenDiv.className || "";
    if (cs.includes("max-h-0") || cs.includes("opacity-0")) {
      const text = deepText(hiddenDiv);
      if (text && text.length > 4) {
        addItem("thought", text, hiddenDiv, "thought-hidden");
      }
    }
  }

  // 4. TOOL CALLS — buttons with action labels
  for (const btn of chatRoot.querySelectorAll("button")) {
    const t = (btn.innerText || "").trim().replace(/\n/g, " ");
    if (/^(Explored|Ran|Viewed|Analyzed|Created|Edited|Searched|Generated|Navigat)/i.test(t)) {
      addItem("tool_call", t, btn, "tool-btn", true);
    }
  }

  // Tool detail blocks (items-baseline)
  for (const el of chatRoot.querySelectorAll(".flex.items-baseline")) {
    const t = (el.innerText || "").trim().replace(/\n/g, " ");
    if (/^(Ran |Viewed |Edited )/.test(t)) {
      addItem("tool_call", t, el, "tool-detail");
    }
  }

  // Inline file/path references
  for (const el of chatRoot.querySelectorAll(
    ".inline-flex.break-all.leading-tight.select-text, " +
    ".inline-flex.leading-tight.select-text"
  )) {
    const t = cleanText(el);
    if (t && t.length > 2) addItem("tool_call", t, el, "tool-ref");
  }

  // ── Sort by DOM position ──────────────────────────────────────────────
  // compareDocumentPosition bit 4 = DOCUMENT_POSITION_FOLLOWING
  items.sort((a, b) => {
    if (a.el === b.el) return 0;
    const pos = a.el.compareDocumentPosition(b.el);
    return (pos & Node.DOCUMENT_POSITION_FOLLOWING) ? -1 : 1;
  });

  // Build final results with sequence numbers
  const results = items.map((item, idx) => ({
    role: item.role,
    text: item.text,
    _seq: idx,
    _cls: item._cls,
  }));

  return JSON.stringify(results);
})();
"""


# ---------------------------------------------------------------------------
# CDP Client
# ---------------------------------------------------------------------------
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
            pass

    async def send(self, method: str, params: dict = None) -> dict:
        self._id += 1
        msg_id = self._id
        fut = asyncio.get_event_loop().create_future()
        self._pending[msg_id] = fut
        await self.ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        return await asyncio.wait_for(fut, timeout=10.0)

    async def close(self):
        if self.ws:
            await self.ws.close()


async def find_target(host: str, port: int) -> dict | None:
    url = f"http://{host}:{port}/json"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                targets = await resp.json(content_type=None)
    except Exception:
        return None

    pages = [
        t for t in targets
        if t.get("type") == "page"
        and "webviewkey" not in t.get("url", "")
        and t.get("webSocketDebuggerUrl")
    ]
    if not pages:
        pages = [t for t in targets if t.get("type") in ("page", "other") and t.get("webSocketDebuggerUrl")]
    if not pages:
        return None
    pages.sort(key=lambda t: len(t.get("title", "")), reverse=True)
    return pages[0]


async def eval_js(cdp: CDPClient, js: str) -> str | None:
    try:
        result = await cdp.send("Runtime.evaluate", {
            "expression": js,
            "returnByValue": True,
            "awaitPromise": False,
        })
        return result.get("result", {}).get("result", {}).get("value")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Session Logger — stabilization + incremental writing
# ---------------------------------------------------------------------------
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
        self.stabilize = stabilize
        self.debug = debug
        self._seen: set[str] = set()
        self._turns: list[dict] = []
        self._pending: dict[str, _Pending] = {}
        self._write_header(task, project)
        print(f"[\u2713] Logging to: {self.path}")

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

    @staticmethod
    def _fp(role: str, text: str) -> str:
        """Fingerprint includes role to avoid cross-role dedup collisions."""
        return hashlib.md5(f"{role}:{text}".encode("utf-8")).hexdigest()

    def ingest(self, turns: list[dict]):
        # Skip sentinel for "no chat panel found"
        if len(turns) == 1 and turns[0].get("role") == "_no_chat":
            return

        # Build active fingerprint set AND a map from fp → latest _seq
        active: set[str] = set()
        seq_map: dict[str, int] = {}
        for t in turns:
            text = (t.get("text") or "").strip()
            role = t.get("role", "unknown")
            if text:
                fp = self._fp(role, text)
                active.add(fp)
                seq_map[fp] = t.get("_seq", 0)

        # Purge pending that disappeared (text changed mid-stream)
        for fp in list(self._pending.keys()):
            if fp not in active:
                del self._pending[fp]

        # Update _seq on all already-committed turns to reflect current DOM order.
        # This is essential: as new elements appear in the DOM, positions shift.
        # We must keep committed turns' _seq in sync so newly committed items
        # can be placed correctly relative to them.
        for existing in self._turns:
            et = existing.get("text", "").strip()
            er = existing.get("role", "unknown")
            if et:
                efp = self._fp(er, et)
                if efp in seq_map:
                    existing["_seq"] = seq_map[efp]

        newly_committed = False
        for turn in turns:
            text = (turn.get("text") or "").strip()
            role = turn.get("role", "unknown")
            if not text:
                continue
            fp = self._fp(role, text)
            if fp in self._seen:
                continue

            if fp not in self._pending:
                self._pending[fp] = _Pending(
                    turn={**turn, "captured_at": datetime.now(timezone.utc).isoformat()}
                )
            else:
                # Update _seq to latest DOM position while pending
                self._pending[fp].turn["_seq"] = turn.get("_seq", 0)
                self._pending[fp].count += 1
                if self._pending[fp].count >= self.stabilize:
                    committed = self._pending.pop(fp)
                    self._seen.add(fp)

                    # Superset dedup: replace shorter subset turns
                    replaced = False
                    for i, existing in enumerate(self._turns):
                        et = existing.get("text", "").strip()
                        er = existing.get("role", "")
                        if er == role and et and et in text and et != text:
                            self._turns[i] = committed.turn
                            replaced = True
                            snippet = text[:60].replace("\n", " ")
                            icon = ROLE_ICONS.get(role, "\u2753")
                            print(f"  [\u21ba] {icon} {role.upper()} (replaced): {snippet}\u2026")
                            break

                    if not replaced:
                        # Skip if this text is a subset of an existing turn
                        is_subset = False
                        for existing in self._turns:
                            et = existing.get("text", "").strip()
                            er = existing.get("role", "")
                            if er == role and text in et and text != et:
                                is_subset = True
                                break

                        if not is_subset:
                            self._turns.append(committed.turn)
                            snippet = text[:60].replace("\n", " ")
                            icon = ROLE_ICONS.get(role, "\u2753")
                            print(f"  [+] {icon} {role.upper()}: {snippet}\u2026")

                    newly_committed = True

        if newly_committed:
            self._flush()

    def _flush(self):
        lines: list[str] = []
        # Re-read existing header
        try:
            existing = self.path.read_text(encoding="utf-8")
            # Find the end of the header (after first ---)
            hdr_end = existing.find("---")
            if hdr_end >= 0:
                lines.append(existing[:hdr_end + 3])
                lines.append("")
            else:
                lines.append(existing)
        except Exception:
            lines.append("# Antigravity Session Trace\n\n---\n")

        # Sort turns by sequence number if available
        sorted_turns = sorted(self._turns, key=lambda t: t.get("_seq", 0))

        for t in sorted_turns:
            role = t.get("role", "unknown")
            icon = ROLE_ICONS.get(role, "\u2753")
            label = role.upper().replace("_", " ")
            ts = t.get("captured_at", "")

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

    def finalise(self):
        self._flush()
        print(f"\n[\u2713] Saved {len(self._turns)} turn(s) \u2192 {self.path}")


# ---------------------------------------------------------------------------
# Main worker loop
# ---------------------------------------------------------------------------
async def run_worker(args):
    project = Path(args.project).resolve()
    logger = SessionLogger(project, args.task, stabilize=args.stabilize, debug=args.debug)
    stop_event = asyncio.Event()

    loop = asyncio.get_event_loop()
    loop.add_signal_handler(signal.SIGINT, lambda: (print("\n[*] Stopping\u2026"), stop_event.set()))
    loop.add_signal_handler(signal.SIGTERM, stop_event.set)

    print(f"[*] Connecting to CDP on {CDP_HOST}:{args.port}\u2026")
    target = None
    for attempt in range(30):
        target = await find_target(CDP_HOST, args.port)
        if target:
            break
        if attempt == 0:
            print("    (waiting for Antigravity to start\u2026)")
        await asyncio.sleep(2)

    if not target:
        sys.exit(f"[error] No Antigravity CDP target on port {args.port}.")

    print(f"[*] Attached: {target.get('title', '?')}  ({target.get('url', '?')[:70]})")
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
            await asyncio.wait_for(asyncio.shield(stop_event.wait()), timeout=POLL_INTERVAL)
        except asyncio.TimeoutError:
            pass
        except websockets.exceptions.ConnectionClosed:
            print("\n[*] Browser disconnected. Exiting\u2026")
            break
        except Exception as e:
            if "close frame" in str(e) or "Connection closed" in str(e):
                print("\n[*] Target disconnected. Exiting\u2026")
                break
            consecutive_errors += 1
            if consecutive_errors > 5:
                print(f"[warn] Repeated errors: {e}")
                consecutive_errors = 0
            await asyncio.sleep(1)

    await cdp.close()
    logger.finalise()


# ---------------------------------------------------------------------------
# Tmux orchestration
# ---------------------------------------------------------------------------
def orchestrate_tmux(args):
    import shlex

    session = f"ag-{args.profile}-{args.task}"

    if subprocess.run(["tmux", "has-session", "-t", session], capture_output=True).returncode == 0:
        print(f"[*] Attaching to: {session}")
        os.execvp("tmux", ["tmux", "attach-session", "-t", session])

    prof_dir = PROFILES_BASE / args.profile
    app_config = prof_dir / "app_config"
    browser_profile = prof_dir / "browser_profile"
    project = Path(args.project).resolve()

    browser_cmd = (
        f"HOME='{app_config}' antigravity "
        f"--user-data-dir='{browser_profile}' "
        f"--remote-debugging-port={args.port} "
        f"'{project}'"
    )

    script = Path(__file__).resolve()
    worker_cmd = (
        f"cd {shlex.quote(str(project))} && uv run --script {script} "
        f"--_worker "
        f"{shlex.quote(args.task)} "
        f"{shlex.quote(args.profile)} "
        f"{shlex.quote(str(project))} "
        f"--port {args.port} "
        f"--stabilize {args.stabilize} "
        + ("--debug " if args.debug else "")
    )

    print(f"[*] Starting tmux session: {session}")
    print(f"    left  \u2192 antigravity {args.profile}")
    print(f"    right \u2192 logger")

    subprocess.run(["tmux", "new-session", "-d", "-s", session, "-x", "220", "-y", "50"])
    subprocess.run(["tmux", "send-keys", "-t", f"{session}:0.0", browser_cmd, "C-m"])
    subprocess.run(["tmux", "split-window", "-h", "-t", f"{session}:0.0"])
    subprocess.run(["tmux", "send-keys", "-t", f"{session}:0.1", worker_cmd, "C-m"])
    os.execvp("tmux", ["tmux", "attach-session", "-t", session])


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Antigravity session logger (opus v4)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("task", nargs="?", default="session",
        help="Task label for log filename (default: session)")
    parser.add_argument("profile", nargs="?", default="phoneiep",
        help="Antigravity profile name (default: phoneiep)")
    parser.add_argument("project", nargs="?", default=None,
        help="Project directory (default: cwd)")
    parser.add_argument("--port", type=int, default=CDP_PORT,
        help=f"CDP debug port (default: {CDP_PORT})")
    parser.add_argument("--stabilize", type=int, default=2, metavar="N",
        help="Polls before committing a turn (default: 2)")
    parser.add_argument("--debug", action="store_true",
        help="Include DOM class info in output")
    parser.add_argument("--_worker", action="store_true", help=argparse.SUPPRESS)

    args = parser.parse_args()
    if args.project is None:
        args.project = str(Path.cwd())

    if args._worker:
        asyncio.run(run_worker(args))
    else:
        orchestrate_tmux(args)


if __name__ == "__main__":
    main()
