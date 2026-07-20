"""URL → cleaned Markdown extraction with trafilatura, readability fallback."""
from __future__ import annotations

import re
from typing import Any

import httpx
import trafilatura
from bs4 import BeautifulSoup
from readability import Document


USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def fetch_url(url: str, timeout: float = 30.0) -> str:
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"}
    with httpx.Client(follow_redirects=True, timeout=timeout, headers=headers) as c:
        r = c.get(url)
        r.raise_for_status()
        return r.text


def _html_to_md_with_trafilatura(html: str, url: str | None) -> tuple[str, str, str]:
    """Returns (markdown_body, title, author). Empty strings if not found."""
    md = trafilatura.extract(
        html,
        url=url,
        output_format="markdown",
        include_links=False,
        include_images=False,
        include_formatting=True,
        favor_recall=True,
    ) or ""
    title = ""
    author = ""
    meta = trafilatura.extract_metadata(html)
    if meta is not None:
        title = (meta.title or "").strip()
        author = (meta.author or "").strip()
    return md, title, author


def _html_to_md_with_readability(html: str) -> tuple[str, str, str]:
    doc = Document(html)
    title = (doc.short_title() or "").strip()
    summary_html = doc.summary(html_partial=True)
    soup = BeautifulSoup(summary_html, "html.parser")
    # crude html→markdown: keep paragraph breaks, drop tags
    lines: list[str] = []
    for el in soup.find_all(["h1", "h2", "h3", "p", "li", "blockquote"]):
        text = el.get_text(" ", strip=True)
        if not text:
            continue
        if el.name == "h1":
            lines.append(f"# {text}")
        elif el.name == "h2":
            lines.append(f"## {text}")
        elif el.name == "h3":
            lines.append(f"### {text}")
        elif el.name == "li":
            lines.append(f"- {text}")
        elif el.name == "blockquote":
            lines.append(f"> {text}")
        else:
            lines.append(text)
    return "\n\n".join(lines), title, ""


def extract(html: str, url: str | None = None) -> dict[str, Any]:
    """Try trafilatura then readability. Returns dict with body/title/author/method."""
    md, title, author = _html_to_md_with_trafilatura(html, url)
    method = "trafilatura"
    if not md or len(md.strip()) < 200:
        rd_md, rd_title, rd_author = _html_to_md_with_readability(html)
        if rd_md and len(rd_md.strip()) > len(md.strip()):
            md, title, author, method = rd_md, rd_title or title, rd_author or author, "readability"
    return {
        # Keep the markdown structure (headings/paragraphs). app.bookjson parses
        # it into chapters/chunks and sanitizes each span for TTS at synth time.
        "body": (md or "").strip() + "\n",
        "title": title,
        "author": author,
        "method": method,
        "confidence_low": len((md or "").strip()) < 400,
    }


# --- TTS text sanitizing ----------------------------------------------------
# Henty (Chatterbox) has NO markdown sanitizer of its own: any stray '#', '*',
# '_', backticks, or link/footnote syntax reaches the model and gets vocalized
# as gibberish. We strip all of it before synthesis. Block *structure*
# (headings/paragraphs) is detected by app.bookjson before this runs per-span,
# so `sanitize_inline` only has to clean a single span of prose.

_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_REF_LINK_RE = re.compile(r"\[([^\]]+)\]\[[^\]]*\]")
_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_CODE_FENCE_RE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
# emphasis delimiters, longest first, with a backreference to the same closer
_EMPHASIS_RE = re.compile(r"(\*\*\*|\*\*|\*|___|__|_)(\S.*?\S|\S)\1")
_FOOTNOTE_RE = re.compile(r"\[\^?\d+\]")          # [3] or [^1] citation markers
_STRAY_MD_RE = re.compile(r"[*_`#>]+")            # safety net for leftovers
_HR_RE = re.compile(r"^\s*([-*_])(?:\s*\1){2,}\s*$")
_MULTI_BLANK_RE = re.compile(r"\n{3,}")
_MULTI_SPACE_RE = re.compile(r"[ \t]{2,}")

_PUNCT_MAP = {
    "‘": "'", "’": "'", "“": '"', "”": '"',
    "–": "-", "—": "-", "…": "...", " ": " ",
    "·": " ", "&": " and ", "%": " percent",
}


def _normalize_punct(text: str) -> str:
    for k, v in _PUNCT_MAP.items():
        text = text.replace(k, v)
    return text


def sanitize_inline(text: str) -> str:
    """Strip inline markdown/markup from a single span of prose, for TTS."""
    if not text:
        return ""
    text = _IMAGE_RE.sub("", text)
    text = _LINK_RE.sub(r"\1", text)
    text = _REF_LINK_RE.sub(r"\1", text)
    text = _INLINE_CODE_RE.sub(r"\1", text)
    # twice, to catch simple nesting like **_word_**
    text = _EMPHASIS_RE.sub(r"\2", text)
    text = _EMPHASIS_RE.sub(r"\2", text)
    text = _FOOTNOTE_RE.sub("", text)
    text = _normalize_punct(text)
    text = _STRAY_MD_RE.sub("", text)
    text = _MULTI_SPACE_RE.sub(" ", text)
    return text.strip()


def _fence_replacer(match: re.Match[str]) -> str:
    body = match.group(1)
    looks_like_prose = (
        " " in body
        and not re.search(r"[{};=()<>]{2,}", body)
        and sum(1 for ch in body if ch.isalpha()) > len(body) * 0.5
    )
    return body if looks_like_prose else ""


def strip_block_marker(line: str) -> str:
    """Remove a leading markdown block marker (heading/list/quote) from a line."""
    line = re.sub(r"^\s{0,3}#{1,6}\s+", "", line)
    line = re.sub(r"^\s{0,3}>\s?", "", line)
    line = re.sub(r"^\s{0,3}([-*+]|\d+[.)])\s+", "", line)
    return line


def tts_clean(md: str) -> str:
    """Fully clean markdown/text into plain prose paragraphs for TTS.

    Preserves paragraph breaks (blank-line separated) but removes ALL markdown
    (headings, lists, quotes, emphasis, links, code, footnotes). Used for the
    manual pasted-text path and as a general-purpose cleaner.
    """
    if not md:
        return ""
    md = _CODE_FENCE_RE.sub(_fence_replacer, md)
    out: list[str] = []
    for raw in re.split(r"\n\s*\n", md):
        lines = []
        for ln in raw.splitlines():
            if _HR_RE.match(ln) or not ln.strip():
                continue
            lines.append(strip_block_marker(ln.strip()))
        cleaned = sanitize_inline(" ".join(l for l in lines if l))
        if cleaned:
            out.append(cleaned)
    return "\n\n".join(out) + "\n" if out else ""
