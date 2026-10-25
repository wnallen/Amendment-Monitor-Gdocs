#!/usr/bin/env python3
"""Deterministic normalizer for watched terms documents.

Same input bytes + same options -> byte-identical output text, always. This is
what makes snapshot hashes comparable across runs: the model never performs
normalization, it only interprets diffs AFTER this pipeline says the content
changed.

Stdlib-only for HTML/plain text. PDF extraction needs `pypdf` (the run skill
installs the pinned version; the environment allows package managers).

Usage:
    python3 scripts/normalize.py RAW_FILE [--format html|pdf|text]
                                 [--select SELECTOR] [--charset NAME]
                                 [--meta META_JSON] > normalized.md

`--select` restricts HTML extraction to the element(s) matching a simple
selector (`main`, `#terms`, `.legal-body`, `article.dpa`, `div#content.wide`).
When given, it must match at least once or the script exits non-zero: a
selector that stops matching means the page was restructured, which the run
treats as a fetch failure rather than a silent re-baseline.

`--meta` writes a JSON sidecar describing exactly how the text was produced
(normalizer version, format, charset, selector, extractor version, sha256).
Two snapshots are hash-comparable only when those fields agree.

Format is auto-detected from content when --format is omitted.
"""

from __future__ import annotations

import argparse
import codecs
import hashlib
import io
import json
import re
import sys
from html.parser import HTMLParser

# Bump when the output of this script changes for the same input. Recorded in
# every snapshot's meta; the run re-baselines (instead of alerting) when the
# previous snapshot was produced by a different version.
NORMALIZER_VERSION = "2"

# Elements whose entire subtree is never document content — dropped wherever
# they appear. Only elements with a REQUIRED end tag belong here: dropping is
# depth-counted, so a void element (<input>, <source>) or one whose end tag is
# optional (<option>) would never close and silently discard the rest of the
# page.
DROP_ALWAYS = {
    "script",
    "style",
    "noscript",
    "template",
    "svg",
    "iframe",
    "canvas",
    "nav",
    "aside",
    "form",
    "button",
    "select",
    "label",
    "dialog",
    "menu",
    "video",
    "audio",
    "picture",
}
# Page chrome — dropped only OUTSIDE a content region. Inside <main>/<article>
# (or the selected element) an HTML5 <header> routinely carries the document
# title and "Effective: ..." date, and a <footer> its version note.
DROP_CHROME = {"header", "footer"}
HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
BLOCK_END = {
    "p",
    "div",
    "section",
    "article",
    "main",
    "ul",
    "ol",
    "table",
    "thead",
    "tbody",
    "blockquote",
    "pre",
    "figure",
    "figcaption",
    "details",
    "summary",
    "dl",
    "dd",
    "address",
    "header",
    "footer",
}
MAIN_REGIONS = {"main", "article"}
VOID = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}

# Invisible code points that CMS round-trips sprinkle into legal text and that
# carry no meaning: zero-width space/joiners, BOM, soft hyphen, word joiner.
INVISIBLE = re.compile("[​‌‍⁠﻿­]")
# Horizontal whitespace incl. NBSP and the Unicode space family.
HSPACE = re.compile("[ \t\r\f\v   -   　]+")

SELECTOR_RE = re.compile(r"^([a-zA-Z][\w-]*)?(?:#([\w-]+))?((?:\.[\w-]+)*)$")


class Selector:
    """A deliberately tiny selector: optional tag, optional #id, any .classes."""

    def __init__(self, text: str) -> None:
        m = SELECTOR_RE.match(text.strip())
        if not text.strip() or not m:
            raise ValueError(f"unsupported selector {text!r} (use tag, #id, .class, or tag#id.class)")
        self.text = text.strip()
        self.tag = (m.group(1) or "").lower() or None
        self.id = m.group(2)
        self.classes = {c for c in m.group(3).split(".") if c}

    def matches(self, tag: str, attrs: list[tuple[str, str | None]]) -> bool:
        if self.tag and tag != self.tag:
            return False
        a = {k.lower(): (v or "") for k, v in attrs}
        if self.id is not None and a.get("id") != self.id:
            return False
        return not self.classes or self.classes <= set(a.get("class", "").split())


class Extractor(HTMLParser):
    """Emits (in_region, text_fragment) pairs in document order."""

    def __init__(self, selector: Selector | None = None) -> None:
        super().__init__(convert_charrefs=True)
        self.selector = selector
        self.drop_depth = 0
        self.region_depth = 0
        self.saw_region = False
        # Open-tag stack of region starts so arbitrary selected elements
        # (e.g. <div id=terms>) close correctly: each entry is True when that
        # open tag started a region.
        self.region_stack: list[tuple[str, bool]] = []
        self.out: list[tuple[bool, str]] = []

    def _emit(self, s: str) -> None:
        if self.drop_depth == 0:
            self.out.append((self.region_depth > 0, s))

    def _is_region_start(self, tag: str, attrs: list[tuple[str, str | None]]) -> bool:
        if self.selector is not None:
            return self.selector.matches(tag, attrs)
        return tag in MAIN_REGIONS

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.drop_depth:
            if tag in DROP_ALWAYS or tag in DROP_CHROME:
                self.drop_depth += 1
            return
        if tag in DROP_ALWAYS or (tag in DROP_CHROME and self.region_depth == 0):
            self.drop_depth += 1
            return
        starts_region = self._is_region_start(tag, attrs)
        if starts_region:
            self.region_depth += 1
            self.saw_region = True
        if tag in HEADINGS:
            self._emit("\n\n" + "#" * HEADINGS[tag] + " ")
        elif tag == "li":
            self._emit("\n- ")
        elif tag in ("br", "tr", "dt"):
            self._emit("\n")
        elif tag == "hr":
            self._emit("\n---\n")
        if tag not in VOID:
            self.region_stack.append((tag, starts_region))

    def handle_endtag(self, tag: str) -> None:
        if (tag in DROP_ALWAYS or tag in DROP_CHROME) and self.drop_depth:
            self.drop_depth -= 1
            return
        # (A kept chrome element — header/footer inside a region — falls through
        # to the block-end handling below.)
        if self.drop_depth:
            return
        if tag in HEADINGS or tag == "tr":
            self._emit("\n")
        elif tag in ("td", "th"):
            self._emit(" | ")
        elif tag in BLOCK_END:
            self._emit("\n\n")
        # Pop to the matching open tag (tolerates unclosed inner elements).
        for i in range(len(self.region_stack) - 1, -1, -1):
            if self.region_stack[i][0] == tag:
                closed_regions = sum(1 for _, started in self.region_stack[i:] if started)
                self.region_depth = max(0, self.region_depth - closed_regions)
                del self.region_stack[i:]
                break

    def handle_data(self, data: str) -> None:
        self._emit(data)


def tidy(text: str) -> str:
    """Whitespace canonicalization — the deterministic final form."""
    text = INVISIBLE.sub("", text)
    lines = []
    for raw_line in text.split("\n"):
        line = HSPACE.sub(" ", raw_line).strip()
        line = re.sub(r"( \|)+$", "", line)  # trailing table-cell separators
        lines.append(line)
    out: list[str] = []
    for line in lines:
        if line == "" and (not out or out[-1] == ""):
            continue
        out.append(line)
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out) + "\n"


_META_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9_.:-]+)""", re.I)


def sniff_charset(raw: bytes, declared: str | None = None) -> str:
    """Pick the decoder: BOM, then the caller's (HTTP) charset, then <meta>, else utf-8.

    Anything unknown to Python's codec registry falls back to utf-8 so the
    result is still deterministic.
    """
    for bom, name in ((codecs.BOM_UTF8, "utf-8-sig"), (codecs.BOM_UTF16_LE, "utf-16"), (codecs.BOM_UTF16_BE, "utf-16")):
        if raw.startswith(bom):
            return name
    candidates = []
    if declared:
        candidates.append(declared)
    m = _META_CHARSET.search(raw[:8192])
    if m:
        candidates.append(m.group(1).decode("ascii", "ignore"))
    for c in candidates:
        try:
            return codecs.lookup(c).name
        except LookupError:
            continue
    return "utf-8"


def from_html(raw: bytes, selector: Selector | None = None, charset: str | None = None) -> str:
    text = raw.decode(charset or sniff_charset(raw), errors="replace")
    p = Extractor(selector)
    p.feed(text)
    p.close()
    if selector is not None and not p.saw_region:
        raise LookupError(f"selector {selector.text!r} matched nothing")
    # Prefer region content when the page marks one; else whole body.
    fragments = [s for in_region, s in p.out if in_region] if p.saw_region else [s for _, s in p.out]
    return tidy("".join(fragments))


def pdf_version() -> str | None:
    try:
        import pypdf
    except ImportError:
        return None
    return getattr(pypdf, "__version__", "unknown")


def from_pdf(raw: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        sys.exit("normalize.py: PDF input requires pypdf — run: pip install pypdf")

    reader = PdfReader(io.BytesIO(raw))
    pages = [(page.extract_text() or "") for page in reader.pages]
    return tidy("\n\n".join(pages))


# A real page opens with one of these within its first few KB. Plain-text
# terms that merely *mention* a tag mid-sentence rarely do.
_HTML_MARKER = re.compile(rb"<(!doctype|html|head|body|main|article|section|div|p|h[1-6]|table|ul|ol)[\s>]")


def detect(raw: bytes) -> str:
    if raw[:5] == b"%PDF-":
        return "pdf"
    head = raw[:4096].lower()
    if _HTML_MARKER.search(head):
        return "html"
    return "text"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("raw_file")
    ap.add_argument("--format", choices=["html", "pdf", "text"], default=None)
    ap.add_argument("--select", default=None, help="HTML region selector: tag, #id, .class, tag#id.class")
    ap.add_argument("--charset", default=None, help="charset from the HTTP Content-Type header, if any")
    ap.add_argument("--meta", default=None, help="write a JSON sidecar describing the pipeline here")
    args = ap.parse_args()

    try:
        selector = Selector(args.select) if args.select else None
    except ValueError as e:
        sys.exit(f"normalize.py: {e}")

    with open(args.raw_file, "rb") as f:
        raw = f.read()
    fmt = args.format or detect(raw)
    charset: str | None = None
    extractor: str | None = None
    if fmt == "pdf":
        normalized = from_pdf(raw)
        extractor = f"pypdf {pdf_version()}"
    elif fmt == "html":
        charset = sniff_charset(raw, args.charset)
        try:
            normalized = from_html(raw, selector, charset)
        except LookupError as e:
            sys.exit(f"normalize.py: {e} in {args.raw_file} — page restructured? treat as a fetch failure")
    else:
        charset = sniff_charset(raw, args.charset)
        normalized = tidy(raw.decode(charset, errors="replace"))
    if not normalized.strip():
        # An empty snapshot would hash stably and mask every later change.
        sys.exit(f"normalize.py: no text extracted from {args.raw_file} — treat as a fetch failure")

    if args.meta:
        meta = {
            "normalizer_version": NORMALIZER_VERSION,
            "source_format": fmt,
            "charset": charset,
            "selector": selector.text if selector else None,
            "extractor": extractor,
            "sha256": hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
            "raw_bytes": len(raw),
        }
        with open(args.meta, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2, sort_keys=True)
            f.write("\n")
    sys.stdout.write(normalized)


if __name__ == "__main__":
    main()
