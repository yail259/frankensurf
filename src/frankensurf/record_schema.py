"""A caller's own fields on each listing item, filled without a model.

read(url, items=True, schema=...) takes either a small object of field names
to type hints:

    {"role": "title", "salary": "price", "location": "location",
     "remote": "boolean", "posted": "date"}

or a JSON Schema subset (properties with type, format, description, and an
optional "x-hint"). Each item then carries extracted: {data, sources,
missing}, like the page-level result["extracted"].

Fields are filled deterministically from what the record detector already
found and from the record's own text:

1. by type hint: a field hinted "price" takes the item's price (or its
   was_price when the field is named like one), "date" its date, and so on;
2. else by label: a "Label: value" line in the record ("Salary: $120k")
   whose label is most like the field's name or description;
3. else, for a boolean, a line that is the field's own word ("Remote").

sources says where each value came from (item.price, label:Salary,
text:Remote, row.salary); missing lists the fields nothing filled. Nothing
here knows a site. A model could fill what is still missing; that hook is
model_fallback, off by default, and nothing in this release sets it.
"""
from __future__ import annotations

import re

from .records import availability, date, location, parse_price, prices

# Type hints a caller may give, and the JSON Schema type each one fills.
HINTS = {"title": "string", "text": "string", "price": "number", "number": "number", "integer": "integer",
         "date": "string", "location": "string", "url": "string", "image": "string", "boolean": "boolean",
         "rating": "number", "count": "integer", "availability": "string", "currency": "string"}
# Field-name words that suggest a hint when the schema gives none.
_HINT_WORDS = (
    ("title", {"title", "name", "role", "headline", "position", "job", "product", "event"}),
    ("price", {"price", "cost", "salary", "pay", "wage", "fee", "amount", "rate", "fare", "ticket", "rent",
               "compensation", "was", "rrp"}),
    ("date", {"date", "posted", "published", "when", "time", "day", "listed", "updated", "starts", "start"}),
    ("location", {"location", "where", "city", "place", "venue", "address", "suburb", "region", "area"}),
    ("url", {"url", "link", "href"}),
    ("image", {"image", "photo", "picture", "thumbnail", "img"}),
    ("rating", {"rating", "stars", "score"}),
    ("count", {"reviews", "comments", "votes", "applicants", "bids"}),
    ("availability", {"availability", "stock", "status"}),
    ("currency", {"currency"}),
)
# Words that name the same thing: a "Pay:" label answers a salary field.
_SAME = ({"salary", "pay", "wage", "compensation", "remuneration", "package"},
         {"location", "where", "city", "place", "venue", "address", "suburb", "region", "area"},
         {"posted", "listed", "published", "date", "added", "when", "on"},
         {"company", "employer", "organisation", "organization", "business", "brand", "seller", "maker",
          "manufacturer", "by"},
         {"price", "cost", "fee", "amount", "ticket", "tickets", "admission", "entry"},
         {"remote", "wfh", "home"},
         {"type", "kind", "category", "format"},
         {"stock", "availability", "available", "status"})
_WAS_WORDS = {"was", "rrp", "original", "list", "old", "before", "regular"}
_IN_STOCK_WORDS = {"stock", "available", "availability", "instock"}
_LABEL_LINE = re.compile(r"^\s*([^\W\d_][\w &/().'-]{0,40}?)\s*[:：]\s*(.*?)\s*$")
_TRUE = re.compile(r"(?i)^(?:yes|y|true|1|✓|✔)$")
_FALSE = re.compile(r"(?i)^(?:no|n|false|0|✗|✘|none|n/a)$")


def _words(text):
    """Lower-case words of a name or label: snake_case and camelCase split."""
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", str(text or ""))
    return {word for word in re.split(r"[^0-9A-Za-z]+", text.lower()) if word}


def _expand(words):
    out = set(words)
    for group in _SAME:
        if out & group:
            out |= group
    return out


def is_hint_schema(schema):
    """True for the small form: an object of field names to type-hint strings."""
    return (isinstance(schema, dict) and bool(schema) and "properties" not in schema
            and all(isinstance(key, str) and isinstance(value, str) for key, value in schema.items()))


def to_json_schema(schema):
    """The small hint form as a JSON Schema (each hint kept as x-hint), so the
    page-level extractor and validation read one shape. A JSON Schema is
    returned as it is."""
    if not is_hint_schema(schema):
        return schema
    unknown = sorted({value for value in schema.values() if value not in HINTS})
    if unknown:
        raise ValueError("schema type hints must be one of " + ", ".join(sorted(HINTS))
                         + "; unknown: " + ", ".join(unknown))
    return {"type": "object", "properties": {name: {"type": HINTS[hint], "x-hint": hint}
                                             for name, hint in schema.items()}}


def fields(schema):
    """The fields a schema asks for: name, hint, type and description words."""
    schema = to_json_schema(schema)
    out = []
    for name, spec in (schema.get("properties") or {}).items():
        spec = spec if isinstance(spec, dict) else {}
        kind = spec.get("type") if isinstance(spec.get("type"), str) else "string"
        hint = spec.get("x-hint") if spec.get("x-hint") in HINTS else None
        words = _words(name)
        described = words | _words(spec.get("description"))
        if hint is None:
            if kind == "boolean":
                hint = "boolean"
            elif spec.get("format") in ("date", "date-time"):
                hint = "date"
            elif spec.get("format") in ("uri", "url", "iri"):
                hint = "url"
            else:
                # The field's name first, then its description; the hint must fill the field's type.
                numeric = kind in ("number", "integer")
                hint = next((candidate for names_of in (words, described) for candidate, names in _HINT_WORDS
                             if names_of & names and (HINTS[candidate] in ("number", "integer")) == numeric),
                            None) or ("number" if numeric else "text")
        out.append({"name": name, "hint": hint, "type": kind, "words": described})
    return out


def _labels(text):
    """("Salary", "$120,000") pairs from a record's text: "Label: value" lines
    (or pieces of a line split by | and ·), and a "Label:" line whose value is
    the next line."""
    pairs = []
    lines = [line.strip() for line in str(text or "").split("\n")]
    for number, line in enumerate(lines):
        for piece in re.split(r"\s+[|·•]\s+", line):
            found = _LABEL_LINE.match(piece)
            if not found:
                continue
            label, value = found.group(1).strip(), found.group(2).strip()
            if not value and number + 1 < len(lines):
                value = lines[number + 1]
            if value and not re.match(r"^//", value):  # Not a URL's scheme split ("https: //").
                pairs.append((label, value))
    return pairs


def _coerce(value, hint, kind):
    """A label's value read as the field's hint, or None when it is not one."""
    value = " ".join(str(value).split())
    if hint == "price":
        return parse_price(value)
    if hint in ("number", "integer", "rating", "count"):
        found = re.search(r"-?\d[\d,]*(?:\.\d+)?", value)
        if not found:
            return None
        number = float(found.group(0).replace(",", ""))
        return int(number) if hint in ("integer", "count") or kind == "integer" or number.is_integer() else number
    if hint == "boolean":
        return True if _TRUE.match(value) else False if _FALSE.match(value) else None
    if hint == "date":
        return date(value).get("date") or value[:80]
    if hint == "location":
        return location(value).get("location") or value[:120]
    if hint == "availability":
        return availability(value).get("availability")
    return value[:500] or None


def _from_item(field, item, claimed):
    """The detector's own field for a hint, when the item has it and no other
    field took it. Returns (value, source) or (None, None)."""
    hint, words = field["hint"], field["words"]
    key = {"title": "name", "date": "date", "location": "location", "url": "url", "image": "image",
           "rating": "rating", "count": "count", "availability": "availability", "currency": "currency"}.get(hint)
    if hint == "price":
        key = "was_price" if words & _WAS_WORDS else "price"
    if hint == "boolean" and words & _IN_STOCK_WORDS and item.get("availability"):
        # "in_stock": true from what the card says about stock.
        state = item["availability"]
        return state in ("in_stock", "limited"), "item.availability"
    if key and key not in claimed and item.get(key) not in (None, "", [], {}):
        return item[key], f"item.{key}"
    return None, None


def _similarity(field, label):
    """How alike a field and a label are: shared words (with their synonyms)
    over all words, from 0 to 1."""
    own, theirs = _expand(field["words"]), _expand(_words(label))
    if not own or not theirs:
        return 0.0
    return len(own & theirs) / len(_words(label) | field["words"])


def fill(schema_fields, item, text, *, row=None, model_fallback=None):
    """{data, sources, missing} for one item. text is the record's own text;
    row, when the source is JSON, lends its keys as labels. model_fallback,
    when given, is called as model_fallback(missing_fields, item, text) and
    returns {name: value}; nothing passes one in this release."""
    data, sources, claimed = {}, {}, set()
    pairs = _labels(text)
    if isinstance(row, dict):
        pairs += [(f"row.{key}", value) for key, value in row.items()
                  if not str(key).startswith("_") and isinstance(value, (str, int, float, bool))]
    used = set()
    for field in schema_fields:
        name, hint = field["name"], field["hint"]
        value, source = _from_item(field, item, claimed)
        if value is not None:
            data[name], sources[name] = value, source
            if source.startswith("item."):
                claimed.add(source[5:])
            continue
        # Labels in the record's text, most like the field's name first.
        ranked = sorted(((_similarity(field, label.removeprefix("row.")), index, label, raw)
                         for index, (label, raw) in enumerate(pairs) if index not in used), key=lambda x: (-x[0], x[1]))
        for score, index, label, raw in ranked:
            if score < 0.5:
                break
            coerced = (raw if isinstance(raw, bool) and hint == "boolean"
                       else raw if isinstance(raw, (int, float)) and hint in ("price", "number", "integer")
                       else _coerce(raw, hint, field["type"]))
            if coerced is not None:
                data[name] = coerced
                sources[name] = label if label.startswith("row.") else f"label:{label}"
                used.add(index)
                break
        if name in data:
            continue
        if hint == "boolean":
            # A tag line that is the field's own word: "Remote", "Free entry".
            for line in str(text or "").split("\n"):
                line_words = _words(line)
                if (line_words and len(line_words) <= 3 and field["words"] & line_words
                        and not line_words & {"not", "no", "non", "without"}):  # "Not remote" says nothing true.
                    data[name], sources[name] = True, f"text:{line.strip()[:60]}"
                    break
        elif hint == "price" and name not in data:
            # An amount that a label did not name, beside the field's own word ("$25 tickets").
            for line in str(text or "").split("\n"):
                if _expand(field["words"]) & _words(line):
                    found = [entry for entry in prices(line) if entry["role"] == "price" and entry["value"] > 0]
                    if found:
                        data[name], sources[name] = found[0]["value"], f"text:{line.strip()[:60]}"
                        break
    missing = [field["name"] for field in schema_fields if field["name"] not in data]
    if missing and model_fallback is not None:
        for name, value in (model_fallback([field for field in schema_fields if field["name"] in missing],
                                           item, text) or {}).items():
            if name in missing and value is not None:
                data[name], sources[name] = value, "model"
        missing = [field["name"] for field in schema_fields if field["name"] not in data]
    return {"data": data, "sources": sources, "missing": missing}


def fill_items(schema, items, texts, rows=None, *, model_fallback=None):
    """Each item with extracted: {data, sources, missing}, from fill()."""
    schema_fields = fields(schema)
    for index, item in enumerate(items):
        row = rows[index] if rows is not None and index < len(rows) else None
        item["extracted"] = fill(schema_fields, item, texts[index] if index < len(texts) else "",
                                 row=row, model_fallback=model_fallback)
    return items


__all__ = ["HINTS", "is_hint_schema", "to_json_schema", "fields", "fill", "fill_items"]
