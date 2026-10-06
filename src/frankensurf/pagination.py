"""Bounded same-origin traversal with raw pages and identity-based deduplication."""
from copy import deepcopy
from dataclasses import dataclass, replace
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import time
import uuid
from urllib.parse import parse_qsl, urlparse


def _merge_listing_page(result, first_seen, rows, number, target):
    listings = result["listings"]
    before_count = len(listings)
    before_overlap = result["overlap_count"]
    for row in rows:
        identifier = row["listing_id"]
        if identifier in first_seen:
            result["overlap_count"] += 1
            original = first_seen[identifier]
            result["overlaps"].append({
                "listing_id": identifier,
                "first_page_number": original["page_number"],
                "repeated_page_number": number,
                "first_url": original["url"], "repeated_url": target,
                "first_promotion_claim": deepcopy(
                    original["row"].get("promotion_claim")),
                "repeated_promotion_claim": deepcopy(
                    row.get("promotion_claim")),
                "first_result_type_claim": deepcopy(
                    original["row"].get("result_type_claim")),
                "repeated_result_type_claim": deepcopy(
                    row.get("result_type_claim")),
            })
        else:
            listings.append(deepcopy(row))
            first_seen[identifier] = {
                "page_number": number, "url": target,
                "row": deepcopy(row)}
    result["page_progress"].append({
        "page_number": number, "url": target,
        "raw_count": len(rows), "new_count": len(listings) - before_count,
        "overlap_count": result["overlap_count"] - before_overlap,
        "unique_total": len(listings),
        "made_progress": len(listings) > before_count})


async def _ordinary_paginate(web, url, adapter, policy, *,
                             continuation_adapter=None,
                             policy_overrides=None, prior_receipts=(),
                             acquisition_budget=True):
    from .runtime import WebFailure
    pages, listings, seen_urls = [], [], set()
    first_seen = {}
    target = url
    source = urlparse(url)
    budget_receipts = list(prior_receipts)
    result = {"pages": pages, "listings": listings, "overlap_count": 0, "overlaps": [], "page_progress": [],
              "status": "page_limit_reached", "catalogue_complete": None,
              "next_url": target, "failure": None}
    for number in range(policy.max_pages):
        if target in seen_urls:
            result.update(status="continuation_cycle", failure="CONTENT_MISMATCH")
            break
        seen_urls.add(target)
        effective = policy
        if acquisition_budget and policy.max_cost_usd is not None:
            try:
                spent = _total_cost(budget_receipts)
            except WebFailure as exc:
                result.update(status="failed", failure={"code": exc.code, "message": exc.message})
                break
            if spent is None or spent > policy.max_cost_usd or (spent == policy.max_cost_usd and spent > 0):
                result.update(status="failed", failure={"code": "BUDGET_EXHAUSTED", "message": "No verified aggregate acquisition budget remains"})
                break
            effective = replace(policy, max_cost_usd=_remaining_cost(policy.max_cost_usd, spent))
        leaf = continuation_adapter if number and continuation_adapter else adapter
        if policy_overrides is None:
            page = await web.extract(target, leaf, effective)
        else:
            overrides = {key: getattr(effective, key) for key in policy_overrides}
            if effective.max_cost_usd is not None:
                overrides["max_cost_usd"] = effective.max_cost_usd
            page = await web.extract(target, leaf, policy_overrides=overrides)
        pages.append(page)
        budget_receipts.append(page["receipt"])
        if acquisition_budget and policy.max_cost_usd is not None:
            try:
                total = _total_cost(budget_receipts)
            except WebFailure as exc:
                result.update(status="failed", failure={"code": exc.code, "message": exc.message})
                break
            if total is None or total > policy.max_cost_usd:
                result.update(status="failed", failure={"code": "BUDGET_EXHAUSTED", "message": "Acquisition cost cannot satisfy aggregate cap"})
                break
        if page["receipt"]["status"] != "observed":
            result.update(status="failed", failure=page["receipt"].get("failure"))
            break
        structured = page.get("structured")
        rows = structured.get("listings") if isinstance(structured, dict) else None
        if (not isinstance(rows, list)
                or not rows
                or any(not isinstance(row, dict) or not isinstance(row.get("listing_id"), str)
                       or not row["listing_id"] for row in rows)):
            result.update(status="content_mismatch", failure="SCHEMA_CHANGED")
            break
        _merge_listing_page(result, first_seen, rows, number + 1, target)
        target = structured.get("next_url")
        result["next_url"] = target
        if target is None:
            result["status"] = "continuation_exhausted"
            break
        try:
            destination = (urlparse(target) if isinstance(target, str)
                and target == target.strip()
                and not any(ord(char) < 32 or ord(char) == 127
                            for char in target) else None)
            valid = (destination and destination.scheme == "https"
                     and destination.netloc == source.netloc
                     and destination.hostname == source.hostname
                     and destination.port == source.port
                     and not destination.username and not destination.password
                     and not destination.params and not destination.fragment)
        except ValueError:
            valid = False
        if not valid:
            result.update(status="invalid_continuation", failure="CONTENT_MISMATCH", next_url=None)
            break
        if target in seen_urls:
            result.update(
                status="continuation_cycle",
                failure={"code": "CONTENT_MISMATCH",
                         "message": "Continuation repeated an acquired URL"})
            break
    return result


@dataclass(frozen=True)
class SequencePage:
    """A leaf source bound to one indexed page in retained acquisition JSON."""
    source_index: int
    url: str
    content: str
    content_type: str
    http_status: int | None = None
    request_url: str | None = None


def native_sequence_pages(request, *, default_content_type=None):
    """Optional native adapter protocol; it does not change ordinary projection."""
    from .runtime import WebFailure
    try:
        packet = json.loads(request.content)
        pages = packet["pages"]
        if not isinstance(pages, list):
            raise ValueError()
        captures = tuple(SequencePage(index, page["url"], page["content"],
            page.get("content_type", default_content_type), page.get("http_status"), page.get("request_url"))
            for index, page in enumerate(pages))
    except (ValueError, TypeError, KeyError):
        raise WebFailure("SCHEMA_CHANGED", "Invalid native indexed-page envelope") from None
    return validate_sequence_pages(request, captures)


def validate_sequence_pages(request, captures):
    from .runtime import WebFailure, _validate_url, _status_failure
    policy = request.policy
    try:
        packet = json.loads(request.content)
        sources = packet["pages"]
        if (not isinstance(sources, list) or not isinstance(captures, (tuple, list))
                or not captures or len(captures) != len(sources)):
            raise ValueError()
        if len(captures) > policy.max_pages:
            raise WebFailure("LIMIT_EXCEEDED", "Native sequence exceeds caller page budget")
        total_bytes = 0
        for index, capture in enumerate(captures):
            source = sources[index]
            if (not isinstance(capture, SequencePage) or type(capture.source_index) is not int
                    or capture.source_index != index or not isinstance(source, dict)
                    or not isinstance(capture.url, str) or not isinstance(capture.content, str)
                    or not isinstance(capture.content_type, str) or not capture.content_type.strip()
                    or capture.url != source["url"] or capture.content != source["content"]
                    or "content_type" in source and capture.content_type != source["content_type"]
                    or capture.http_status != source.get("http_status")
                    or capture.request_url != source.get("request_url")):
                raise ValueError()
            _validate_url(capture.url)
            if capture.request_url is not None:
                _validate_url(capture.request_url)
            if capture.http_status is not None:
                if type(capture.http_status) is not int:
                    raise ValueError()
                if failure := _status_failure(capture.http_status):
                    raise WebFailure(failure, "Retained native page response failed", capture.http_status,
                                     response_url=capture.url)
            total_bytes += len(capture.content.encode())
        if total_bytes > policy.max_bytes or len(request.content.encode()) > policy.max_bytes:
            raise WebFailure("LIMIT_EXCEEDED", "Native capture exceeds caller byte budget")
    except (ValueError, TypeError, KeyError, AttributeError):
        raise WebFailure("SCHEMA_CHANGED", "Native capture is not bound to retained indexed source") from None
    return tuple(captures)


def _same_url(left, right):
    """Unique query keys can reorder; scope, values and paths cannot change."""
    try:
        a, b = urlparse(left), urlparse(right)
        pairs_a = parse_qsl(a.query, keep_blank_values=True)
        pairs_b = parse_qsl(b.query, keep_blank_values=True)
        return (a.scheme == b.scheme == "https" and a.netloc == b.netloc and a.path == b.path
                and a.params == b.params and not a.username and not b.username
                and not a.password and not b.password and not a.fragment and not b.fragment
                and len(pairs_a) == len({key for key, _ in pairs_a})
                and len(pairs_b) == len({key for key, _ in pairs_b})
                and sorted(pairs_a) == sorted(pairs_b))
    except (ValueError, TypeError, AttributeError):
        return False


def _cost(receipt):
    from .runtime import WebFailure
    value = receipt.get("cost_usd")
    if value is None:
        return None
    try:
        valid = type(value) in (int, float) and math.isfinite(value) and value >= 0
    except (ValueError, OverflowError):
        valid = False
    if not valid:
        raise WebFailure("SCHEMA_CHANGED", "Invalid acquisition cost")
    return value


def _total_cost(receipts):
    unique = {}
    for receipt in receipts:
        unique.setdefault(receipt.get("trace_id") or id(receipt), receipt)
    values = [_cost(receipt) for receipt in unique.values()]
    if any(value is None for value in values):
        return None
    total = float(sum((Decimal(str(value)) for value in values), Decimal(0)))
    from .runtime import WebFailure
    try:
        valid = math.isfinite(total)
    except (ValueError, OverflowError):
        valid = False
    if not valid:
        raise WebFailure("SCHEMA_CHANGED", "Aggregate acquisition cost is not finite")
    return total


def _remaining_cost(cap, spent):
    return float(Decimal(str(cap)) - Decimal(str(spent)))


def _empty_failure(error):
    return {"pages": [], "listings": [], "overlap_count": 0, "overlaps": [], "page_progress": [],
            "status": "failed", "catalogue_complete": None, "next_url": None,
            "failure": {"code": error.code, "message": error.message}}


def _page_receipt(acquisition, plan, number, leaf, version, capture=None):
    receipt = deepcopy(acquisition["receipt"])
    shared = receipt["trace_id"]
    receipt.update(operation="extract", adapter=leaf, adapter_version=version,
        acquisition_adapter=plan.acquisition_adapter,
        acquisition_adapter_version=plan.recipe.acquisition_adapter_version,
        acquisition_trace_id=shared, trace_id=uuid.uuid4().hex,
        cost_usd=receipt.get("cost_usd") if number == 0 else 0,
        latency_ms=receipt.get("latency_ms") if number == 0 else 0,
        attempts=receipt.get("attempts", []) if number == 0 else [],
        cost_basis="shared acquisition " + shared + "; total allocated once to first page projection",
        receipt_basis="leaf projection of retained native acquisition", projection_performed=False)
    if capture is not None:
        receipt.update(http_status=capture.http_status, final_url=capture.url,
            source_page={"index": capture.source_index, "url": capture.url,
                "request_url": capture.request_url, "content_type": capture.content_type,
                "content_sha256": hashlib.sha256(capture.content.encode()).hexdigest(),
                "artifact_sha256": receipt["evidence"][0]["sha256"],
                "evidence_pointer": "/pages/" + str(capture.source_index) + "/content"})
    return receipt


async def _native_paginate(web, url, adapter, continuation_adapter, plan, policy):
    from .adapters import AdapterRequest
    from .runtime import WebFailure, parse_content
    acquisition = await web._read(url, policy, provider=plan.provider, adapter=plan.acquisition_adapter,
        _route_scope=plan.recipe.fingerprint, _recipe=plan.recipe.metadata())
    receipt = acquisition["receipt"]
    first_receipt = _page_receipt(acquisition, plan, 0, adapter, plan.recipe.adapter_version)
    try:
        cost = _cost(receipt)
        cap = policy.max_cost_usd
        if cap is not None and (cost is None or cost > cap):
            raise WebFailure("BUDGET_EXHAUSTED", "Native acquisition cost cannot satisfy the remaining aggregate cap")
        if receipt["status"] != "observed":
            failure = receipt.get("failure") or {}
            raise WebFailure(failure.get("code", "UNKNOWN"), failure.get("message", "Native acquisition failed"))
        if (receipt.get("adapter") != plan.acquisition_adapter
                or receipt.get("adapter_version") != plan.recipe.acquisition_adapter_version
                or receipt.get("provider_version") != plan.recipe.provider_version
                or receipt.get("method") != plan.provider
                or web.adapters.require_sequence_enabled(plan.acquisition_adapter).version != plan.recipe.acquisition_adapter_version
                or web.providers.require_enabled(plan.provider, policy).version != plan.recipe.provider_version):
            raise WebFailure("SCHEMA_CHANGED", "Acquisition versions no longer match the configured recipe")
        if not _same_url(url, acquisition["url"]):
            raise WebFailure("CONTENT_MISMATCH", "Native acquisition changed initial source scope")
        try:
            reference = receipt["evidence"][0]
            raw = Path(reference["path"]).read_bytes()
            if (hashlib.sha256(raw).hexdigest() != reference["sha256"]
                    or len(raw) != reference["bytes"] or len(raw) > policy.max_bytes
                    or json.loads(raw) != json.loads(acquisition["content"])):
                raise ValueError()
        except (OSError, KeyError, IndexError, TypeError, ValueError):
            raise WebFailure("SCHEMA_CHANGED", "Native content is not bound to retained acquisition evidence") from None
        request = AdapterRequest(acquisition["content"], acquisition["content_type"], acquisition["url"],
                                 policy=policy, requested_url=url)
        captures = web.adapters.sequence_pages(plan.acquisition_adapter, request)
    except WebFailure as exc:
        result = _empty_failure(exc)
        first_receipt.update(status="failed", failure=result["failure"])
        result["pages"].append({"url": url, "structured": None, "receipt": first_receipt})
        return result, receipt

    class CapturedPages:
        position = 0
        async def extract(self, target, leaf, leaf_policy):
            number = self.position
            self.position += 1
            capture = captures[number] if number < len(captures) else None
            version = plan.recipe.adapter_version if number == 0 else plan.recipe.continuation_adapter_version
            page_receipt = _page_receipt(acquisition, plan, number, leaf, version, capture)
            try:
                if capture is None:
                    raise WebFailure("SCHEMA_CHANGED", "Native sequence ended before its leaf continuation")
                if not _same_url(target, capture.url):
                    raise WebFailure("CONTENT_MISMATCH", "Native source differs from the requested leaf continuation")
                if web.adapters.require_enabled(leaf).version != version:
                    raise WebFailure("SCHEMA_CHANGED", "Requested leaf adapter version changed during traversal")
                parsed = parse_content(capture.content, capture.content_type, capture.url, leaf,
                    policy=leaf_policy, requested_url=target,
                    adapter_registry=web.adapters)
                page_receipt.update(status="observed", failure=None, requested_url=target,
                                    projection_performed=True)
                return {"url": capture.url, "content": capture.content, "content_type": capture.content_type,
                        **parsed, "field_status": {"availability": "unknown", "transaction_price": "unknown", "content": "observed"},
                        "receipt": page_receipt}
            except WebFailure as exc:
                page_receipt.update(status="failed", failure={"code": exc.code, "message": exc.message}, requested_url=target)
                return {"url": target, "structured": None, "receipt": page_receipt}
    result = await _ordinary_paginate(CapturedPages(), url, adapter, policy,
                                      continuation_adapter=continuation_adapter, acquisition_budget=False)
    result["native_sequence"] = True
    return result, receipt


def _attach_receipt(web, result, url, adapter, continuation_adapter, policy,
                    receipts, planning, started):
    from .runtime import WebFailure, utcnow
    try:
        total = _total_cost(receipts)
    except WebFailure as exc:
        total = None
        result.update(status="failed", failure={"code": exc.code, "message": exc.message})
    cap = policy.max_cost_usd
    if cap is not None and (total is None or total > cap):
        result.update(status="failed", failure={"code": "BUDGET_EXHAUSTED", "message": "Aggregate acquisition cost cannot satisfy caller cap"})
    failure = result.get("failure")
    if isinstance(failure, str):
        failure = {"code": failure}
    success = result["status"] in {"page_limit_reached", "continuation_exhausted"} and failure is None
    versions = {page["receipt"].get("adapter"): page["receipt"].get("adapter_version") for page in result["pages"]}
    selected_adapter = adapter or next((
        page["receipt"].get("adapter") for page in result["pages"]
        if isinstance(page.get("receipt"), dict)), None)
    evidence = {item["sha256"]: item for receipt in receipts
                for item in receipt.get("evidence", []) if isinstance(item, dict) and "sha256" in item}
    native = next((item for item in reversed(receipts)
                   if item.get("route_recipe", {}).get("representation") == "native_sequence"), None)
    configured = native.get("route_recipe", {}) if native else {}
    receipt = {"trace_id": uuid.uuid4().hex, "operation": "paginate", "status": "observed" if success else "failed",
        "requested_url": url, "final_url": result["pages"][-1].get("url") if result["pages"] else url,
        "adapter": selected_adapter, "adapter_version": versions.get(selected_adapter, configured.get("adapter_version")),
        "continuation_adapter": continuation_adapter or selected_adapter,
        "continuation_adapter_version": versions.get(continuation_adapter or selected_adapter, configured.get("continuation_adapter_version")),
        "acquisition_adapter": native.get("adapter") if native else None,
        "acquisition_adapter_version": native.get("adapter_version") if native else None,
        "acquisitions": deepcopy(receipts),
        "page_receipts": [deepcopy(page["receipt"]) for page in result["pages"]],
        "attempts": [attempt for item in receipts for attempt in item.get("attempts", [])],
        "identity": policy.identity, "cache_hit": bool(receipts) and all(item.get("cache_hit") for item in receipts),
        "evidence": list(evidence.values()), "cost_usd": total,
        "cost_basis": "aggregate of distinct acquisition traces; page projections do not add acquisition cost",
        "max_cost_usd": cap, "cost_budget_satisfied": (total <= cap if total is not None else False) if cap is not None else None,
        "observed_at": utcnow(), "freshness_seconds": None,
        "latency_ms": round((time.monotonic() - started) * 1000), "failure": failure,
        "routing": {"operator_recipes": {"skipped": list(planning.skipped), "automatic_promotion": False}}}
    result["receipt"] = receipt
    web._save_trace({"url": url, "receipt": receipt, "structured": {
        "status": result["status"], "overlap_count": result["overlap_count"],
        "page_progress": result["page_progress"], "catalogue_complete": None}}, record_observations=False)
    return result


async def paginate(web, url, adapter, policy, *,
                   continuation_adapter=None, protected_fields=frozenset(),
                   policy_overrides=None):
    from .routes import RouteRecipeError, RecipePlanning
    from .runtime import WebFailure, _validate_url
    started = time.monotonic()
    try:
        _validate_url(url)
        planning = web.routes.plan_paginate(url, adapter, continuation_adapter,
            policy, protected_fields, providers=getattr(web, "providers", None),
            adapters=getattr(web, "adapters", None))
    except (RouteRecipeError, WebFailure) as exc:
        error = exc if isinstance(exc, WebFailure) else WebFailure(exc.code, str(exc))
        return _attach_receipt(web, _empty_failure(error), url, adapter, continuation_adapter,
                               policy, [], RecipePlanning(), started)
    if not planning.plans and not planning.skipped:
        result = await _ordinary_paginate(web, url, adapter, policy, continuation_adapter=continuation_adapter,
                                         policy_overrides=policy_overrides)
        if policy.max_cost_usd is None:
            return result
        return _attach_receipt(web, result, url, adapter, continuation_adapter, policy,
                               [page["receipt"] for page in result["pages"]], planning, started)
    receipts = []
    budget_policy = policy
    receipt_continuation = continuation_adapter
    for plan in planning.plans:
        effective = plan.policy
        if budget_policy.max_cost_usd is None and effective.max_cost_usd is not None:
            budget_policy = replace(budget_policy, max_cost_usd=effective.max_cost_usd)
        caps = [value for value in (effective.max_cost_usd, budget_policy.max_cost_usd) if value is not None]
        cap = min(caps) if caps else None
        if cap is not None:
            try:
                spent = _total_cost(receipts)
            except WebFailure as exc:
                result = _empty_failure(exc)
                break
            if spent is None or spent > cap or (spent == cap and spent > 0):
                result = _empty_failure(WebFailure("BUDGET_EXHAUSTED", "No verified native acquisition budget remains"))
                break
            effective = replace(effective, max_cost_usd=_remaining_cost(cap, spent))
        selected_continuation = continuation_adapter or plan.recipe.continuation_adapter
        result, acquisition = await _native_paginate(web, url, adapter, selected_continuation, plan, effective)
        receipts.append(acquisition)
        failure = result.get("failure")
        code = failure.get("code") if isinstance(failure, dict) else failure
        if result["status"] in {"page_limit_reached", "continuation_exhausted"} or code == "BUDGET_EXHAUSTED" or code in policy.terminal_failures or code in policy.context_stop_failures:
            receipt_continuation = selected_continuation
            break
    else:
        result = await _ordinary_paginate(web, url, adapter, budget_policy, continuation_adapter=continuation_adapter,
                                         policy_overrides=policy_overrides, prior_receipts=receipts)
        receipts.extend(page["receipt"] for page in result["pages"])
    return _attach_receipt(web, result, url, adapter, receipt_continuation,
                           budget_policy, receipts, planning, started)
