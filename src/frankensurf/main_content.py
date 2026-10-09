"""The page's main content: the article, without menus, footers and banners.

Agents usually want the text a person came for, and only the first few
thousand characters of a page fit in front of a model. main_content() keeps
the article and drops navigation, so the start of the text is the start of
the content.

- HTML: trafilatura when it is installed (pip install "frankensurf[main]"),
  else a built-in extractor: drop boilerplate elements by tag, ARIA role and
  class or id words, prefer <article>/<main>, else the block with the most
  paragraph text.
- Markdown (from hosted readers such as Jina Reader): cut the link lists at
  the top and bottom; keep the title.

Every rule is a web convention; nothing here knows a site.
"""
from __future__ import annotations

import re

_BOILERPLATE_TAGS = ("script", "style", "noscript", "svg", "nav", "header", "footer", "aside", "form",
                     "iframe", "template", "button", "dialog")
_BOILERPLATE_ROLES = {"navigation", "banner", "contentinfo", "complementary", "search", "dialog",
                      "alertdialog", "menu", "menubar"}
_BOILERPLATE_WORDS = re.compile(
    r"(?i)(^|[\s_-])(nav|navbar|menu|footer|header|masthead|sidebar|side-bar|breadcrumbs?|cookie|consent|"
    r"gdpr|banner|share|sharing|social|related|recommend|newsletter|subscribe|signup|promo|advert|ads?|"
    r"sponsor|popup|modal|overlay|comments?|skip|toolbar|pagination|outbrain|taboola)([\s_-]|$)")
_CONSENT = re.compile(r"(?i)\b(cookies?|consent|accept all|reject all|privacy settings|manage preferences|"
                      r"we use cookies|your privacy)\b")
_MD_LINK = re.compile(r"!?\[([^\]]*)\]\(([^)]*)\)")


def main_content(content: str, content_type: str, url: str, text: str | None = None) -> dict | None:
    """{"text", "method", "chars"} for an HTML or markdown page, or None."""
    kind = (content_type or "").lower()
    if not content:
        return None
    if "markdown" in kind or ("html" not in kind and content.lstrip().startswith(("#", "Title:"))):
        extracted, method = _markdown_main(content), "markdown"
    elif "html" in kind:
        extracted, method = _trafilatura(content, url), "trafilatura"
        if not extracted or len(extracted) < 200:
            extracted, method = _html_main(content), "builtin"
            if not extracted or len(extracted) < 300:
                # Class words can catch a whole page wrapper ("site-header-layout");
                # strip by tag and role only.
                loose = _html_main(content, strict=False)
                if loose and len(loose) > len(extracted or ""):
                    extracted = loose
    else:
        return None
    if not extracted:
        return None
    result = {"text": extracted, "method": method, "chars": len(extracted)}
    if consent_only(extracted):
        result["cookie_notice"] = True
    return result


def consent_only(text: str) -> bool:
    """Text that is mostly a cookie or consent notice, not the page."""
    words = len(text.split())
    hits = len(_CONSENT.findall(text))
    return 0 < words < 400 and hits >= 3 and hits / words > 0.02


def _trafilatura(content, url):
    try:
        import trafilatura
    except ImportError:
        return None
    try:
        return trafilatura.extract(content[:4_000_000], url=url, include_comments=False, include_tables=True,
                                   favor_recall=True, output_format="txt") or None
    except Exception:
        return None


def _boilerplate(element, strict=True) -> bool:
    if element.name in _BOILERPLATE_TAGS:
        return True
    attrs = getattr(element, "attrs", None) or {}
    if str(attrs.get("role") or "").lower() in _BOILERPLATE_ROLES:
        return True
    if attrs.get("aria-hidden") == "true" or "hidden" in attrs:
        return True
    if not strict:
        return False
    words = " ".join([str(attrs.get("id") or ""), " ".join(attrs.get("class") or [])])
    return bool(words.strip()) and bool(_BOILERPLATE_WORDS.search(words))


def _html_main(content, strict=True):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(content[:4_000_000], "html.parser")
    title = soup.find("h1")
    for element in list(soup.find_all(True)):
        if getattr(element, "decomposed", False):
            continue
        # The article container itself may carry words like "related"; keep the obvious bodies.
        if element.name in ("article", "main") or (element.get("role") or "") == "main":
            continue
        try:
            if _boilerplate(element, strict):
                element.decompose()
        except AttributeError:
            continue
    candidates = soup.select('article, main, [role="main"], [itemprop="articleBody"]')
    best = max(candidates, key=lambda node: len(_paragraph_text(node)), default=None)
    if best is None or len(_paragraph_text(best)) < 400:
        blocks = soup.find_all(["div", "section", "td"], limit=5000)
        best = max(blocks, key=_density_score, default=None) or soup.body or soup
    lines = _lines(best)
    if title is not None:
        heading = " ".join(title.get_text(" ", strip=True).split())
        if heading and (not lines or lines[0] != heading):
            lines.insert(0, heading)
    return "\n\n".join(lines).strip() or None


def _paragraph_text(node):
    return " ".join(p.get_text(" ", strip=True) for p in node.find_all("p", limit=2000))


def _density_score(node):
    """Paragraph text in this block, less what sits in links: a readability-style
    score that favours the block holding the article."""
    paragraphs = [p for p in node.find_all("p", recursive=True, limit=500)
                  if len(p.get_text(" ", strip=True)) >= 40]
    if not paragraphs:
        return 0
    text = sum(len(p.get_text(" ", strip=True)) for p in paragraphs)
    linked = sum(len(a.get_text(" ", strip=True)) for p in paragraphs for a in p.find_all("a"))
    # Deep wrappers repeat their children's text; a small penalty prefers the tighter block.
    depth = len(list(node.parents))
    return text - 2 * linked + depth


def _lines(node):
    lines = []
    for element in node.find_all(["h1", "h2", "h3", "h4", "p", "li", "blockquote", "pre", "td", "figcaption"]):
        if element.find(["p", "li", "h1", "h2", "h3", "h4"]):
            continue  # Its children carry the text.
        text = " ".join(element.get_text(" ", strip=True).split())
        if len(text) < 2:
            continue
        if element.name == "li" and len(text) < 25 and element.find("a"):
            continue  # A menu entry that survived.
        if lines and lines[-1] == text:
            continue
        lines.append(text)
    return lines


def _link_share(line):
    visible = _MD_LINK.sub(lambda found: found.group(1), line)
    linked = sum(len(found.group(1)) for found in _MD_LINK.finditer(line))
    letters = len(re.sub(r"[\s*#>|_`-]", "", visible))
    return (linked / letters) if letters else 1.0


def _body_line(line):
    plain = _MD_LINK.sub(lambda found: found.group(1), line).strip(" *#>-|")
    return len(plain) >= 80 and _link_share(line) < 0.35


def _markdown_main(content):
    """Cut a markdown page to its body: from just before the first real
    paragraph to just after the last one, keeping the title line."""
    lines = content.splitlines()
    title = next((line for line in lines[:40] if line.startswith("# ") or line.lower().startswith("title:")), None)
    body = [index for index, line in enumerate(lines) if _body_line(line)]
    if not body:
        return content.strip() or None
    start, end = body[0], body[-1]
    # Keep up to three heading or short lines right before the first paragraph
    # (the article's own heading and byline), unless they are links.
    kept_before = 0
    while start > 0 and kept_before < 3:
        previous = lines[start - 1].strip()
        if previous and _link_share(previous) >= 0.5:
            break
        start -= 1
        kept_before += bool(previous)
    kept = [line for line in lines[start:end + 1]
            if not (line.strip() and _link_share(line) >= 0.8 and not _body_line(line))]
    text = "\n".join(kept).strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    if title and title.strip() not in text[:400]:
        text = title.strip() + "\n\n" + text
    return text or None
