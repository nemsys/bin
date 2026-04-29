# ag-logger-opus.py — Reliable Antigravity Chat Logger

## Problem Analysis

Both existing scripts use CDP (Chrome DevTools Protocol) to inject JavaScript into Antigravity's Electron renderer and poll the DOM for chat content. They both fail in different ways:

### ag-logger-claude.py Issues
1. **CSS noise leaking into output** — The `cleanText()` helper strips `<style>` tags, but the `leading-relaxed` divs contain an embedded `<style>` block for markdown-alert CSS. The regex `NOISE_RE` tries to filter it (`/^\/\*.*?alert\.css/s`) but the text still bleeds into output (see session1_trace.md lines 49–161 — a full CSS dump got captured as an "AGENT" turn).
2. **Role misclassification** — Agent responses and thoughts both come from `div.leading-relaxed.select-text`. The heuristic of walking ancestors to find "Thought for" vs "Worked for" buttons is fragile. In session1_trace the greeting "Hello! How can I help you today?" was classified as `THOUGHT` instead of `AGENT`.
3. **Duplicate content** — The superset dedup catches some cases but the same thought text ("Prioritizing Tool Usage...") appears multiple times across turns.
4. **Tool calls barely captured** — Only inline file references are detected. Actual tool invocations (file views, command runs, grep searches) with their parameters and results are not captured.

### ag-logger-gemini.py Issues
1. **Falls back to `raw_page`** — Session3 shows it captured the Agent Manager project-picker page instead of the chat panel, producing a useless `raw_page` dump. The JS has no `findChatRoot()` logic to scope to the chat panel.
2. **Flat element selection** — It queries all `button`, `.select-text.leading-relaxed` etc. without scoping to the chat panel, so it picks up random UI chrome.
3. **Opacity-based thought detection is wrong** — `el.className.includes("opacity-70")` is not a reliable signal. The real thought content is nested inside collapsible sections preceded by "Thought for Xs" buttons.
4. **All timestamps identical** — In session4_trace.md every entry has the exact same `captured_at` timestamp (`21:11:50.557xxx`), which means all turns stabilized in the same poll cycle. This happens because the stabilization logic doesn't preserve capture order — it just commits everything at once.

### Common Issues
- Neither script properly handles the case where the chat panel hasn't opened yet (landing on the project picker page).
- Neither script captures **tool call details** (tool name, parameters, output) — only vague summaries.
- The "Worked for Xs" / "Thought for Xs" collapsible sections need to be **expanded** (clicked) before their content is visible in the DOM.

## Proposed Changes

### [NEW] [ag-logger-opus.py](file:///data/bin/LLM/google-antigravity/ag-logger-opus.py)

A new unified script that addresses all the above issues. Key design decisions:

#### 1. Robust Chat Panel Detection
- Use the same `findChatRoot()` approach from the Claude script but with a **pre-check**: if the page is on the Agent Manager / project picker (detected by checking for "Agent Manager" text or missing chat input), **skip that poll** rather than capturing garbage.

#### 2. Force-Expand Collapsibles Before Extraction
- Before extracting text, inject JS that clicks all collapsed "Thought for Xs" and "Worked for Xs" buttons to reveal hidden content. This ensures thoughts and tool details are in the DOM.
- Use `el.getAttribute('aria-expanded') === 'false'` or check for hidden overflow containers.

#### 3. Improved Role Classification
- **USER**: `[data-testid="user-input-step"] .whitespace-pre-wrap` — same as both scripts, this works.
- **THOUGHT**: Content inside a container whose **preceding sibling** is a button matching `/^Thought for \d/i`. Walk the DOM tree structurally.
- **TOOL_CALL**: Content inside containers preceded by tool summary labels (buttons/divs with text like "Explored N folders", "Ran command", "Analyzed /path", etc.). Also capture inline tool reference pills (`.inline-flex.break-all`).
- **AGENT**: Any `div.leading-relaxed.select-text` that is NOT inside a thought or tool collapsible section. These are the final agent response blocks.

#### 4. Aggressive Noise Filtering
- Strip embedded `<style>` and `<script>` tags from cloned nodes before reading text.
- Filter known noise patterns (CSS blocks, UI chrome text, status bar text).
- Reject any text that starts with `/*` or contains CSS property patterns like `border-left:`.

#### 5. Ordered, Incremental Logging
- Use DOM order (element position) to determine turn sequence, not capture time.
- Assign a **sequence number** to each turn based on its position in the DOM.
- Only commit a turn after it's been stable for N polls (same stabilization logic).
- Append new turns incrementally rather than rewriting the whole file each time.

#### 6. Better Deduplication
- Fingerprint on `(role, text_hash)` pairs.
- If a new turn's text is a **superset** of an existing committed turn (same role), replace the shorter one.
- If text is a **subset** of an existing committed turn, skip it.

#### 7. Tmux Orchestration (preserved)
- Keep the same tmux launch/attach pattern from both scripts.
- Profile configuration for Antigravity instances.

### Structure Overview

```
ag-logger-opus.py
├── EXTRACT_JS          — Injected JS: expand collapsibles, extract turns
├── CDPClient           — WebSocket CDP client (unchanged from existing)
├── SessionLogger       — Stabilization + incremental file writing
├── orchestrate_tmux()  — tmux session management
├── run_worker()        — Main CDP poll loop
└── main()              — CLI entry point
```

## Open Questions

> [!IMPORTANT]
> **Collapsible expansion**: Clicking "Thought for Xs" buttons programmatically will change the visible UI for the user. Is this acceptable? An alternative is to read the DOM's hidden content directly via `el.textContent` on overflow-hidden containers, which avoids visual side effects but may miss content that's lazy-loaded.

> [!NOTE]
> **Tool call detail level**: The current scripts only capture tool *summaries* ("Explored 2 folders", "Analyzed /path"). The actual tool parameters and results (e.g., the file contents viewed, grep results) are inside further-nested collapsible sections. Do you want to capture those detailed results too, or just the summary labels?

## Verification Plan

### Manual Testing
1. Launch with `uv run --script ag-logger-opus.py session_test phoneiep /home/nemsys/projects/tmp/ag-extractor-test`
2. In the Antigravity chat, send a few messages, trigger tool calls (e.g., "list files in current directory"), wait for responses
3. Check `.agents/log/*_trace.md` for:
   - All user messages captured
   - Agent responses captured (not thoughts)
   - Thoughts captured separately with correct role
   - Tool calls captured with labels
   - No CSS/noise contamination
   - No duplicate entries
   - Correct temporal ordering
