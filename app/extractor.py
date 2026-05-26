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
        "body": tts_clean(md),
        "title": title,
        "author": author,
        "method": method,
        "confidence_low": len((md or "").strip()) < 400,
    }


# --- TTS-readiness pass ----------------------------------------------------

_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]+\)")
_CODE_FENCE_RE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
_MULTI_BLANK_RE = re.compile(r"\n{3,}")
_MULTI_SPACE_RE = re.compile(r"[ \t]{2,}")


def tts_clean(md: str) -> str:
    if not md:
        return ""
    # replace links with their visible text
    md = _LINK_RE.sub(r"\1", md)
    # drop images entirely
    md = _IMAGE_RE.sub("", md)
    # keep code fence contents only if they look like prose (have spaces, no `;{}` cluster)
    def _fence(match: re.Match[str]) -> str:
        body = match.group(1)
        looks_like_prose = (
            " " in body
            and not re.search(r"[{};=()<>]{2,}", body)
            and sum(1 for ch in body if ch.isalpha()) > len(body) * 0.5
        )
        return body if looks_like_prose else ""
    md = _CODE_FENCE_RE.sub(_fence, md)
    # inline code → bare text
    md = _INLINE_CODE_RE.sub(r"\1", md)
    # normalize smart quotes / dashes
    md = (
        md.replace("‘", "'").replace("’", "'")
        .replace("“", '"').replace("”", '"')
        .replace("–", "-").replace("—", "-")
        .replace("…", "...")
    )
    # collapse whitespace
    md = _MULTI_SPACE_RE.sub(" ", md)
    md = _MULTI_BLANK_RE.sub("\n\n", md)
    return md.strip() + "\n"
