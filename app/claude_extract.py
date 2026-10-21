"""Claude cleanup pass: raw page/PDF (+ trafilatura draft) → clean reading markdown.

Site layouts vary too much for heuristics alone (nav, subscribe boxes, captions,
footnote markers, comment threads), and PDFs need real reading. So after the
cheap trafilatura draft, we hand the saved source to headless Claude Code
(`claude -p`) in a scratch dir and ask it to write `clean.md`.

Claude gets Read/Write only — no web tools. WebFetch returns a model summary,
not the full text, so BacklogCast always fetches the bytes itself first.
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import config

log = logging.getLogger("backlogcast.claude")

NOT_ARTICLE = "NOT_ARTICLE"

INSTRUCTIONS = """\
# Task: extract the reading material

This folder holds one fetched document that will be narrated as a podcast episode:

- `{source}` — the original {kind_desc}
- `draft.md` — an automatic extraction ({draft_desc})

Read `{source}` (with a plain Read — no page ranges), use `draft.md` as a head start where it is right, and
write `clean.md` in this folder containing ONLY the reading material, as markdown:

- First line: `# <title of the piece>`. Keep the author's section headings as `##`.
- Keep every paragraph of the body text in order, verbatim. Do not summarize,
  shorten, paraphrase, or add commentary.
- Remove everything that is not the piece itself: navigation, bylines/dates
  boilerplate, subscribe/share/like prompts, "related posts", image captions,
  photo credits, ads, cookie notices, comment sections, author bios, and
  footnote/citation markers inside sentences (e.g. `[12]`, superscript numbers).
- Drop the footnotes/endnotes/bibliography section at the end, and any
  table of contents.
- Numbered paragraphs (e.g. "1. ...") may keep their numbers.
- No links, images, tables, or HTML — plain prose paragraphs separated by
  blank lines.

If the document is NOT readable material — a paywall or login wall showing
only a teaser, an error page, a listing/index page — write `clean.md` with a
single line `NOT_ARTICLE: <short reason>` instead.

Write `clean.md` once with the Write tool, then reply with one short sentence.
"""


@dataclass
class ClaudeResult:
    markdown: str
    verdict: str          # "ok" | "not_article" | "suspect"
    reason: str = ""
    cost_usd: float = 0.0


class ClaudeExtractError(RuntimeError):
    pass


def claude_bin() -> str | None:
    return config.claude_bin() or shutil.which("claude")


def clean_with_claude(
    source: Path, kind: str, draft_md: str, timeout: int = 900,
) -> ClaudeResult:
    """Run the cleanup in a temp dir (outside the repo, so no CLAUDE.md gets
    loaded). `source` is raw.html or source.pdf. Raises ClaudeExtractError."""
    exe = claude_bin()
    if not exe:
        raise ClaudeExtractError("claude CLI not found (set CLAUDE_BIN)")
    with tempfile.TemporaryDirectory(prefix="blc-claude-") as td:
        work = Path(td)
        src_name = "source.pdf" if kind == "pdf" else "raw.html"
        shutil.copyfile(source, work / src_name)
        (work / "draft.md").write_text(draft_md or "", encoding="utf-8")
        (work / "instructions.md").write_text(INSTRUCTIONS.format(
            source=src_name,
            kind_desc="PDF" if kind == "pdf" else "web page HTML",
            draft_desc="empty — none for PDFs" if not draft_md.strip()
                       else "may include junk or miss parts",
        ), encoding="utf-8")
        cmd = [
            exe, "-p", "Follow the instructions in instructions.md exactly.",
            "--output-format", "json",
            "--model", config.claude_model(),
            "--allowedTools", "Read,Write",
            "--disallowedTools", "Bash,WebFetch,WebSearch,Edit",
            "--permission-mode", "acceptEdits",
            "--strict-mcp-config",
        ]
        clean = work / "clean.md"
        cost, reply = 0.0, ""
        # One retry when Claude finishes without writing clean.md (it can get
        # sidetracked by a failed tool call); errors and timeouts aren't retried.
        for _attempt in range(2):
            try:
                proc = subprocess.run(
                    cmd, cwd=work, capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=timeout,
                )
            except subprocess.TimeoutExpired:
                raise ClaudeExtractError(f"claude timed out after {timeout}s")
            try:
                out = json.loads(proc.stdout or "{}")
                cost += float(out.get("total_cost_usd") or 0.0)
                reply = str(out.get("result") or "")
                if out.get("is_error"):
                    raise ClaudeExtractError(f"claude error: {reply[:300]}")
            except ValueError:
                pass
            if proc.returncode != 0:
                raise ClaudeExtractError(
                    f"claude exited {proc.returncode}: {(proc.stderr or proc.stdout)[-300:]}")
            if clean.exists():
                break
        else:
            raise ClaudeExtractError(f"claude did not write clean.md: {reply[:300]}")
        md = clean.read_text(encoding="utf-8").strip()

    if not md:
        raise ClaudeExtractError("clean.md is empty")
    if md.startswith(NOT_ARTICLE):
        return ClaudeResult(md, "not_article", md.split(":", 1)[-1].strip(), cost)
    verdict, reason = "ok", ""
    if kind == "html" and len(draft_md.strip()) > 1500:
        ratio = len(md) / len(draft_md.strip())
        if ratio < 0.4 or ratio > 1.5:
            verdict, reason = "suspect", f"length {ratio:.0%} of the automatic draft"
    log.info("claude cleanup: %d chars, verdict=%s, $%.3f", len(md), verdict, cost)
    return ClaudeResult(md + "\n", verdict, reason, cost)
