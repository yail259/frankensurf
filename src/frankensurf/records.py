"""Repeated records on a page, with typed fields found by pluggable detectors.

A listing page (shop results, a deal feed, a job board, search results)
repeats one block per record: a title link, maybe an image, and a few short
facts. This module finds those blocks by shape alone and types their facts:

- markdown_records(text, url) finds repeated cards in markdown or plain text
  (what text-only readers return), where there is no markup to select from;
- detect(text) runs the field detectors over one block's text. Detectors are
  plain functions in DETECTORS, grouped in SETS: an "offer" set (price,
  was_price, currency, availability, stock_count) beside a general set
  (date, rating, count, location). A record carries a field only when the
  block makes it evident.

Nothing here knows a site or assumes products: offers are one detector set
among others. Item fields keep the items contract (name, url, image, price,
was_price, availability, stock_count) so shopping pages read as before.
"""
from __future__ import annotations

import contextvars
import re
from urllib.parse import urljoin, urlparse

# ---- prices ----

# Currency markers before a number, longest first; the code when the marker
# names one (a bare "$" or "¥" names no currency on its own).
_CURRENCY_PREFIX = (("AU$", "AUD"), ("A$", "AUD"), ("US$", "USD"), ("NZ$", "NZD"), ("CA$", "CAD"),
                    ("C$", "CAD"), ("HK$", "HKD"), ("S$", "SGD"), ("R$", "BRL"), ("AUD", "AUD"),
                    ("USD", "USD"), ("NZD", "NZD"), ("CAD", "CAD"), ("EUR", "EUR"), ("GBP", "GBP"),
                    ("£", "GBP"), ("€", "EUR"), ("₹", "INR"), ("$", None), ("¥", None))
_CURRENCY_SUFFIX = (("€", "EUR"), ("kr", None), ("zł", "PLN"), ("Kč", "CZK"))
_PREFIX = "|".join(("(?<![A-Za-z])" + re.escape(marker).replace(r"\$", r"\s?\$")) if marker[0].isalpha()
                   else re.escape(marker) for marker, _ in _CURRENCY_PREFIX)
_SUFFIX = "|".join(re.escape(marker) for marker, _ in _CURRENCY_SUFFIX)
# A number split across elements ("$ 69 . 99", "69</span><span>.99") still reads
# as one: whitespace may sit between the marker, the whole part, the point and
# the cents. A word-glued marker (US$ in "MouseUS$") still counts.
_NUMBER = r"(\d{1,3}(?:,\d{3})+|\d+)(?:\s*\.\s*(\d{1,2})(?!\d))?"
_PRICE = re.compile(rf"(?P<approx>[~≈]\s*)?(?P<code>{_PREFIX})\s*{_NUMBER}"
                    rf"|{_NUMBER}\s?(?P<suffix>{_SUFFIX})(?![A-Za-z])")
# Words just before a price that make it something other than the price now.
_WAS_BEFORE = re.compile(r"(?i)(?:\bwas|\brrp|\blist(?:\s+price)?|\breg(?:ular)?(?:\s+price)?|\boriginal(?:ly)?"
                         r"|\bretail|\bcompare\s+at|\breduced\s+from|\bbefore|\busually|\bstrike)\W{0,4}$")
# "$299.95 RRP", but not "$39.99 RRP: $49.99" or "$45 was $60" (now, then was).
_WAS_AFTER = re.compile(r"(?i)^\W{0,3}(?:rrp|list\s+price)\b(?![\s:]{0,4}(?:[$£€₹]|[A-Z]{1,3}\s?\$|\d))")
_SAVE_BEFORE =re.compile(r"(?i)\b(?:save|saving|you\s+save|discount)\W{0,4}$")
_SAVE_AFTER = re.compile(r"(?i)^\s*(?:off\b|discount\b)")
_THRESHOLD_BEFORE = re.compile(r"(?i)(?:\bover|\babove|\bspend|\bmin(?:imum)?|\boff|\borders?\s+of|\bafter|\+)"
                               r"\W{0,3}$")
_THRESHOLD_AFTER = re.compile(r"(?i)^\W{0,3}(?:(?:min(?:imum)?\s+)?spend|or\s+more|and\s+over)\b")
_FEE_AFTER = re.compile(r"(?i)^\W{0,3}(?:shipping|postage|delivery\s+fee|p&p|per\s+month|/\s*mo|a\s+month|pm\b)"
                        r"|^\s*/\s*\d*\s*(?:k?g|m?l|oz|lb|ea|each|unit|item|piece|pc)\b")  # a unit price
_RANGE_JOIN = re.compile(r"^\s*(?:-|–|—|to)\s*$")


def _currency(match):
    marker = "".join((match.group("code") or match.group("suffix")).split())
    table = _CURRENCY_PREFIX if match.group("code") else _CURRENCY_SUFFIX
    return next((code for name, code in table if name == marker), None)


def prices(text):
    """Every amount attached to a currency marker in text, in order: dicts of
    value, currency (when the marker names one), and role: price, was (a
    was/RRP/list price), save (an amount saved), approx (a converted guide
    like ~A$49), other (a threshold or fee), plus upto for a range's top."""
    found = []
    text = text or ""
    for match in _PRICE.finditer(text):
        groups = match.groups()
        whole, cents = (groups[2], groups[3]) if match.group("code") else (groups[4], groups[5])
        if whole is None:
            continue
        value = float(whole.replace(",", "") + ("." + cents if cents else ""))
        before = text[max(0, match.start() - 24):match.start()]
        after = text[match.end():match.end() + 20]
        role = ("approx" if match.group("approx")
                else "was" if _WAS_BEFORE.search(before) or _WAS_AFTER.search(after)
                else "save" if _SAVE_BEFORE.search(before) or _SAVE_AFTER.search(after)
                else "other" if (_THRESHOLD_BEFORE.search(before) or _THRESHOLD_AFTER.search(after)
                                 or _FEE_AFTER.search(after))
                else "price")
        entry = {"value": int(value) if value.is_integer() else value, "currency": _currency(match),
                 "role": role, "start": match.start(), "end": match.end(),
                 "said": bool(_SAVE_BEFORE.search(before))}
        previous = found[-1] if found else None
        if (previous and previous["role"] == role == "price" and _RANGE_JOIN.match(text[previous["end"]:match.start()])
                and entry["value"] > previous["value"]):
            previous["upto"] = entry["value"]
            continue
        found.append(entry)
    return found


def _bare_number(text):
    """A string that is only a number, maybe with a currency marker: "39.99", "AUD 39"."""
    match = re.fullmatch(rf"\s*(?:{_PREFIX})?\s*(\d[\d,]*(?:\.\d+)?)\s*(?:{_SUFFIX})?\s*", text)
    if not match:
        return None
    try:
        value = float(match.group(1).replace(",", ""))
    except ValueError:
        return None
    return int(value) if value.is_integer() else value


# Offer clauses: "Mouse from $129 Delivered, White $159 (Was $259)" names two
# offers. Commas, semicolons, " / " and " | " end a clause; a bracket ends one
# only when it holds a price of its own ("(Was $259)" and "($299.95 RRP)" stay
# with the price before them). A comma inside a number ("$1,299") is no break.
_CLAUSE_BREAK = re.compile(r"\s*(?:;|,(?!\d)|\s/\s|\s\|\s)\s*")
_BRACKETED = re.compile(r"\(([^()]*)\)|\[([^\[\]]*)\]")
_LABEL_EDGE = re.compile(r"(?i)^(?:from|was|now|only|just|rrp|save|delivered|shipped|each|for|at|and|or|plus|"
                         r"with|in)\b\W*|\W*\b(?:from|was|now|only|just|rrp|save|delivered|shipped|each|for|at|"
                         r"and|or|plus|with|in)$")
_BREAK = "\x00"


def _plain_prices(text):
    return [entry for entry in prices(text) if entry["value"] > 0 and entry["role"] == "price"]


def clauses(text):
    """The offer clauses in text: pieces that each hold one price now, with the
    words naming it and its own was price or saving. A piece with no amount
    names the offer after it; a piece with only a was price or a saving
    belongs to the offer before it."""
    text = text or ""
    marked = _BRACKETED.sub(lambda found: (_BREAK + found.group(0) + _BREAK)
                            if _plain_prices(found.group(1) or found.group(2) or "") else found.group(0), text)
    pieces = [piece.strip() for part in marked.split(_BREAK) for piece in _CLAUSE_BREAK.split(part)]
    out, pending = [], []
    for piece in pieces:
        if not piece:
            continue
        found = [entry for entry in prices(piece) if entry["value"] > 0]
        if any(entry["role"] == "price" for entry in found) and (pending or not out or _words(_label(piece))):
            out.append(" ".join(pending + [piece]))
            pending = []
        elif found and out:
            # "..., was $60" is the was price of the offer before, and "US $236 / A $360"
            # (a price with no words of its own) is that offer in another currency.
            out[-1] += " " + piece
        else:
            pending.append(piece)  # A label ("White,") or a was price that leads ("RRP $199, now $99").
    if pending:
        if out:
            out[-1] += " " + " ".join(pending)
        else:
            out.append(" ".join(pending))
    return out


def _label(clause):
    """The words that name an offer: its clause without amounts or price words."""
    text = re.sub(r"(?i)\b(?:was|rrp|save|list\s+price)\b", " ", _PRICE.sub(" ", clause))
    text = re.sub(r"\(\s*[^\w()]*\)|\[\s*[^\w\[\]]*\]", " ", text)
    text = " ".join(re.sub(r"[()\[\]]", " ", text).split()).strip(" ,;:/|+-–—~")
    while True:
        shorter = _LABEL_EDGE.sub("", text).strip(" ,;:/|+-–—~")
        if shorter == text:
            return text
        text = shorter


def _words(text):
    return {word for word in re.findall(r"[^\W_]{2,}", (text or "").lower())}


def offer(text, *, title=None):
    """The price now, the was price and the currency evident in one block of text.

    The price now is the first plain price. A was price comes from an explicit
    was/RRP/list price above it, or a saving that agrees with two prices (or
    one: "$79 Save $10" was $89). Amounts that are thresholds, fees or
    converted guides are never the price, and a number with no currency
    marker is never a price.

    Text that names several offers ("from $129, White $159 (Was $259)") is
    read clause by clause: each pairs its own price and was price, never one
    from another clause. offers lists them (label, price, was_price); the
    price is the clause whose label best matches title, else the first."""
    # Several offers share one line (a deal title): the title when it shows
    # prices (a card's text may split them over lines), else the line the
    # price now comes from, is read clause by clause.
    line = title if title and _plain_prices(title) else next(
        (line for line in (text or "").split("\n") if _plain_prices(line)), "")
    parts = clauses(line)
    if len(parts) < 2:
        return _offer(text)
    offers = []
    for part in parts:
        found = _offer(part)
        if "price" in found:
            offers.append({"label": _label(part), **found})
    if len(offers) < 2:
        return _offer(text) if not offers else {key: value for key, value in offers[0].items() if key != "label"}
    # A title that is itself the line of offers names them all: the first leads.
    name = _words(title) if title and not _plain_prices(title) else set()
    chosen, best = offers[0], 0.0
    for candidate in offers:
        words = _words(candidate["label"])
        score = len(words & name) / len(words | name) if words and name else 0.0
        if score > best:
            chosen, best = candidate, score
    out = {key: value for key, value in chosen.items() if key != "label"}
    out["offers"] = [{key: value for key, value in entry.items() if key in ("label", "price", "was_price", "price_max",
                                                                           "currency")} for entry in offers]
    return out


def _offer(text):
    """offer() for text that names one offer."""
    # "$0" is a price that has not loaded (or an empty cart), not a price.
    found = [entry for entry in prices(text) if entry["value"] > 0]
    plain = [entry for entry in found if entry["role"] == "price"]
    if not plain:
        # A converted guide is better than nothing when it is all there is.
        plain = [entry for entry in found if entry["role"] == "approx"]
    if not plain:
        return {}
    saves = [entry["value"] for entry in found if entry["role"] == "save"]
    was = [entry["value"] for entry in found if entry["role"] == "was"]
    current = plain[0]
    values = {entry["value"] for entry in plain}
    # "$89 Save $10! $79": two prices a saving apart are was and now.
    for saving in saves:
        pair = [value for value in values if any(abs(value - other - saving) < 0.011 for other in values)]
        if pair:
            high = max(pair)
            low = next(value for value in values if abs(high - value - saving) < 0.011)
            current = next(entry for entry in plain if entry["value"] == low)
            was.append(high)
            break
    else:
        # One price and "Save $X": the price was X more. ("$X off" alone is
        # often a promotion with conditions, so it only confirms a pair.)
        said = [entry["value"] for entry in found if entry["role"] == "save" and entry["said"]]
        if said and len(values) == 1:
            was.append(round(current["value"] + said[0], 2))
    out = {"price": current["value"]}
    if current.get("upto"):
        out["price_max"] = current["upto"]
    was = [value for value in was if value > current["value"]]
    if was:
        out["was_price"] = max(was)
    if current["currency"]:
        out["currency"] = current["currency"]
    return out


def parse_price(value):
    """A price from a field's value: a number as it is, a bare numeric string,
    or the current price in a text (attached to a currency marker), else None."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value if value >= 0 else None
    if not isinstance(value, str):
        return None
    bare = _bare_number(value)
    if bare is not None:
        return bare
    return offer(value).get("price")


# ---- other detectors ----

_EXPIRED = re.compile(r"(?i)\b(?:expired|deal\s+(?:has\s+)?ended|offer\s+ended|no\s+longer\s+available)\b")
_OUT = re.compile(r"(?i)\b(?:out\s+of\s+stock|sold\s*out|unavailable|not\s+available|no\s+stock|backorder(?:ed)?|"
                  r"pre-?order)\b|\b0\s+(?:units?|unit\(s\)|left|in\s+stock)\b")
_LIMITED = re.compile(r"(?i)\b(?:only\s+\d+\s+left|\d+\s+left\s+in\s+stock|low\s+stock|limited\s+stock|"
                      r"few\s+left|almost\s+gone|selling\s+fast)\b")
_IN = re.compile(r"(?i)\b(?:in\s+stock|available\s+now|in\s+store|ready\s+to\s+ship|\d+\s+units?\b|\d+\s+unit\(s\))")
_STOCK_COUNT = re.compile(r"(?i)(?<![$\d.,])\b(?:only[ \t]+)?(\d{1,5})[ \t]+(?:units?\b|unit\(s\)|left\b|"
                          r"(?:items?[ \t]+)?in[ \t]+stock\b|available\b)|\bstock[ \t:]{0,3}(\d{1,5})\b")


def availability(text):
    """in_stock, out_of_stock, limited or expired when the block says so."""
    text = text or ""
    for state, pattern in (("expired", _EXPIRED), ("out_of_stock", _OUT), ("limited", _LIMITED), ("in_stock", _IN)):
        if pattern.search(text):
            return {"availability": state}
    return {}


def stock_count(text):
    found = _STOCK_COUNT.search(text or "")
    if not found:
        return {}
    return {"stock_count": int(found.group(1) or found.group(2))}


_MONTH = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?"
_DATE = re.compile(rf"(?i)\b(?:\d{{4}}-\d{{2}}-\d{{2}}(?:[T ]\d{{2}}:\d{{2}}(?::\d{{2}})?)?"
                   rf"|\d{{1,2}}(?:st|nd|rd|th)?\s+{_MONTH},?\s+\d{{4}}|{_MONTH}\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}}"
                   rf"|\d{{1,2}}/\d{{1,2}}/\d{{4}}"
                   rf"|(?:\d+|an?)\s+(?:second|minute|min|hour|hr|day|week|month|year)s?\s+ago"
                   rf"|(?:posted|listed|updated|added)\s+(?:today|yesterday))\b")


def date(text):
    found = _DATE.search(text or "")
    return {"date": " ".join(found.group(0).split())} if found else {}


_RATING = re.compile(r"(?i)(?:\b(\d(?:\.\d)?)\s*(?:out\s+of\s+5|/\s*5\b)(?:\s*stars?)?|\b(\d\.\d)\s+stars?\b"
                     r"|★\s*(\d(?:\.\d)?)\b|\brat(?:ing|ed)\W{0,3}(\d(?:\.\d)?)\b)")
_COUNT = re.compile(r"(?i)\(?\b(\d{1,3}(?:,\d{3})+|\d+)\)?\s+(reviews?|ratings?|comments?|votes?|answers?|replies|"
                    r"views|applicants|bids|sold)\b")


def rating(text):
    found = _RATING.search(text or "")
    if not found:
        return {}
    value = float(next(group for group in found.groups() if group))
    return {"rating": value} if 0 < value <= 5 else {}  # 0.0 is "not rated yet"


def count(text):
    found = _COUNT.search(text or "")
    if not found:
        return {}
    noun = found.group(2).lower()
    return {"count": int(found.group(1).replace(",", "")), "count_of": noun if noun.endswith("s") else noun + "s"}


_LOCATION_LABEL = re.compile(r"(?im)(?:^|\|)\s*(?:location|based\s+in|📍)\s*:?\s*([^\n|]{2,60})")
# A short line that is only "Place, REGION" (a region code, maybe a postcode) or
# Remote/Hybrid: how cards show where.
_PLACE_LINE = re.compile(r"(?m)^[ \t]*((?:Remote|Hybrid)\b[^\n\d]{0,30}|[A-Z][a-z'.-]+(?:[ \t][A-Z][a-z'.-]+){0,3},"
                         r"[ \t]*[A-Z]{2,3}(?:[ \t]\d{4,5})?)[ \t]*$")


def location(text):
    found = _LOCATION_LABEL.search(text or "") or _PLACE_LINE.search(text or "")
    return {"location": " ".join(found.group(1).split()).strip(" ,")} if found else {}


_AD_LABEL = re.compile(r"(?im)^\s*(?:sponsored(?:\s+ad)?|advertisement|promoted|ad)\s*$")


def sponsored(text):
    return {"sponsored": True} if _AD_LABEL.search(text or "") else {}


DETECTORS = {"offer": offer, "availability": availability, "stock_count": stock_count, "date": date,
             "rating": rating, "count": count, "location": location, "sponsored": sponsored}
SETS = {"offers": ("offer", "availability", "stock_count"), "general": ("date", "rating", "count", "location"),
        "ads": ("sponsored",)}
DEFAULT = SETS["offers"] + SETS["general"] + SETS["ads"]


def detect(text, names=DEFAULT, *, title=None):
    """Typed fields evident in one block's text, from the named detectors.
    Place-like fields are read from the lines other than the title."""
    fields = {}
    body = text or ""
    if title:
        body = "\n".join(line for line in body.split("\n") if line.strip() != title.strip())
    for name in names:
        detector = DETECTORS.get(name)
        if detector is None:
            continue
        found = (detector(text, title=title) if name == "offer"
                 else detector(body if name == "location" else text))
        for key, value in found.items():
            fields.setdefault(key, value)
    return fields


# ---- ads ----

_AD_HOST = re.compile(r"(?i)(?:^|\.)(?:ad|ads|adclick|adserver|sponsored-ads|sponsored|clickserve|doubleclick|"
                      r"googleadservices|adsrvr|adnxs|criteo|taboola|outbrain)[\w-]*\.")
_AD_PATH = re.compile(r"(?i)/(?:aclk|adclick|clk|click|pagead|sspa/click|sponsored)(?:/|\?|$)")


def is_ad(record):
    """A sponsored entry: labelled so, or linking through an ad or click-tracking host."""
    if record.get("sponsored"):
        return True
    link = urlparse(str(record.get("url") or ""))
    name = str(record.get("name") or "")
    return bool(_AD_HOST.search((link.hostname or "") + ".") or _AD_PATH.search(link.path or "")
                or re.match(r"(?i)\s*sponsored\b", name))


# ---- markdown cards ----

_LINK = re.compile(r"(!?)\[((?:[^\[\]]|\[[^\[\]]*\](?:\([^)]*\))?)*)\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
_IMAGE = re.compile(r"!\[([^\]]*)\]\(\s*<?([^)\s>]+)")
_NOT_ITEM_LINK = re.compile(r"(?i)login|log-in|signin|sign-in|account|register|wishlist|cart|checkout|"
                            r"privacy|terms|contact|help|faq|about")
_BUTTON = re.compile(r"(?i)^(?:add\s+to\s+(?:cart|basket|wishlist)|buy(?:\s+now)?|view(?:\s+details)?|details|"
                     r"compare|more|read\s+more|shop\s+now|apply(?:\s+now)?|save|share|special|sale|new|hot)\W*$")
MIN_RECORDS = 3
# Other pages of the same site, for template subtraction: a callable from a
# page URL to the texts of its cached sibling pages, set by the runtime around
# discovery (no reads of its own). None, or no sibling cached, skips the check.
SIBLINGS = contextvars.ContextVar("frankensurf_record_siblings", default=None)
# Typed fields that are numbers: navigation carries none of them.
_NUMERIC = frozenset({"price", "was_price", "date", "rating", "count", "stock_count"})
# A record whose confidence falls below this is dropped (see _confidence).
MIN_CONFIDENCE = 0.5


def _plain(text):
    """Markdown inline text as plain text: links become their words, images go."""
    text = _IMAGE.sub("", text)
    text = _LINK.sub(lambda found: found.group(2), text)
    text = re.sub(r"[*_`#>]+", " ", text)
    text = re.sub(r"^\s*(?:[-+]|\d+\.)\s+", "", text)
    return " ".join(text.split())


def _same_site(host, other):
    from .module_discovery import _same_site as same
    return same(host, other)


def markdown_records(text, url, *, keep_text=False):
    """Records from markdown or text: repeated blocks that each open with a
    link to a different same-site page. Blocks are grouped by which typed
    fields they carry; the largest well-typed group is the listing.
    keep_text adds each record's own text as "_text" (for record_schema)."""
    text = text or ""
    host = urlparse(url).hostname or ""
    page = url.split("#")[0].rstrip("/")
    blocks, current, seen = [], None, set()
    lines = text.splitlines()[:40_000]
    for number, line in enumerate(lines):
        starts = []
        for found in _LINK.finditer(line):
            if found.group(1):
                continue
            target = urljoin(url, found.group(3)).split("#")[0]
            parsed = urlparse(target)
            label = _plain(found.group(2))
            if (parsed.scheme not in ("http", "https") or target.rstrip("/") == page
                    or not _same_site(parsed.hostname or "", host) or _NOT_ITEM_LINK.search(parsed.path or "")
                    or len(parsed.path.strip("/")) == 0):
                continue
            image = _IMAGE.search(found.group(2))
            starts.append((target, label, image))
        opener = next((start for start in starts if start[0] not in seen), None)
        if opener is not None and (current is None or opener[0] != current["url"]):
            seen.add(opener[0])
            current = {"url": opener[0], "names": [], "alts": [], "image": None, "lines": [], "start": number,
                       "label": opener[1]}
            blocks.append(current)
        if current is None:
            continue
        for target, label, image in starts:
            if target == current["url"]:
                if image:
                    current["image"] = current["image"] or urljoin(url, image.group(2))
                    # Readers caption images "Image 3: ..."; the alt text is a fallback name.
                    alt = re.sub(r"^Image\s+\d+\s*:\s*", "", _plain(image.group(1)))
                    if alt:
                        current["alts"].append(alt)
                elif label:
                    current["names"].append(label)
        if not current["image"]:
            image = _IMAGE.search(line)
            if image and not image.group(2).startswith("data:"):
                current["image"] = urljoin(url, image.group(2))
        plain = _plain(line)
        if plain:
            current["lines"].append(plain)
        if len(current["lines"]) > 60:
            current = None  # A card is short; a long run is page text, not a record.
    records = []
    for block in blocks:
        # A label ("[In stock](...)", "(6 new offers)", "$58.88") is not a name.
        names = [name for name in block["names"] + block["alts"] if 3 <= len(name) <= 300 and not _BUTTON.match(name)
                 and not (len(name) <= 20 and availability(name)) and not re.fullmatch(r"\(.*\)", name)
                 and len(re.findall(r"[^\W\d_]", _COUNT.sub("", _RATING.sub("", _PRICE.sub("", name))))) >= 3]
        if not names:
            continue
        # The title is the shortest full label (the longer ones are blurbs).
        name = min(names[:3], key=len) if len(names) > 1 and len(names[0]) > 160 else names[0]
        body = "\n".join(block["lines"])
        record = {"name": name, "url": block["url"]}
        if block["image"]:
            record["image"] = block["image"]
        typed = detect(body, title=name)
        record.update(typed)
        # The record's signature counts only card facts: a price in running
        # prose ("book online at $80 for ...") is page text, not a field.
        terse = "\n".join(line for line in block["lines"] if line == name or not _prose(line))
        following = next((line for line in block["lines"][block["lines"].index(name) + 1:]
                          if line != name), None) if name in block["lines"] else None
        if (following and 2 <= len(following) <= 60 and ":" not in following and not _BUTTON.match(following)
                and not re.search(r"\d", following) and not detect(following, title=None)):
            record["subtitle"] = following
        record["_shape"] = frozenset(key for key in detect(terse, title=name) if key not in ("sponsored", "count_of"))
        record["_lines"] = block["lines"]
        record["_position"] = block["start"] / max(1, len(lines))
        record["_label"] = block["label"]
        records.append(record)
    template = None
    siblings = SIBLINGS.get()
    if siblings is not None and len(records) >= MIN_RECORDS:
        try:
            template = page_template(siblings(url), url)
        except Exception:
            template = None  # Template subtraction is a refinement; a bad cache entry never stops a read.
    found = _dominant(records, template)
    if not keep_text:
        for record in found:
            record.pop("_text", None)
    return found


def _prose(line):
    """A line of running prose, not a card fact: a long sentence."""
    words = line.split()
    return len(line) > 200 or (len(words) >= 10 and line.rstrip().endswith((".", "!", "?")))


def _key(line):
    return " ".join(line.lower().split())


def page_template(texts, url):
    """What a site repeats on every page (menus, footers, banners): the lines
    and link targets of its other pages' texts. Returns (lines, links), or None
    when there is no other page."""
    lines, links = set(), set()
    for text in texts or ():
        for line in (text or "").splitlines()[:40_000]:
            for found in _LINK.finditer(line):
                if not found.group(1):
                    links.add(urljoin(url, found.group(3)).split("#")[0].rstrip("/"))
            plain = _key(_plain(line))
            if len(plain) >= 3:
                lines.add(plain)
    return (lines, links) if lines or links else None


def _templated(record, template):
    """True when a record is the site's template: its link or name and most
    of its lines also sit on another page of the site."""
    lines, links = template
    own = [_key(line) for line in record["_lines"] if len(line) >= 3]
    if not own:
        return False
    shared = sum(1 for line in own if line in lines)
    return ((record["url"].rstrip("/") in links or _key(record["name"]) in lines)
            and shared >= 0.5 * len(own))


def _confidence(members, template):
    """Each member's confidence that it is a record of the group's listing,
    from three signals stacked (each one multiplies):

    - field signature: the group's records share at least two field kinds
      (the link and a typed field, e.g. link+price or link+date) across most
      members; a group that does not is navigation (0), and a member missing
      some of the shared kinds is less likely one of them (0.5 to 1);
    - template: a member whose link and lines also sit on another cached page
      of the site is boilerplate (0.6: a product can be on two pages), and a
      group that is mostly boilerplate is the site's template (0);
    - position and link density: a member at the top or bottom of the page
      whose link is short and internal and that carries no numeric field is
      navigation (0.3).
    """
    kinds = [record["_shape"] | {"link"} for record in members]
    counts = {}
    for shape in kinds:
        for kind in shape:
            counts[kind] = counts.get(kind, 0) + 1
    shared = {kind for kind, hits in counts.items() if hits >= 0.6 * len(members)}
    if len(shared) < 2:
        return [0.0] * len(members)
    templated = [_templated(record, template) for record in members] if template else [False] * len(members)
    if template and sum(templated) >= 0.8 * len(members):
        return [0.0] * len(members)
    out = []
    for record, shape, boiler in zip(members, kinds, templated):
        confidence = 0.5 + 0.5 * len(shape & shared) / len(shared)
        if boiler:
            confidence *= 0.6
        position = record["_position"]
        if (position < 0.15 or position > 0.85) and not (record["_shape"] & _NUMERIC) \
                and len(str(record.get("_label") or record["name"]).split()) <= 4 \
                and len([part for part in urlparse(record["url"]).path.split("/") if part]) <= 2:
            confidence *= 0.3
        out.append(round(confidence, 2))
    return out


def _dominant(records, template=None):
    """The records that share the page's repeated shape: grouped by the typed
    fields they carry, the group with the most fields times rows wins, after
    each group drops its members of low confidence (_confidence). Plain link
    lists (menus, tags, crumbs) carry no fields and lose."""
    groups = {}
    for record in records:
        for key in record["_shape"]:
            groups.setdefault(key, []).append(record)
    best, best_score = [], 0
    for key, members in groups.items():
        if len(members) < MIN_RECORDS:
            continue
        scored = _confidence(members, template)
        members = [record for record, confidence in zip(members, scored) if confidence >= MIN_CONFIDENCE]
        if len(members) < MIN_RECORDS:
            continue
        shared = sum(1 for other in groups if len([m for m in members if other in m["_shape"]]) >= 0.6 * len(members))
        score = len(members) * (1 + shared) * (2 if key == "price" else 1)
        if score > best_score:
            best, best_score = members, score
    # Records of one listing link to one kind of page: when most share a path
    # segment (/product/, /job/, /dp/), a record without it is a side link
    # (a poster's profile, an offers page) that sat inside the card.
    segments = {}
    for record in best:
        for segment in set(urlparse(record["url"]).path.strip("/").split("/")[:-1]):
            if segment and not segment.isdigit():
                segments[segment] = segments.get(segment, 0) + 1
    common = {segment for segment, hits in segments.items() if hits >= 0.6 * len(best)}
    if common:
        best = [record for record in best
                if common & set(urlparse(record["url"]).path.strip("/").split("/")[:-1])]
    chosen = {id(record) for record in best}
    out = []
    for record in records:
        if id(record) in chosen and not is_ad(record):
            kept = {key: value for key, value in record.items() if not key.startswith("_") and key != "sponsored"}
            out.append({**kept, "_text": "\n".join(record["_lines"])})
    return out


# ---- items ----

_OPTIONAL = ("was_price", "price_max", "currency", "offers", "availability", "stock_count", "date", "rating",
             "count", "count_of", "location", "subtitle")


def enrich(item, text, names=DEFAULT):
    """Fill an item's evident typed fields from its block's text, keeping what
    it already has. The price keeps the item's own; a was price must be above it."""
    found = detect(text, names, title=item.get("name"))
    if found.pop("sponsored", None):
        item["sponsored"] = True
    if item.get("price") in (None, "") and "price" in found:
        item["price"] = found["price"]
    elif found.get("offers") and isinstance(item.get("price"), (int, float)):
        # The item's own price picks its offer: the was price is that offer's only.
        own = next((entry for entry in found["offers"] if entry["price"] == item["price"]), {})
        found.pop("was_price", None)
        if own.get("was_price"):
            found["was_price"] = own["was_price"]
    for key in _OPTIONAL:
        if key in found and item.get(key) in (None, ""):
            item[key] = found[key]
    if isinstance(item.get("was_price"), (int, float)) and isinstance(item.get("price"), (int, float)) \
            and item["was_price"] <= item["price"]:
        item.pop("was_price")
    return item


def page_prices(text):
    """How many plain prices the page's text shows: a listing shows many."""
    return sum(1 for entry in prices((text or "")[:400_000]) if entry["role"] == "price" and entry["value"] > 0)


__all__ = ["DETECTORS", "SETS", "DEFAULT", "detect", "offer", "prices", "parse_price", "is_ad",
           "markdown_records", "enrich", "page_prices"]
