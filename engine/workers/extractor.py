"""HTML content extractor — CSS selector, XPath, text, regex, and Markdown.

Powered by lxml + cssselect. Zero network calls; operates on raw HTML string.

Scrapling features ported here:
  to_markdown()  — convert HTML body to clean Markdown via markdownify
  find_similar() — adaptive element matching: score candidates by text/
                   attrs/position similarity and return those above threshold
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any

try:
    from lxml import html as lxml_html
    _LXML = True
except ImportError:  # pragma: no cover
    _LXML = False

try:
    from markdownify import markdownify as _md
    _MARKDOWNIFY = True
except ImportError:  # pragma: no cover
    _MARKDOWNIFY = False

_SKIP_TAGS: frozenset[str] = frozenset(
    {"script", "style", "meta", "link", "noscript", "head", "template"}
)
_SKIP_SCHEMES: tuple[str, ...] = ("javascript:", "mailto:", "tel:", "data:")


@dataclass
class ExtractionResult:
    ok: bool
    selector: str
    mode: str          # css | xpath | text | regex
    output_type: str   # text | html | attrs | all
    matches: list[dict[str, Any]] = field(default_factory=list)
    count: int = 0
    truncated: bool = False
    error: str | None = None


# ── helpers ────────────────────────────────────────────────────────────

def _el_path(el: Any) -> str:
    parts: list[str] = []
    node = el
    while node is not None:
        tag = str(node.tag) if isinstance(node.tag, str) else ""
        if not tag or tag.startswith("{") or tag == "[document]":
            break
        parts.append(tag)
        node = node.getparent()
    return " > ".join(reversed(parts))


def _el_dict(el: Any, output_type: str) -> dict[str, Any]:
    tag = str(el.tag) if isinstance(el.tag, str) else ""
    attrs: dict[str, str] = dict(el.attrib) if el.attrib is not None else {}
    inner_text = " ".join(el.itertext()).strip()
    if output_type == "text":
        return {"tag": tag, "text": inner_text}
    if output_type == "html":
        return {"tag": tag, "html": lxml_html.tostring(el, encoding="unicode", method="html")}
    if output_type == "attrs":
        return {"tag": tag, "text": inner_text, "attrs": attrs}
    # "all"
    return {
        "tag": tag,
        "text": inner_text,
        "html": lxml_html.tostring(el, encoding="unicode", method="html"),
        "attrs": attrs,
        "path": _el_path(el),
    }


def _visible_text(root: Any) -> str:
    """Recursively extract visible text, skipping script/style nodes."""
    parts: list[str] = []
    for el in root.iter():
        if str(el.tag) in _SKIP_TAGS:
            continue
        if el.text:
            t = el.text.strip()
            if t:
                parts.append(t)
        if el.tail:
            t = el.tail.strip()
            if t:
                parts.append(t)
    return " ".join(parts)


# ── main class ─────────────────────────────────────────────────────────

class HtmlExtractor:
    """Parse HTML and run CSS selector, XPath, text, or regex extraction.

    output_type controls what each match dict contains:
      text   → {tag, text}
      html   → {tag, html}
      attrs  → {tag, text, attrs}
      all    → {tag, text, html, attrs, path}
    """

    def __init__(self, html: str, base_url: str = "") -> None:
        if not _LXML:
            raise RuntimeError(
                "lxml not installed. Run: pip install 'lxml>=4.9' 'cssselect>=1.2'"
            )
        parser = lxml_html.HTMLParser(recover=True, encoding="utf-8")
        self._doc = lxml_html.document_fromstring(
            html.encode("utf-8", errors="replace"), parser=parser
        )
        if base_url:
            try:
                self._doc.make_links_absolute(base_url, resolve_base_href=False)
            except Exception:
                pass

    # ---- selection -------------------------------------------------------

    def css(
        self,
        selector: str,
        output_type: str = "all",
        limit: int = 50,
    ) -> ExtractionResult:
        """CSS selector extraction (requires cssselect package)."""
        try:
            elements = self._doc.cssselect(selector)
        except Exception as exc:
            return ExtractionResult(
                ok=False, selector=selector, mode="css",
                output_type=output_type, error=str(exc)[:160],
            )
        truncated = len(elements) > limit
        return ExtractionResult(
            ok=True, selector=selector, mode="css", output_type=output_type,
            matches=[_el_dict(el, output_type) for el in elements[:limit]],
            count=len(elements), truncated=truncated,
        )

    def xpath(
        self,
        query: str,
        output_type: str = "all",
        limit: int = 50,
    ) -> ExtractionResult:
        """XPath extraction."""
        try:
            raw = self._doc.xpath(query)
        except Exception as exc:
            return ExtractionResult(
                ok=False, selector=query, mode="xpath",
                output_type=output_type, error=str(exc)[:160],
            )
        raw_list: list[Any] = raw if isinstance(raw, list) else [raw]
        items: list[dict[str, Any]] = []
        for el in raw_list[:limit]:
            if isinstance(el, str):
                items.append({"tag": "#text", "text": el})
            elif hasattr(el, "tag"):
                items.append(_el_dict(el, output_type))
        return ExtractionResult(
            ok=True, selector=query, mode="xpath", output_type=output_type,
            matches=items, count=len(raw_list),
            truncated=len(raw_list) > limit,
        )

    def find_by_text(
        self,
        text: str,
        partial: bool = True,
        case_sensitive: bool = False,
        output_type: str = "all",
        limit: int = 20,
    ) -> ExtractionResult:
        """Find elements containing the given text."""
        needle = text if case_sensitive else text.lower()
        matched: list[Any] = []
        for el in self._doc.iter():
            if str(el.tag) in _SKIP_TAGS:
                continue
            inner = " ".join(el.itertext()).strip()
            haystack = inner if case_sensitive else inner.lower()
            if (needle in haystack) if partial else (needle == haystack):
                matched.append(el)
                if len(matched) >= limit * 4:
                    break
        return ExtractionResult(
            ok=True, selector=text, mode="text", output_type=output_type,
            matches=[_el_dict(el, output_type) for el in matched[:limit]],
            count=len(matched), truncated=len(matched) > limit,
        )

    def find_by_regex(
        self,
        pattern: str,
        output_type: str = "all",
        limit: int = 20,
    ) -> ExtractionResult:
        """Find elements whose visible text matches a regex pattern."""
        try:
            compiled = re.compile(pattern, re.IGNORECASE | re.DOTALL)
        except re.error as exc:
            return ExtractionResult(
                ok=False, selector=pattern, mode="regex",
                output_type=output_type, error=str(exc),
            )
        matched: list[Any] = []
        for el in self._doc.iter():
            if str(el.tag) in _SKIP_TAGS:
                continue
            inner = " ".join(el.itertext()).strip()
            if compiled.search(inner):
                matched.append(el)
                if len(matched) >= limit * 4:
                    break
        return ExtractionResult(
            ok=True, selector=pattern, mode="regex", output_type=output_type,
            matches=[_el_dict(el, output_type) for el in matched[:limit]],
            count=len(matched), truncated=len(matched) > limit,
        )

    # ---- convenience -------------------------------------------------------

    def get_all_text(self) -> str:
        """All visible text, script/style nodes removed."""
        return _visible_text(self._doc)

    def get_title(self) -> str:
        titles = self._doc.cssselect("title")
        if titles:
            return " ".join(titles[0].itertext()).strip()
        h1s = self._doc.cssselect("h1")
        if h1s:
            return " ".join(h1s[0].itertext()).strip()
        return ""

    def get_links(self) -> list[str]:
        """All non-inline href values from <a> tags."""
        links: list[str] = []
        for el in self._doc.cssselect("a[href]"):
            href = (el.get("href") or "").strip()
            if href and not any(href.startswith(s) for s in _SKIP_SCHEMES):
                links.append(href)
        return links

    def get_meta(self) -> dict[str, str]:
        """Extract common <meta> tags: description, keywords, og:title, etc."""
        meta: dict[str, str] = {}
        for el in self._doc.cssselect("meta"):
            name = (el.get("name") or el.get("property") or "").lower()
            content = el.get("content") or ""
            if name and content:
                meta[name] = content
        return meta

    def to_markdown(
        self,
        strip: list[str] | None = None,
        convert: list[str] | None = None,
    ) -> str:
        """Convert the HTML body to Markdown (requires markdownify).

        strip   — tag names whose content is dropped entirely (default: script, style)
        convert — tag names to convert; None means convert everything
        """
        if not _MARKDOWNIFY:
            return self.get_all_text()
        html_str = lxml_html.tostring(self._doc, encoding="unicode", method="html")
        _strip = strip if strip is not None else ["script", "style", "head", "noscript"]
        kwargs: dict[str, Any] = {"strip": _strip, "heading_style": "ATX"}
        if convert is not None:
            kwargs["convert"] = convert
        md: str = _md(html_str, **kwargs)
        lines = [ln.rstrip() for ln in md.splitlines()]
        cleaned = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
        return cleaned

    def find_similar(
        self,
        text: str = "",
        tag: str = "",
        attrs: dict[str, str] | None = None,
        threshold: float = 0.6,
        limit: int = 10,
        output_type: str = "all",
    ) -> ExtractionResult:
        """Adaptive element matching (Scrapling-inspired).

        Scores every element against the provided hints and returns those
        whose combined similarity exceeds `threshold` (0–1).

        Scoring breakdown (each component 0–1, equally weighted):
          text_sim   — SequenceMatcher ratio of element visible text vs `text`
          tag_sim    — 1 if tag matches, 0 otherwise (skipped if tag=="")
          attr_sim   — fraction of provided attrs that match element attrs
        """
        scores: list[tuple[float, Any]] = []
        want_attrs = attrs or {}

        for el in self._doc.iter():
            if not isinstance(el.tag, str) or el.tag in _SKIP_TAGS:
                continue

            components: list[float] = []

            if text:
                el_text = " ".join(el.itertext()).strip()
                components.append(
                    SequenceMatcher(None, text.lower(), el_text.lower()).ratio()
                )

            if tag:
                components.append(1.0 if el.tag.lower() == tag.lower() else 0.0)

            if want_attrs:
                el_attrs = dict(el.attrib) if el.attrib is not None else {}
                matched_attrs = sum(
                    1 for k, v in want_attrs.items()
                    if v.lower() in (el_attrs.get(k) or "").lower()
                )
                components.append(matched_attrs / len(want_attrs))

            if not components:
                continue
            score = sum(components) / len(components)
            if score >= threshold:
                scores.append((score, el))

        scores.sort(key=lambda x: x[0], reverse=True)
        top = [el for _, el in scores[:limit]]
        return ExtractionResult(
            ok=True, selector=f"similar(text={text!r},tag={tag!r})",
            mode="similar", output_type=output_type,
            matches=[_el_dict(el, output_type) for el in top],
            count=len(scores), truncated=len(scores) > limit,
        )
