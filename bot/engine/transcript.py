"""HTML transcript renderer for Claude Code session JSONL files.

Produces a self-contained, styled HTML document suitable for sharing outside
Discord. All text-bearing content flows through `redact_secrets` before being
emitted. Redaction is best-effort on known token shapes — shell commands and
other opaque tool inputs may still leak custom env vars, which is why Bash
tool_input gets a distinct visual accent so reviewers scan it first.

Pure module: no Discord imports, no I/O side effects besides reading the
session file.
"""

from __future__ import annotations

import html
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from bot.platform.formatting import redact_secrets


# --- Noise filter (mirrors sessions._parse_record) ---

def _is_noise_text(text: str) -> bool:
    """Drop Claude Code's internal command-name markers and interrupts."""
    if "<command-name>" in text or "<local-command-" in text:
        return True
    if text.startswith("[Request interrupted"):
        return True
    return False


# --- Markdown → HTML (minimal, HTML-safe) ---

_FENCE_RE = re.compile(r"```(\w+)?\n(.*?)(?:```|\Z)", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_BOLD_RE = re.compile(r"\*\*([^\*\n]+)\*\*")
_ITALIC_RE = re.compile(r"(?<!\*)\*([^\*\n]+)\*(?!\*)")
_PARA_SPLIT_RE = re.compile(r"\n{2,}")
_PLACEHOLDER_SPLIT_RE = re.compile(r"(\x00FENCE\d+\x00)")
_PLACEHOLDER_RE = re.compile(r"\x00FENCE(\d+)\x00")


def _render_markdown(text: str) -> str:
    """Render a subset of markdown to HTML. Input is ALREADY redacted.

    Ordering:
      1. Extract fenced code blocks as placeholders (keep raw, escape later).
      2. HTML-escape the rest of the text.
      3. Apply inline code / bold / italic on the escaped text.
      4. Reinsert fenced blocks as <pre><code> with their content escaped.
      5. Wrap blank-line-separated segments in <p> (except for <pre>).
    """
    placeholders: list[str] = []

    def _stash(m: re.Match) -> str:
        lang = (m.group(1) or "").strip()
        code = m.group(2)
        lang_class = f' class="lang-{html.escape(lang, quote=True)}"' if lang else ""
        stashed = (
            f'<pre><code{lang_class}>{html.escape(code)}</code></pre>'
        )
        placeholders.append(stashed)
        return f"\x00FENCE{len(placeholders) - 1}\x00"

    with_fences = _FENCE_RE.sub(_stash, text)

    # Escape everything else
    escaped = html.escape(with_fences)

    # Apply inline decorations on escaped text — these only insert known tags
    # and never interpolate into attribute positions.
    escaped = _INLINE_CODE_RE.sub(lambda m: f"<code>{m.group(1)}</code>", escaped)
    escaped = _BOLD_RE.sub(lambda m: f"<strong>{m.group(1)}</strong>", escaped)
    escaped = _ITALIC_RE.sub(lambda m: f"<em>{m.group(1)}</em>", escaped)

    # Split into paragraph blocks; each block that isn't a fence placeholder
    # gets wrapped in <p>. Preserve internal single newlines as <br>.
    # Fence placeholders may appear *inside* a paragraph (no blank-line
    # separator around them), and <pre> nested inside <p> is invalid HTML —
    # split each paragraph on inline placeholders so <pre> always stands alone.
    html_parts: list[str] = []
    for part in _PARA_SPLIT_RE.split(escaped):
        part = part.strip("\n")
        if not part:
            continue
        for chunk in _PLACEHOLDER_SPLIT_RE.split(part):
            if not chunk:
                continue
            if chunk.startswith("\x00FENCE"):
                html_parts.append(chunk)
            else:
                stripped = chunk.strip("\n")
                if not stripped:
                    continue
                html_parts.append("<p>" + stripped.replace("\n", "<br>") + "</p>")

    out = "\n".join(html_parts)

    # Reinsert fenced code placeholders. Bounds-check in case the source text
    # contained a literal \x00FENCE<n>\x00 sequence that survived escaping —
    # without this, a crafted out-of-range index would IndexError.
    def _unstash(m: re.Match) -> str:
        idx = int(m.group(1))
        if 0 <= idx < len(placeholders):
            return placeholders[idx]
        return m.group(0)

    return _PLACEHOLDER_RE.sub(_unstash, out)


# --- JSONL walking ---

def _iter_jsonl(path: Path):
    """Yield parsed JSON objects, skipping unreadable lines."""
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _collect_tool_results(session_file: Path) -> dict[str, str]:
    """First pass: map tool_use_id → concatenated tool_result text.

    Tool results arrive in subsequent `user` records with content blocks of
    type `tool_result`. We index by `tool_use_id` so the second pass can
    attach them to their corresponding tool_use.
    """
    out: dict[str, str] = {}
    for rec in _iter_jsonl(session_file):
        if rec.get("type") != "user":
            continue
        msg = rec.get("message", {})
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "tool_result":
                continue
            tid = block.get("tool_use_id")
            if not tid:
                continue
            raw = block.get("content", "")
            if isinstance(raw, list):
                texts = [
                    b.get("text", "")
                    for b in raw
                    if isinstance(b, dict) and b.get("type") == "text"
                ]
                raw = "\n".join(t for t in texts if t)
            if not isinstance(raw, str):
                raw = str(raw)
            out[tid] = raw
    return out


# --- Tool summary helpers ---

def _tool_summary(name: str, tool_input: dict) -> str:
    """One-line summary for a tool_use collapsible heading."""
    if not isinstance(tool_input, dict):
        return name
    # Pick the most informative single field per common tool
    for key in ("file_path", "path", "pattern", "command", "url", "query", "description"):
        val = tool_input.get(key)
        if isinstance(val, str) and val.strip():
            short = val.strip().splitlines()[0]
            if len(short) > 80:
                short = short[:77] + "..."
            return f"{name} · {short}"
    return name


def _format_tool_input(tool_input: dict) -> str:
    """Pretty-print tool input as JSON (redacted)."""
    try:
        pretty = json.dumps(tool_input, indent=2, ensure_ascii=False)
    except (TypeError, ValueError):
        pretty = str(tool_input)
    return redact_secrets(pretty)


# --- HTML assembly ---

_CSS = """
:root {
  color-scheme: light dark;
  --bg: #ffffff;
  --fg: #1a1a1a;
  --muted: #6b7280;
  --border: #e5e7eb;
  --user-bg: #eef2ff;
  --user-border: #6366f1;
  --code-bg: #f3f4f6;
  --thinking-bg: #faf5ff;
  --thinking-border: #a855f7;
  --tool-bg: #f9fafb;
  --tool-border: #9ca3af;
  --bash-bg: #fffbeb;
  --bash-border: #f59e0b;
  --notice-bg: #fef3c7;
  --notice-fg: #78350f;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0f1115;
    --fg: #e5e7eb;
    --muted: #9ca3af;
    --border: #272a31;
    --user-bg: #1e1b4b;
    --user-border: #818cf8;
    --code-bg: #1f2937;
    --thinking-bg: #2a1a3a;
    --thinking-border: #c084fc;
    --tool-bg: #1a1d23;
    --tool-border: #6b7280;
    --bash-bg: #2a1f05;
    --bash-border: #fbbf24;
    --notice-bg: #3a2807;
    --notice-fg: #fde68a;
  }
}
* { box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  background: var(--bg);
  color: var(--fg);
  max-width: 820px;
  margin: 0 auto;
  padding: 24px 16px 64px;
  line-height: 1.5;
  font-size: 15px;
}
header { border-bottom: 1px solid var(--border); padding-bottom: 16px; margin-bottom: 24px; }
header h1 { margin: 0 0 6px; font-size: 22px; }
.meta { color: var(--muted); font-size: 13px; margin-bottom: 12px; }
.meta span + span::before { content: " · "; }
.prompt {
  background: var(--code-bg); border-left: 3px solid var(--muted);
  padding: 10px 14px; border-radius: 4px; font-size: 14px;
  white-space: pre-wrap;
}
.redaction-notice {
  background: var(--notice-bg); color: var(--notice-fg);
  border-radius: 6px; padding: 10px 14px; font-size: 13px; margin-bottom: 20px;
}
.turn { margin: 18px 0; }
.turn.user {
  background: var(--user-bg);
  border-left: 3px solid var(--user-border);
  padding: 10px 14px; border-radius: 4px;
}
.turn.user .role,
.turn.assistant .role {
  font-size: 12px; text-transform: uppercase; letter-spacing: 0.06em;
  color: var(--muted); margin-bottom: 4px;
}
.turn.assistant { border-top: 1px dashed var(--border); padding-top: 14px; }
.text p { margin: 8px 0; }
.text p:first-child { margin-top: 0; }
.text p:last-child { margin-bottom: 0; }
code {
  background: var(--code-bg); padding: 1px 5px; border-radius: 3px;
  font-family: ui-monospace, "Cascadia Mono", Menlo, Consolas, monospace;
  font-size: 0.92em;
}
pre {
  background: var(--code-bg); padding: 10px 12px; border-radius: 4px;
  overflow-x: auto; white-space: pre-wrap; word-break: break-word;
  font-family: ui-monospace, "Cascadia Mono", Menlo, Consolas, monospace;
  font-size: 13px;
}
pre code { background: transparent; padding: 0; font-size: inherit; }
details {
  margin: 10px 0; border-radius: 4px; padding: 6px 10px;
  background: var(--tool-bg); border-left: 3px solid var(--tool-border);
}
details.thinking {
  background: var(--thinking-bg); border-left-color: var(--thinking-border);
}
details.tool.bash {
  background: var(--bash-bg); border-left-color: var(--bash-border);
}
details > summary {
  cursor: pointer; font-size: 13px; color: var(--muted);
  padding: 2px 0; user-select: none;
}
details[open] > summary { margin-bottom: 8px; }
details pre { margin: 6px 0; }
.tool-label {
  display: inline-block; font-size: 11px; text-transform: uppercase;
  color: var(--muted); letter-spacing: 0.06em; margin: 8px 0 4px;
}
footer {
  margin-top: 48px; padding-top: 16px; border-top: 1px solid var(--border);
  color: var(--muted); font-size: 12px;
}
"""


def _render_assistant_content(content: list, tool_results: dict[str, str]) -> list[str]:
    """Render an assistant message's content blocks as HTML fragments."""
    out: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")

        if btype == "text":
            raw = block.get("text", "")
            if not isinstance(raw, str) or not raw.strip():
                continue
            if _is_noise_text(raw):
                continue
            redacted = redact_secrets(raw)
            out.append(f'<div class="text">{_render_markdown(redacted)}</div>')

        elif btype == "thinking":
            raw = block.get("thinking") or block.get("text", "")
            if not isinstance(raw, str) or not raw.strip():
                continue
            redacted = redact_secrets(raw)
            out.append(
                '<details class="thinking">'
                '<summary>\U0001f4ad Thinking</summary>'
                f'<div class="text">{_render_markdown(redacted)}</div>'
                '</details>'
            )

        elif btype == "tool_use":
            name = block.get("name", "tool")
            tid = block.get("id", "")
            tinput = block.get("input", {}) or {}
            # Redact before the summary hits <summary> — _tool_summary pulls
            # raw values straight from tool_input (e.g. curl -H "Bearer ...")
            # so the string must pass through redact_secrets before escape.
            summary = redact_secrets(_tool_summary(name, tinput))
            extra_cls = " bash" if name == "Bash" else ""
            pretty_input = _format_tool_input(tinput)
            result_html = ""
            if tid and tid in tool_results:
                raw_result = tool_results.get(tid, "")
                redacted_result = redact_secrets(raw_result)
                # Tool results are often raw text/code — render as <pre>
                result_html = (
                    '<span class="tool-label">Result</span>'
                    f'<pre>{html.escape(redacted_result)}</pre>'
                )
            out.append(
                f'<details class="tool{extra_cls}">'
                f'<summary>\U0001f527 {html.escape(summary)}</summary>'
                '<span class="tool-label">Input</span>'
                f'<pre>{html.escape(pretty_input)}</pre>'
                f'{result_html}'
                '</details>'
            )
    return out


def _render_user_content(content) -> str | None:
    """Render a user turn. Returns None if the turn is noise/tool-result-only."""
    if isinstance(content, str):
        text = content.strip()
        if not text or _is_noise_text(text):
            return None
        body = text
    elif isinstance(content, list):
        # User records with content arrays are typically tool_result deliveries;
        # the tool_result text is attached under its tool_use in the assistant
        # turn, so we only surface plain text blocks here.
        texts = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        texts = [t for t in texts if isinstance(t, str) and t.strip() and not _is_noise_text(t)]
        if not texts:
            return None
        body = "\n\n".join(texts)
    else:
        return None

    redacted = redact_secrets(body)
    return (
        '<article class="turn user">'
        '<div class="role">User</div>'
        f'<div class="text">{_render_markdown(redacted)}</div>'
        '</article>'
    )


def _fmt_cost(cost: float | None) -> str:
    if cost is None:
        return ""
    return f"${cost:.4f}"


def _fmt_duration_ms(ms: int | float | None) -> str:
    if not ms:
        return ""
    secs = ms / 1000
    if secs >= 60:
        return f"{secs / 60:.1f}m"
    return f"{secs:.0f}s"


def render_transcript_html(
    session_file: Path,
    *,
    title: str,
    instance_summary: dict | None = None,
) -> str:
    """Render a session JSONL as a self-contained HTML document.

    `instance_summary` may include: session_id, prompt, repo, mode, effort,
    cost_usd, duration_ms, num_turns.
    """
    tool_results = _collect_tool_results(session_file)

    body_parts: list[str] = []
    turn_count = 0
    final_result: dict | None = None

    for rec in _iter_jsonl(session_file):
        rtype = rec.get("type")
        if rtype == "user":
            msg = rec.get("message", {})
            rendered = _render_user_content(msg.get("content"))
            if rendered:
                body_parts.append(rendered)
                turn_count += 1
        elif rtype == "assistant":
            msg = rec.get("message", {})
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            fragments = _render_assistant_content(content, tool_results)
            if not fragments:
                continue
            body_parts.append(
                '<article class="turn assistant">'
                '<div class="role">Assistant</div>'
                + "\n".join(fragments)
                + '</article>'
            )
            turn_count += 1
        elif rtype == "result":
            final_result = rec

    # --- Header metadata ---
    summary = instance_summary or {}
    meta_bits: list[str] = []
    if summary.get("repo"):
        meta_bits.append(html.escape(str(summary["repo"])))
    if summary.get("mode"):
        meta_bits.append(f"Mode: {html.escape(str(summary['mode']))}")
    if summary.get("effort"):
        meta_bits.append(f"Effort: {html.escape(str(summary['effort']))}")
    if summary.get("session_id"):
        meta_bits.append(f"Session: {html.escape(str(summary['session_id']))}")
    meta_bits.append(datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
    meta_html = "".join(f"<span>{bit}</span>" for bit in meta_bits)

    prompt_html = ""
    if summary.get("prompt"):
        prompt_redacted = redact_secrets(str(summary["prompt"]))
        prompt_html = f'<div class="prompt">{html.escape(prompt_redacted)}</div>'

    # --- Footer ---
    footer_bits: list[str] = []
    cost = None
    duration_ms = None
    turns_num = turn_count
    if final_result:
        cost = final_result.get("total_cost_usd") or final_result.get("cost_usd")
        duration_ms = final_result.get("duration_ms")
        turns_num = final_result.get("num_turns") or turn_count
    elif summary:
        cost = summary.get("cost_usd")
        duration_ms = summary.get("duration_ms")
        if summary.get("num_turns"):
            turns_num = summary.get("num_turns")

    if turns_num:
        footer_bits.append(f"{turns_num} turns")
    dur = _fmt_duration_ms(duration_ms)
    if dur:
        footer_bits.append(dur)
    cost_str = _fmt_cost(cost)
    if cost_str:
        footer_bits.append(cost_str)
    footer_bits.append("Generated by Claude Code Bot")
    footer_html = " · ".join(footer_bits)

    title_esc = html.escape(title)

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title_esc}</title>
<style>{_CSS}</style>
</head>
<body>
<header>
<h1>{title_esc}</h1>
<div class="meta">{meta_html}</div>
{prompt_html}
</header>
<div class="redaction-notice">
⚠ Secrets auto-redacted on known token patterns (API keys, JWTs, bearer tokens, connection strings).
Shell commands and custom env vars are best-effort &mdash; review before sharing externally.
</div>
<main>
{"".join(body_parts)}
</main>
<footer>{footer_html}</footer>
</body>
</html>
"""
