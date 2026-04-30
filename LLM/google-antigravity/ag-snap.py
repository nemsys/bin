#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["websockets>=12.0", "aiohttp>=3.9"]
# ///
"""
ag-snap: Antigravity one-shot session capture.

Takes a single DOM snapshot via CDP and writes a trace file.
No polling, no stabilization — run this when the conversation is done.

Usage:
  uv run --script ag-snap.py [task] [project_dir] [--port N]

Examples:
  uv run --script ag-snap.py                          # task=session, project=cwd
  uv run --script ag-snap.py refactor-sum             # named task
  uv run --script ag-snap.py refactor-sum /data/projects/myproject

Flags:
  --port N      CDP debug port (default: 9222)
  --out DIR     Output directory (default: <project>/.agents/log/)
  --stdout      Print trace to stdout instead of writing a file

Typical workflow:
  1. Open Antigravity with --remote-debugging-port=9222
  2. Have your conversation
  3. Run ag-snap (before closing or starting a new conversation)
"""

import asyncio
import aiohttp
import websockets
import json
import sys
import argparse
from pathlib import Path
from datetime import datetime, timezone

CDP_HOST = "localhost"
CDP_PORT = 9222

ROLE_ICONS = {
    "user":      "\U0001f464",
    "thought":   "\U0001f9e0",
    "tool_call": "\U0001f527",
    "agent":     "\U0001f916",
    "unknown":   "\u2753",
}

# ---------------------------------------------------------------------------
# JavaScript — identical extraction logic, returns turns sorted by DOM order
# ---------------------------------------------------------------------------
EXTRACT_JS = r"""
(function () {
  "use strict";
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

    function cleanText(el) {
      const clone = el.cloneNode(true);
      clone.querySelectorAll("style, script, link, button, .copy-button").forEach(t => t.remove());
      let txt = (clone.innerText || clone.textContent || "").trim();
      txt = txt.replace(/^(bash|python|terminal|copy|content_copy|alternate_email|check)\s+/i, "");
      txt = txt.replace(/\s+(copy|content_copy|alternate_email|check)$/i, "");
      txt = txt.replace(/\/\*[\s\S]*?\*\//g, "");
      txt = txt.replace(/@media\s*\([^)]*\)\s*\{[\s\S]*?\}\s*\}/g, "");
      txt = txt.replace(/\.[a-zA-Z_-]+\s*\{[^}]*\}/g, "");
      txt = txt.replace(/div:has\([^)]*\)[\s\S]*?(?=\n\n|\n[A-Z]|$)/g, "");
      return txt.trim();
    }

    function deepText(el) {
      const clone = el.cloneNode(true);
      clone.querySelectorAll("style, script, link").forEach(t => t.remove());
      let txt = (clone.textContent || "").trim();
      txt = txt.replace(/\/\*[\s\S]*?\*\//g, "");
      txt = txt.replace(/@media\s*\([^)]*\)\s*\{[\s\S]*?\}\s*\}/g, "");
      txt = txt.replace(/\.[a-zA-Z_-]+\s*\{[^}]*\}/g, "");
      return txt.trim();
    }

    function findTimestamp(el) {
      let p = el.closest('[data-testid*="step"], .isolate, .flex-row, .flex-col, [class*="Turn"]');
      if (!p) p = el.parentElement;
      if (p.hasAttribute('data-timestamp')) return p.getAttribute('data-timestamp');
      const timeEl = p.querySelector('time, [class*="time"], .text-xs, .opacity-50');
      if (timeEl) {
        const t = (timeEl.getAttribute('datetime') || timeEl.innerText || "").trim();
        if (t && t.length < 40) return t;
      }
      return null;
    }

    const items = [];
    const seenFp = new Set();

    function addItem(role, text, el, skipNoise) {
      text = text.replace(/alternate_email\s*content_copy/g, "").trim();
      text = text.replace(/chevron_right/g, "").trim();
      const fp = role + ":" + text.slice(0, 300);
      if (seenFp.has(fp)) return;
      seenFp.add(fp);
      items.push({ el, role, text, timestamp: findTimestamp(el) });
    }

    const NOISE_EXACT = new Set(["Plan", "Send", "mic", "Review Changes", "Accept all", "Reject all", "See all", "Attach", "alternate_email", "content_copy", "python", "text", "Markdown", "Go Live", "Antigravity - Settings", "chevron_right", "Drag a view here to display.", "add", "more_vert", "check"]);
    const NOISE_RE = [/^Ask anything[,.].*@ to mention/is, /^AI may make mistakes/i, /^Gemini \d/i, /^Claude /i, /^(Worked|Thought) for \d/i, /^\d+ Files? With Changes\s*$/, /^[+-]\d+\s*$/, /^\+\d+ -\d+$/, /^Press desired key/i, /^[A-Z][a-zA-Z ]+\n\d+[hm]\s*$/, /^F-\d+\s+LF\s+/, /^AG:\s*\d+-\d+%/, /^\w[\w.-]*\.(py|js|ts|md|json|yaml|sh|txt|css)\s*$/, /^\w[\w.-]*\.(py|js|ts|md|json|yaml|sh|txt|css)\s+\+\d+/, /^border-/, /^margin-/, /^padding-/, /^\{[\s\S]*:\s*[\s\S]*\}/, /^alternate_email\s*content_copy/];
    function isNoise(text, minLen) {
      if (!text || text.length < (minLen || 2)) return true;
      if (NOISE_EXACT.has(text)) return true;
      if (NOISE_RE.some(re => re.test(text))) return true;
      if (/\{[^}]*(?:border|margin|padding|color|font|display)\s*:/i.test(text)) return true;
      return false;
    }
    const originalAddItem = addItem;
    addItem = function(role, text, el, skipNoise) {
      if (!skipNoise && isNoise(text, role === "user" ? 1 : 4)) return;
      originalAddItem(role, text, el, skipNoise);
    };

    for (const step of chatRoot.querySelectorAll('[data-testid="user-input-step"]')) {
      const inner = step.querySelector(".whitespace-pre-wrap");
      if (inner) addItem("user", cleanText(inner), step);
    }
    for (const el of chatRoot.querySelectorAll("div.leading-relaxed.select-text")) {
      const text = cleanText(el);
      if (!text) continue;
      const isThought = el.classList.contains("opacity-70") || el.className.includes("opacity-70");
      addItem(isThought ? "thought" : "agent", text, el);
    }
    const THOUGHT_RE = /^Thought for \d/i;
    for (const btn of chatRoot.querySelectorAll("button")) {
      const btnText = (btn.innerText || "").trim().replace(/\n/g, " ");
      if (!THOUGHT_RE.test(btnText)) continue;
      const isolate = btn.closest(".isolate");
      if (!isolate) continue;
      const hiddenDiv = isolate.querySelector(".overflow-hidden");
      if (!hiddenDiv) continue;
      const cs = hiddenDiv.className || "";
      if (cs.includes("max-h-0") || cs.includes("opacity-0")) {
        const text = deepText(hiddenDiv);
        if (text && text.length > 4) addItem("thought", text, hiddenDiv);
      }
    }
    for (const btn of chatRoot.querySelectorAll("button")) {
      const t = (btn.innerText || "").trim().replace(/\n/g, " ");
      if (/^(Explored|Ran|Viewed|Analyzed|Created|Edited|Searched|Generated|Navigat)/i.test(t)) {
        let detail = t;
        let next = btn.nextElementSibling || btn.parentElement.nextElementSibling;
        const code = (next && next.querySelector('pre, code, .font-mono')) || btn.parentElement.querySelector('pre, code, .font-mono');
        if (code) {
          const fullText = cleanText(code);
          if (fullText && fullText.length > 5) detail = t + ":\n" + fullText;
        }
        addItem("tool_call", detail, btn, true);
      }
    }
    for (const el of chatRoot.querySelectorAll(".flex.items-baseline")) {
      const t = (el.innerText || "").trim().replace(/\n/g, " ");
      if (/^(Ran |Viewed |Edited )/.test(t)) addItem("tool_call", t, el, true);
    }
    for (const el of chatRoot.querySelectorAll(".inline-flex.break-all.leading-tight.select-text, .inline-flex.leading-tight.select-text")) {
      const t = cleanText(el);
      if (t && t.length > 2 && (t.includes("/") || t.includes("."))) addItem("tool_call", t, el, true);
    }
    items.sort((a, b) => {
      if (a.el === b.el) return 0;
      return (a.el.compareDocumentPosition(b.el) & Node.DOCUMENT_POSITION_FOLLOWING) ? -1 : 1;
    });
    return JSON.stringify(items.map((item, idx) => ({ role: item.role, text: item.text, timestamp: item.timestamp, seq: idx })));
})();
"""


# ---------------------------------------------------------------------------
# CDP helpers (minimal)
# ---------------------------------------------------------------------------
class CDPClient:
    def __init__(self, ws_url: str):
        self.ws_url = ws_url
        self.ws = None
        self._id = 0
        self._pending: dict = {}

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
    pages = [t for t in targets
             if t.get("type") == "page"
             and "webviewkey" not in t.get("url", "")
             and t.get("webSocketDebuggerUrl")]
    if not pages:
        pages = [t for t in targets
                 if t.get("type") in ("page", "other") and t.get("webSocketDebuggerUrl")]
    if not pages:
        return None
    pages.sort(key=lambda t: len(t.get("title", "")), reverse=True)
    return pages[0]


async def eval_js(cdp: CDPClient, js: str) -> str | None:
    result = await cdp.send("Runtime.evaluate", {
        "expression": js,
        "returnByValue": True,
        "awaitPromise": False,
    })
    return result.get("result", {}).get("result", {}).get("value")


# ---------------------------------------------------------------------------
# Render trace
# ---------------------------------------------------------------------------
def render_trace(turns: list[dict], task: str, project: Path) -> str:
    now_global = datetime.now(timezone.utc).isoformat()
    lines = [
        "# Antigravity Session Trace",
        f"**Captured:** {now_global}",
        f"**Project:** `{project.resolve()}`",
        f"**Task:** {task}",
        "",
        "---",
        "",
    ]
    for t in turns:
        role = t.get("role", "unknown")
        icon = ROLE_ICONS.get(role, "\u2753")
        label = role.upper().replace("_", " ")
        ts = t.get("timestamp") or now_global
        lines.append(f"### {icon} {label}  `{ts}`")
        lines.append("")
        lines.append(t["text"].strip())
        lines.append("")
        lines.append("---")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def snap(args):
    project = Path(args.project).resolve()

    print(f"[*] Connecting to CDP on {CDP_HOST}:{args.port}…")
    target = await find_target(CDP_HOST, args.port)
    if not target:
        sys.exit(f"[error] No Antigravity CDP target on port {args.port}.\n"
                 f"        Make sure Antigravity is running with "
                 f"--remote-debugging-port={args.port}")

    print(f"[*] Target: {target.get('title', '?')}  ({target.get('url', '?')[:70]})")

    cdp = CDPClient(target["webSocketDebuggerUrl"])
    await cdp.connect()
    await cdp.send("Runtime.enable")

    raw = await eval_js(cdp, EXTRACT_JS)
    await cdp.close()

    if not raw:
        sys.exit("[error] JS extraction returned nothing.")

    turns = json.loads(raw)

    if len(turns) == 1 and turns[0].get("role") == "_no_chat":
        sys.exit("[error] Chat panel not found — is a conversation open in Antigravity?")

    print(f"[*] Captured {len(turns)} turn(s) in DOM order.")

    trace = render_trace(turns, args.task, project)

    if args.stdout:
        print("\n" + trace)
        return

    if args.out:
        out_dir = Path(args.out)
    else:
        out_dir = project / ".agents" / "log"
    out_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / f"{ts}_{args.task}_trace.md"
    out_path.write_text(trace, encoding="utf-8")
    print(f"[\u2713] Saved → {out_path}")


def main():
    parser = argparse.ArgumentParser(
        description="ag-snap: one-shot Antigravity conversation capture",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("task", nargs="?", default="session",
        help="Label for the log filename (default: session)")
    parser.add_argument("project", nargs="?", default=None,
        help="Project directory (default: cwd)")
    parser.add_argument("--port", type=int, default=CDP_PORT,
        help=f"CDP debug port (default: {CDP_PORT})")
    parser.add_argument("--out", metavar="DIR", default=None,
        help="Output directory (default: <project>/.agents/log/)")
    parser.add_argument("--stdout", action="store_true",
        help="Print trace to stdout instead of writing a file")

    args = parser.parse_args()
    if args.project is None:
        args.project = str(Path.cwd())

    asyncio.run(snap(args))


if __name__ == "__main__":
    main()