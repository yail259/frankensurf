"""Site modules: per-site knowledge a calling agent saves, as data.

An agent that learns how a site works (its real search URL, where results
live in the page, which fields matter) can save that as a site module instead
of a throwaway parser. A module is data, never code:

- match: an origin and a path pattern, like a route recipe;
- templates: URLs with parameters, such as a search URL;
- readiness: a content selector and settle time (operational defaults only);
- sources: where rows live (JSON-LD, JSON embedded in a script, JSON the page
  fetched, or HTML elements);
- items: a field map over one source, optionally joined with a second;
- pagination: a page parameter or a next-link selector;
- invalid: markers of the site's error or empty-feed pages;
- assertions: a frankensurf.workload-assertions/v1 set over the output.

Modules live in the operator's state directory beside route recipes, share
their private-file storage, origin/path matching, versioning and the rule
that saved data never grants authority (identity, providers, paid tools or
other origins). Nothing site-specific ships with FrankenSurf: there is no
bundled module layer.
"""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, replace
import copy
import hashlib
import json
import re
from urllib.parse import parse_qsl, quote, quote_plus, urlencode, urljoin, urlparse, urlunparse

from .routes import RouteRecipeError, RouteRecipeRegistry, _defaults, _identifier, _origin, _version

SCHEMA = "frankensurf.site-module/v1"
STORE_SCHEMA = "frankensurf.site-modules/v1"
# The module a read is running with, for checks deep in the read path
# (site-declared invalid pages). Set only for the duration of one read.
ACTIVE: ContextVar = ContextVar("frankensurf_site_module", default=None)

_MAX_BYTES = 64 * 1024
_MAX_ITEMS = 500
_TEXT_MAX = 2000
_KEYS = frozenset({"schema", "id", "version", "match", "templates", "readiness", "sources", "items",
                   "pagination", "invalid", "assertions", "policy_defaults", "enabled", "notes"})
_PARAM = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]{0,63})\}")
_PATH_SEGMENT = re.compile(r"[^.]{1,128}")
# A quantified group containing a quantifier, e.g. (a+)+: catastrophic backtracking.
_NESTED_QUANTIFIER = re.compile(r"\([^()]*[+*][^()]*\)\s*[+*{]")
_PRICE_NUMBER = re.compile(r"-?\d[\d,]*(?:\.\d+)?")


class SiteModuleError(RouteRecipeError):
    code = "MODULE_INVALID"


def _invalid(message="Invalid site module"):
    raise SiteModuleError(message)


def _text(value, maximum=300, *, allow_empty=False):
    if not isinstance(value, str) or len(value) > maximum or (not allow_empty and not value.strip()):
        _invalid()
    return value


def _selector(value):
    _text(value, 300)
    try:
        from soupsieve import compile as compile_selector
        compile_selector(value)
    except Exception:
        _invalid("Invalid CSS selector in site module")
    return value


def _path(value):
    """A dotted path into JSON: names, or integer indexes into lists."""
    if value is None or value == "":
        return ""
    if not isinstance(value, str) or len(value) > 200 or not all(
            _PATH_SEGMENT.fullmatch(segment) for segment in value.split(".")):
        _invalid("Invalid path in site module")
    return value


def _int(value, low, high):
    if type(value) is not int or not low <= value <= high:
        _invalid()
    return value


def _strings(value, maximum_items=20, maximum=200):
    if not isinstance(value, list) or len(value) > maximum_items:
        _invalid()
    return [_text(item, maximum) for item in value]


def _validate_match(raw):
    if not isinstance(raw, dict) or not {"origin", "path_pattern"} <= set(raw) <= {
            "origin", "path_pattern", "query_keys"}:
        _invalid("Site module match needs origin and path_pattern")
    origin = _origin(raw["origin"])
    pattern = _text(raw["path_pattern"], 200)
    if _NESTED_QUANTIFIER.search(pattern):
        _invalid("Site module path_pattern has nested quantifiers")
    try:
        re.compile(pattern)
    except re.error:
        _invalid("Invalid site module path_pattern")
    keys = raw.get("query_keys")
    if keys is not None:
        keys = [_identifier(key) for key in _strings(keys, 30, 128)]
    return {"origin": origin, "path_pattern": pattern, **({"query_keys": keys} if keys is not None else {})}


def _validate_templates(raw, origin):
    if not isinstance(raw, dict) or len(raw) > 8:
        _invalid()
    templates = {}
    for name, spec in raw.items():
        _identifier(name)
        if not isinstance(spec, dict) or set(spec) - {"url", "params"} or "url" not in spec:
            _invalid("Site module template needs a url")
        url = _text(spec["url"], 1000)
        if not url.startswith(origin + "/") and url != origin:
            _invalid("Site module template must stay on the module's origin")
        params = spec.get("params") or {}
        if not isinstance(params, dict) or set(_PARAM.findall(url)) != set(params):
            _invalid("Site module template params must match its {placeholders}")
        checked = {}
        for param, rule in params.items():
            if rule == "required":
                checked[param] = {"required": True, "encoding": "query"}
                continue
            if not isinstance(rule, dict) or set(rule) - {"default", "required", "encoding"}:
                _invalid()
            encoding = rule.get("encoding", "query")
            if encoding not in ("query", "path"):
                _invalid()
            default = rule.get("default")
            if default is not None and type(default) not in (str, int):
                _invalid()
            checked[param] = {"required": bool(rule.get("required", default is None)),
                              "encoding": encoding, **({"default": default} if default is not None else {})}
        templates[name] = {"url": url, "params": checked}
    return templates


def _validate_sources(raw):
    if not isinstance(raw, dict) or not 0 < len(raw) <= 8:
        _invalid("Site module needs 1 to 8 sources")
    sources = {}
    for name, spec in raw.items():
        _identifier(name)
        if not isinstance(spec, dict):
            _invalid()
        kind = spec.get("kind")
        if kind == "jsonld":
            allowed, checked = {"kind", "type", "path"}, {"kind": kind, "path": _path(spec.get("path"))}
            if spec.get("type") is not None:
                checked["type"] = _text(spec["type"], 100)
        elif kind == "embedded_json":
            allowed = {"kind", "selector", "marker", "path", "decode"}
            checked = {"kind": kind, "selector": _selector(spec.get("selector", "script")),
                       "path": _path(spec.get("path"))}
            if spec.get("marker") is not None:
                checked["marker"] = _text(spec["marker"], 200)
            if spec.get("decode") is not None:
                if spec["decode"] not in _DECODERS:
                    _invalid("embedded_json decode must be one of: " + ", ".join(sorted(_DECODERS)))
                checked["decode"] = spec["decode"]
        elif kind == "captured_json":
            allowed = {"kind", "url_contains", "path"}
            checked = {"kind": kind, "url_contains": _text(spec.get("url_contains"), 300),
                       "path": _path(spec.get("path"))}
        elif kind == "html":
            allowed = {"kind", "item_selector", "fields"}
            fields = spec.get("fields")
            if not isinstance(fields, dict) or not 0 < len(fields) <= 40:
                _invalid("An html source needs 1 to 40 fields")
            checked_fields = {}
            for field, rule in fields.items():
                _identifier(field)
                if not isinstance(rule, dict) or set(rule) - {"selector", "attribute"}:
                    _invalid()
                attribute = rule.get("attribute")
                if isinstance(attribute, str):
                    attribute = [attribute]
                if attribute is not None and (not isinstance(attribute, list) or not 0 < len(attribute) <= 5):
                    _invalid("attribute is a name or a list of up to 5 names, tried in order")
                checked_fields[field] = {
                    **({"selector": _selector(rule["selector"])} if rule.get("selector") else {}),
                    **({"attribute": [_identifier(name) for name in attribute]} if attribute else {})}
            checked = {"kind": kind, "item_selector": _selector(spec.get("item_selector")),
                       "fields": checked_fields}
        else:
            _invalid("Site module source kind must be jsonld, embedded_json, captured_json or html")
        if set(spec) - allowed:
            _invalid("Unknown site module source setting")
        sources[name] = checked
    return sources


def _validate_field_map(raw):
    if not isinstance(raw, dict) or not 0 < len(raw) <= 40:
        _invalid("A field map needs 1 to 40 fields")
    fields = {}
    for name, rule in raw.items():
        _identifier(name)
        if isinstance(rule, str):
            rule = {"path": rule}
        if not isinstance(rule, dict) or set(rule) - {"path", "type"}:
            _invalid()
        kind = rule.get("type", "auto")
        if kind not in ("auto", "text", "number", "url"):
            _invalid("Field type must be auto, text, number or url")
        fields[name] = {"path": _path(rule.get("path")), "type": kind}
    return fields


def _validate_items(raw, sources):
    if not isinstance(raw, dict) or set(raw) - {"from", "fields", "join", "limit"}:
        _invalid()
    if raw.get("from") not in sources:
        _invalid("Site module items must come from a declared source")
    items = {"from": raw["from"], "fields": _validate_field_map(raw.get("fields")),
             "limit": _int(raw.get("limit", 200), 1, _MAX_ITEMS)}
    join = raw.get("join")
    if join is not None:
        if not isinstance(join, dict) or set(join) - {"from", "on", "fields"} or join.get("from") not in sources:
            _invalid("Site module join must name a declared source")
        on = join.get("on", "position")
        if on != "position":
            if not isinstance(on, dict) or set(on) != {"left", "right"}:
                _invalid("Join on is 'position' or {left, right}")
            on = {"left": _identifier(on["left"]), "right": _path(on["right"])}
        items["join"] = {"from": join["from"], "on": on, "fields": _validate_field_map(join.get("fields"))}
    return items


def _validate_pagination(raw):
    if not isinstance(raw, dict):
        _invalid()
    if "next_selector" in raw:
        if set(raw) != {"next_selector"}:
            _invalid()
        return {"next_selector": _selector(raw["next_selector"])}
    if set(raw) - {"param", "start", "step", "max_pages"} or "param" not in raw:
        _invalid("Pagination is {param, start, step, max_pages} or {next_selector}")
    return {"param": _identifier(raw["param"]), "start": _int(raw.get("start", 1), 0, 10_000),
            "step": _int(raw.get("step", 1), 1, 10_000), "max_pages": _int(raw.get("max_pages", 10), 1, 50)}


def _validate_invalid(raw):
    if not isinstance(raw, dict) or set(raw) - {"final_url_contains", "title", "text"} or not raw:
        _invalid()
    return {key: [item.lower() for item in _strings(raw[key])] for key in raw}


def _validate_readiness(raw):
    if not isinstance(raw, dict) or set(raw) - {"selector", "settle_ms", "timeout_seconds"}:
        _invalid()
    checked = {}
    if raw.get("selector") is not None:
        checked["selector"] = _selector(raw["selector"])
    if "settle_ms" in raw:
        checked["settle_ms"] = _int(raw["settle_ms"], 0, 30_000)
    if "timeout_seconds" in raw:
        checked["timeout_seconds"] = _int(raw["timeout_seconds"], 1, 20)
    return checked


@dataclass(frozen=True)
class SiteModule:
    id: str
    version: str
    match: object
    sources: object = None
    items: object = None
    templates: object = None
    readiness: object = None
    pagination: object = None
    invalid: object = None
    assertions: object = None
    policy_defaults: object = None
    enabled: bool = True
    notes: str = ""

    @classmethod
    def from_record(cls, raw):
        """Validate an agent-authored module. Raises SiteModuleError."""
        try:
            return cls._from_record(raw)
        except SiteModuleError:
            raise
        except RouteRecipeError as error:
            # The shared recipe validators (IDs, versions, origins) speak for modules too.
            raise SiteModuleError(str(error).replace("public route recipe", "site module")) from None

    @classmethod
    def _from_record(cls, raw):
        if not isinstance(raw, dict):
            _invalid()
        try:
            if len(json.dumps(raw, allow_nan=False)) > _MAX_BYTES:
                _invalid("Site module is larger than 64 KB")
        except (TypeError, ValueError):
            _invalid("Site module must be JSON")
        if raw.get("schema", SCHEMA) != SCHEMA or set(raw) - _KEYS:
            _invalid("Unknown site module field or schema")
        match = _validate_match(raw.get("match"))
        sources = _validate_sources(raw["sources"]) if raw.get("sources") is not None else None
        items = _validate_items(raw["items"], sources or {}) if raw.get("items") is not None else None
        if items is None and sources:
            _invalid("Site module sources need an items map")
        assertions = None
        if raw.get("assertions") is not None:
            from .repair import normalize_workload_assertions
            try:
                assertions = normalize_workload_assertions(raw["assertions"])
            except ValueError as error:
                _invalid(str(error))
        if raw.get("enabled", True) not in (True, False):
            _invalid()
        try:
            defaults = dict(_defaults(dict(raw.get("policy_defaults") or {}), (match["origin"],)))
        except RouteRecipeError:
            _invalid("Site module policy_defaults may hold operational settings only")
        module = cls(
            id=_identifier(raw.get("id")), version=_version(raw.get("version")), match=match,
            sources=sources, items=items,
            templates=_validate_templates(raw["templates"], match["origin"]) if raw.get("templates") else None,
            readiness=_validate_readiness(raw["readiness"]) if raw.get("readiness") else None,
            pagination=_validate_pagination(raw["pagination"]) if raw.get("pagination") else None,
            invalid=_validate_invalid(raw["invalid"]) if raw.get("invalid") else None,
            assertions=assertions, policy_defaults=defaults,
            enabled=raw.get("enabled", True), notes=_text(raw.get("notes", ""), 1000, allow_empty=True))
        if not (module.items or module.templates or module.invalid or module.readiness or module.policy_defaults):
            _invalid("Site module does nothing: add items, templates, invalid markers, readiness or defaults")
        return module

    def record(self):
        record = {"schema": SCHEMA, "id": self.id, "version": self.version, "match": self.match,
                  "enabled": self.enabled}
        for key in ("sources", "items", "templates", "readiness", "pagination", "invalid", "assertions"):
            if getattr(self, key):
                record[key] = getattr(self, key)
        if self.policy_defaults:
            record["policy_defaults"] = dict(self.policy_defaults)
        if self.notes:
            record["notes"] = self.notes
        return copy.deepcopy(record)

    @property
    def fingerprint(self):
        raw = json.dumps(self.record(), sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        return hashlib.sha256(raw).hexdigest()

    def metadata(self):
        return {"id": self.id, "version": self.version, "sha256": self.fingerprint, "enabled": self.enabled,
                "origin": self.match["origin"], "path_pattern": self.match["path_pattern"],
                "templates": sorted(self.templates or {}), "sources": sorted(self.sources or {}),
                "has_assertions": self.assertions is not None, "notes": self.notes}

    def matches(self, url):
        try:
            parsed = urlparse(url)
            if parsed.scheme + "://" + parsed.netloc != self.match["origin"] or len(parsed.path) > 2048:
                return False
            if re.fullmatch(self.match["path_pattern"], parsed.path) is None:
                return False
            keys = self.match.get("query_keys")
            return keys is None or {key for key, _ in parse_qsl(parsed.query)} <= set(keys)
        except (ValueError, TypeError):
            return False

    def same_origin(self, url):
        try:
            parsed = urlparse(url)
            return parsed.scheme + "://" + parsed.netloc == self.match["origin"]
        except (ValueError, TypeError):
            return False

    # ---- what a module adds to a read ----

    def policy_overrides(self):
        """Operational defaults this module supplies; the caller's own values win."""
        values = dict(self.policy_defaults or {})
        if self.readiness:
            if "selector" in self.readiness:
                values.setdefault("content_ready_selector", self.readiness["selector"])
                values.setdefault("content_ready_timeout_seconds", self.readiness.get("timeout_seconds", 10))
            if "settle_ms" in self.readiness:
                values.setdefault("settle_ms", self.readiness["settle_ms"])
        if any(spec["kind"] == "captured_json" for spec in (self.sources or {}).values()):
            values.setdefault("capture_json_responses", True)
        return values

    def build_url(self, template, params):
        if not self.templates or template not in self.templates:
            raise SiteModuleError(f"Site module {self.id} has no template {template!r}")
        spec = self.templates[template]
        params = dict(params or {})
        unknown = set(params) - set(spec["params"])
        if unknown:
            raise SiteModuleError("Unknown template parameters: " + ", ".join(sorted(unknown)))
        values = {}
        for name, rule in spec["params"].items():
            value = params.get(name, rule.get("default"))
            if value is None:
                raise SiteModuleError(f"Template parameter {name!r} is required")
            if type(value) not in (str, int) or len(str(value)) > 500:
                raise SiteModuleError(f"Template parameter {name!r} must be a short string or integer")
            encode = quote_plus if rule["encoding"] == "query" else (lambda text: quote(text, safe=""))
            values[name] = encode(str(value))
        url = _PARAM.sub(lambda found: values[found.group(1)], spec["url"])
        if not self.same_origin(url):
            raise SiteModuleError("Template URL left the module's origin")
        return url

    def invalid_page(self, title, text, final_url):
        """True when the site's own markers say this is an error or empty page."""
        if not self.invalid:
            return False
        final = (final_url or "").lower()
        title = (title or "").lower()
        body = (text or "")[:200_000].lower()
        return (any(marker in final for marker in self.invalid.get("final_url_contains", ()))
                or any(marker in title for marker in self.invalid.get("title", ()))
                or any(marker in body for marker in self.invalid.get("text", ())))

    def extract(self, result, url, *, query_terms=()):
        """The module's output for one observed page: items, next page, assertions."""
        base = result.get("url") or url
        output = {"items": [], "count": 0, "first": {}, "matching_query": []}
        if self.items:
            rows = _source_rows(self.sources[self.items["from"]], result, base)
            items = [_map_fields(row, self.items["fields"], base) for row in rows[:self.items["limit"]]]
            join = self.items.get("join")
            if join:
                others = _source_rows(self.sources[join["from"]], result, base)
                _join(items, others, join, base)
            items = [item for item in items if any(value not in (None, "", [], {}) for value in item.values())]
            output.update(items=items, count=len(items), first=items[0] if items else {})
            if query_terms:
                output["matching_query"] = [
                    item for item in items
                    if any(term in json.dumps(item, ensure_ascii=False).lower() for term in query_terms)]
        next_url = self._next_url(result, base)
        if next_url:
            output["next_url"] = next_url
        return output

    def _next_url(self, result, base):
        if not self.pagination:
            return None
        if "next_selector" in self.pagination:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup((result.get("content") or "")[:4_000_000], "html.parser")
            element = soup.select_one(self.pagination["next_selector"])
            href = element.get("href") if element is not None else None
            target = urljoin(base, href) if href else None
            return target if target and self.same_origin(target) else None
        parsed = urlparse(base)
        pairs = parse_qsl(parsed.query, keep_blank_values=True)
        param, start, step = self.pagination["param"], self.pagination["start"], self.pagination["step"]
        current = next((value for key, value in pairs if key == param), None)
        try:
            page = int(current) if current is not None else start
        except ValueError:
            return None
        if (page - start) // step + 1 >= self.pagination["max_pages"]:
            return None
        pairs = [(key, value) for key, value in pairs if key != param] + [(param, str(page + step))]
        return urlunparse(parsed._replace(query=urlencode(pairs)))


# ---- extraction ----

def _walk(value, path):
    """The value at a dotted path, or None. Integer segments index lists."""
    if not path:
        return value
    for segment in path.split("."):
        if isinstance(value, dict):
            value = value.get(segment)
        elif isinstance(value, list) and re.fullmatch(r"-?\d+", segment):
            index = int(segment)
            value = value[index] if -len(value) <= index < len(value) else None
        else:
            return None
        if value is None:
            return None
    return value


def _as_rows(value):
    if isinstance(value, list):
        return [row for row in value if row is not None]
    return [value] if isinstance(value, dict) else []


def _jsonld_blocks(structured):
    found = []

    def visit(node):
        if isinstance(node, dict):
            found.append(node)
            for key in ("@graph",):
                if isinstance(node.get(key), list):
                    for child in node[key]:
                        visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)
    visit((structured or {}).get("jsonld") if isinstance(structured, dict) else None)
    return found


def _types(node):
    if not isinstance(node, dict):
        return set()  # A row's "item" may be a plain string, not a node.
    kind = node.get("@type")
    return {kind} if isinstance(kind, str) else set(kind) if isinstance(kind, list) else set()


def _json_after(text, marker):
    """The JSON value that starts at the first { or [ after marker."""
    start = 0
    if marker is not None:
        start = text.find(marker)
        if start < 0:
            return None
        start += len(marker)
    positions = [index for index in (text.find("{", start), text.find("[", start)) if index >= 0]
    if not positions:
        return None
    try:
        value, _ = json.JSONDecoder().raw_decode(text, min(positions))
        return value
    except ValueError:
        return None


_NEXT_FLIGHT_PUSH = re.compile(r'self\.__next_f\.push\(\[1,("(?:[^"\\]|\\.)*")\]\)')


def _next_flight(content):
    """The Next.js app-router payload: the page's data streamed as JS string chunks.
    A framework convention, not site knowledge."""
    chunks = []
    for found in _NEXT_FLIGHT_PUSH.finditer(content[:8_000_000]):
        try:
            chunks.append(json.loads(found.group(1)))
        except ValueError:
            continue
    return "".join(chunks)


_DECODERS = {"next_flight": _next_flight}


def _source_rows(spec, result, base):
    kind = spec["kind"]
    if kind == "jsonld":
        for block in _jsonld_blocks(result.get("structured")):
            if spec.get("type") and spec["type"] not in _types(block):
                continue
            value = _walk(block, spec["path"])
            if value is not None:
                return _as_rows(value)
        return []
    if kind == "captured_json":
        captured = result.get("captured_json") or {}
        entries = captured.get("items") if isinstance(captured, dict) else captured
        for entry in entries or ():
            if isinstance(entry, dict) and spec["url_contains"] in str(entry.get("url") or ""):
                value = _walk(entry.get("data"), spec["path"])
                if value is not None:
                    return _as_rows(value)
        return []
    if kind == "embedded_json" and spec.get("decode"):
        text = _DECODERS[spec["decode"]](result.get("content") or "")
        value = _json_after(text, spec.get("marker")) if text else None
        value = _walk(value, spec["path"]) if value is not None else None
        return _as_rows(value) if value is not None else []
    from bs4 import BeautifulSoup
    soup = BeautifulSoup((result.get("content") or "")[:4_000_000], "html.parser")
    if kind == "embedded_json":
        for element in soup.select(spec["selector"])[:200]:
            text = element.get_text()
            if spec.get("marker") is not None and spec["marker"] not in text:
                continue
            value = _json_after(text, spec.get("marker"))
            value = _walk(value, spec["path"]) if value is not None else None
            if value is not None:
                return _as_rows(value)
        return []
    rows = []
    for element in soup.select(spec["item_selector"])[:_MAX_ITEMS]:
        row = {}
        for field, rule in spec["fields"].items():
            target = element.select_one(rule["selector"]) if rule.get("selector") else element
            if target is None:
                row[field] = None
            elif rule.get("attribute"):
                # The first attribute present wins: lazy images keep the real URL in data-src.
                value = next((target.get(name) for name in rule["attribute"]
                              if target.get(name) and not str(target.get(name)).startswith("data:")), None)
                row[field] = " ".join(value) if isinstance(value, list) else value
            else:
                row[field] = target.get_text(" ", strip=True)
        rows.append(row)
    return rows


def _coerce(value, kind, base):
    if kind == "url":
        if not isinstance(value, str) or not value.strip():
            return None
        absolute = urljoin(base, value.strip())
        return absolute if urlparse(absolute).scheme in ("http", "https") else None
    if kind == "number":
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value
        found = _PRICE_NUMBER.search(str(value)) if value is not None else None
        if not found:
            return None
        number = float(found.group(0).replace(",", ""))
        return int(number) if number.is_integer() else number
    if kind == "text":
        return None if value is None else (" ".join(str(value).split()))[:_TEXT_MAX]
    if isinstance(value, str):
        return value.strip()[:_TEXT_MAX]
    if isinstance(value, (dict, list)):
        raw = json.dumps(value, ensure_ascii=False)
        return value if len(raw) <= 4 * _TEXT_MAX else None
    return value


def _map_fields(row, fields, base):
    return {name: _coerce(_walk(row, rule["path"]), rule["type"], base) for name, rule in fields.items()}


def _key(value):
    return str(value).strip().rstrip("/").lower() if value not in (None, "") else None


def _join(items, others, join, base):
    """Fill each item's blank fields from its partner row in a second source."""
    if join["on"] == "position":
        partners = list(zip(items, others))
    else:
        index = {}
        for row in others:
            key = _key(_coerce(_walk(row, join["on"]["right"]), "auto", base))
            if key is not None:
                index.setdefault(key, row)
        partners = [(item, index.get(_key(item.get(join["on"]["left"])))) for item in items]
    for item, row in partners:
        if row is None:
            continue
        for name, value in _map_fields(row, join["fields"], base).items():
            if item.get(name) in (None, "", [], {}):
                item[name] = value


# ---- storage ----

class SiteModuleRegistry(RouteRecipeRegistry):
    """Agent-authored modules in the operator's private store, beside route recipes.

    Shares the recipe store's private-file, locking and atomic-save rules.
    There is no bundled layer: FrankenSurf ships no site modules.
    """
    label = "Site module"

    @staticmethod
    def _decode(data, basis):
        if (not isinstance(data, dict) or set(data) != {"schema", "modules"}
                or data["schema"] != STORE_SCHEMA or not isinstance(data["modules"], list)):
            _invalid("Site module store is invalid")
        modules = [SiteModule.from_record(row) for row in data["modules"]]
        if len({module.id for module in modules}) != len(modules):
            _invalid("Site module store has duplicate IDs")
        return modules

    def _write(self, modules):
        self._save_document({"schema": STORE_SCHEMA, "modules": [module.record() for module in modules]},
                            prefix=".site-modules-")

    def _load(self):
        return self._load_operator()

    def put(self, raw):
        """Save a module (a new ID, or a new version of an existing one)."""
        module = raw if isinstance(raw, SiteModule) else SiteModule.from_record(raw)
        with self._mutation():
            modules = [item for item in self._load_operator() if item.id != module.id] + [module]
            self._write(modules)
        return module.metadata()

    def get(self, identifier):
        _identifier(identifier)
        module = next((item for item in self._load_operator() if item.id == identifier), None)
        if module is None:
            raise SiteModuleError(f"No site module {identifier!r}")
        return module

    def enable(self, identifier, enabled=True):
        if type(enabled) is not bool:
            _invalid()
        with self._mutation():
            modules = self._load_operator()
            if identifier not in {module.id for module in modules}:
                raise SiteModuleError(f"No site module {identifier!r}")
            self._write([replace(module, enabled=enabled) if module.id == identifier else module
                         for module in modules])

    def delete(self, identifier):
        _identifier(identifier)
        with self._mutation():
            modules = self._load_operator()
            if identifier not in {module.id for module in modules}:
                raise SiteModuleError(f"No site module {identifier!r}")
            self._write([module for module in modules if module.id != identifier])

    def inspect(self):
        return [module.metadata() for module in self._load_operator()]

    def match(self, url):
        """The enabled module for this URL: the longest path pattern wins a tie."""
        candidates = [module for module in self._load_operator() if module.enabled and module.matches(url)]
        return max(candidates, key=lambda module: len(module.match["path_pattern"]), default=None)

    def plan(self, *args, **kwargs):  # Modules never route: they only shape reads.
        raise NotImplementedError

    plan_paginate = plan


def receipt_record(module, source, output, validation, invalid=False):
    """What a read's receipt says about the module that shaped it."""
    record = {"id": module.id, "version": module.version, "sha256": module.fingerprint, "source": source,
              "items": output["count"] if output else 0}
    if output and output.get("next_url"):
        record["next_url"] = output["next_url"]
    if validation is not None:
        record["assertions"] = validation
        record["status"] = validation["status"]
    if invalid:
        record["status"] = "invalid"
    if record.get("status") in ("failed", "invalid") and source != "override":
        record["repair"] = ("The site may have changed. Draft the next version of this module and"
                            " propose it with propose_module_repair(trace_id, module): FrankenSurf checks"
                            " it against this page and a fresh read; the owner then promotes it.")
    return record


# ---- repair: the calling agent proposes, Core validates, the owner promotes ----

MODULE_PATCH = "module_patch"


def _fixture_result(content, url, final_url):
    """A retained page rebuilt into the fields a module reads."""
    from .runtime import _parse_builtin_content
    parsed = _parse_builtin_content(content, "text/html", final_url or url, None)
    return {"url": final_url or url, "content": content, "title": parsed.get("title"),
            "text": parsed.get("text") or "", "structured": parsed.get("structured")}


def _check(module, result, url, assertions, source):
    """Run a module over one page against the base module's assertions."""
    from .completeness import query_terms
    from .repair import evaluate_workload_assertions
    if module.invalid_page(result.get("title"), result.get("text"), result.get("url")):
        return {"source": source, "status": "failed", "reason": "invalid_page", "items": 0}
    output = module.extract(result, url, query_terms=query_terms(url))
    validation = evaluate_workload_assertions(output, assertions)
    return {"source": source, "status": validation["status"], "items": output["count"],
            "assertions": validation}


async def propose_module_repair(runtime, trace_id, candidate, *, run_live_canary=True):
    """Validate an agent-drafted next version of a module that failed on a read.

    The proposal must keep the module's ID and origin and use a new version.
    It is checked with the base module's own assertions (a fix can't weaken the
    contract) against the page the failing read retained and, when asked, a
    fresh independent read. Nothing changes until the owner promotes it with
    promote_repair(proposal_id, proposal_sha256).
    """
    from .repair import (REPAIR_PROPOSAL_SCHEMA, REPAIR_VALIDATION_SCHEMA, _canonical_json,
                         _evidence_rows, _json_artifact_bytes, _load_evidence, _write_json_artifact)
    trace = runtime.trace(trace_id)
    receipt = trace.get("receipt") or {}
    used = receipt.get("module") or {}
    if receipt.get("identity") or used.get("status") not in ("failed", "invalid") or used.get("source") == "override":
        raise SiteModuleError("Only a public read whose saved module failed can be repaired")
    base = runtime.site_modules.get(used["id"])
    if base.fingerprint != used.get("sha256"):
        raise SiteModuleError("The module changed after that read; read again with the current version")
    if base.assertions is None:
        raise SiteModuleError("A module needs assertions before it can be repaired")
    proposed = candidate if isinstance(candidate, SiteModule) else SiteModule.from_record(candidate)
    if proposed.id != base.id or proposed.match["origin"] != base.match["origin"]:
        raise SiteModuleError("A repair keeps the module's ID and origin")
    if proposed.version == base.version:
        raise SiteModuleError("A repair needs a new version")
    url = receipt.get("requested_url") or trace.get("url")
    checks = []
    for descriptor in _evidence_rows(receipt)[:4]:
        try:
            content = _load_evidence(descriptor, 20 * 1024 * 1024, state_dir=runtime.state_dir)
        except (OSError, ValueError):
            continue
        if "<" not in content[:2000]:
            continue
        page = _fixture_result(content, url, receipt.get("final_url"))
        checks.append({**_check(proposed, page, url, base.assertions, "retained_failure_fixture"),
                       "evidence": descriptor,
                       "base": _check(base, page, url, base.assertions, "base_on_fixture")["status"]})
    canary = {"status": "not_requested", "attempts": []}
    if run_live_canary:
        fresh = await runtime.read(url, module_override=proposed.record(),
                                   policy_overrides={"freshness": "now"})
        fresh_receipt = fresh.get("receipt") or {}
        attempt = {"trace_id": fresh_receipt.get("trace_id"), "method": fresh_receipt.get("method"),
                   "evidence": _evidence_rows(fresh_receipt)}
        if fresh_receipt.get("status") == "observed":
            attempt.update(_check(proposed, fresh, url, base.assertions, "independent_live_canary"))
        else:
            attempt.update(status="failed", failure=(fresh_receipt.get("failure") or {}).get("code"))
        canary = {"status": attempt["status"], "attempts": [attempt]}
    fixture_passed = any(item["status"] == "passed" for item in checks)
    state = ("canary_validated" if fixture_passed and canary["status"] == "passed"
             else "fixture_validated" if fixture_passed and not run_live_canary
             else "validation_failed")
    body = {"schema": REPAIR_PROPOSAL_SCHEMA, "version": 1, "kind": MODULE_PATCH,
            "target": {"operation": "read", "url": url, "failure_trace_id": trace_id, "identity_class": "public",
                       "module": {"id": base.id, "base_version": base.version, "base_sha256": base.fingerprint,
                                  "base_record": base.record()}},
            "change": proposed.record()}
    proposal_id = hashlib.sha256(_canonical_json(body)).hexdigest()
    validation = {"schema": REPAIR_VALIDATION_SCHEMA, "proposal_id": proposal_id,
                  "status": "passed" if state == "canary_validated" else "failed",
                  "original_assertions": base.assertions, "checks": checks, "live_canary": canary}
    proposal = {**body, "id": proposal_id, "state": state,
                "validation_sha256": hashlib.sha256(_json_artifact_bytes(validation)).hexdigest()}
    proposal_artifact = _write_json_artifact(runtime.state_dir, ("repairs", "proposals", proposal_id,
                                                                 "proposal.json"), proposal)
    _write_json_artifact(runtime.state_dir, ("repairs", "proposals", proposal_id, "validation.json"), validation)
    return {"status": "proposal_ready" if state == "canary_validated" else "validation_failed",
            "proposal": {"id": proposal_id, "sha256": proposal_artifact["sha256"], "state": state,
                         "module": proposed.id, "from_version": base.version, "to_version": proposed.version},
            "validation": validation,
            "promotion": {"automatic": False,
                          "required_action": ("Owner promotion: frankensurf repair-promote " + proposal_id
                                              + " --proposal-sha256 " + proposal_artifact["sha256"])
                          if state == "canary_validated" else "Revise the module and propose again"}}


def promote_module_patch(runtime, proposal, validation, validation_raw, expected_sha256):
    """Activate a validated module patch: re-check, save the new version, keep the old for rollback."""
    from .repair import _load_evidence
    target = proposal.get("target") or {}
    base_info = target.get("module") or {}
    if (validation.get("status") != "passed" or (validation.get("live_canary") or {}).get("status") != "passed"
            or target.get("identity_class") != "public"):
        raise ValueError("Promotion requires a passed fixture check and a passed live canary")
    current = runtime.site_modules.get(base_info.get("id"))
    if current.fingerprint != base_info.get("base_sha256"):
        raise ValueError("The module changed after the proposal was validated")
    proposed = SiteModule.from_record(proposal["change"])
    assertions = validation.get("original_assertions")
    url = target.get("url")
    passed = False
    for check in validation.get("checks") or ():
        try:
            content = _load_evidence(check["evidence"], 20 * 1024 * 1024, state_dir=runtime.state_dir)
        except (OSError, ValueError, KeyError, TypeError):
            continue
        page = _fixture_result(content, url, None)
        if _check(proposed, page, url, assertions, "promotion_fixture_revalidation")["status"] == "passed":
            passed = True
            break
    if not passed:
        raise ValueError("The retained page no longer validates the proposal")
    runtime.site_modules.put(proposed)
    return {"id": proposed.id, "version": proposed.version, "sha256": proposed.fingerprint,
            "previous": {"version": current.version, "sha256": current.fingerprint}}


__all__ = ["SCHEMA", "SiteModule", "SiteModuleError", "SiteModuleRegistry", "ACTIVE", "receipt_record"]
