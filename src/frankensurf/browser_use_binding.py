"""Trusted local model factories keep credentials outside agent tool arguments."""
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class ModelBinding:
    llm: object
    billing: str
    # None means unmeasured, including for unmetered/local models.
    measured_cost_usd: Callable[[], float | None] | None = None
    # A trusted conservative upper bound, not a measured cost. Required for
    # paid calls when the caller supplies max_cost_usd.
    next_call_cost_upper_bound: Callable[..., float] | None = None


def load_binding(source, name, expected_billing, *, source_bytes=None):
    from types import ModuleType
    # Execute immutable snapshot bytes instead of an mtime-based .pyc cache.
    # Advance binding revision when imported external settings change.
    module = ModuleType("frankensurf_operator_model")
    module.__file__ = str(source)
    exec(compile(source.read_bytes() if source_bytes is None else source_bytes, str(source), "exec"), module.__dict__)
    factory = getattr(module, name, None)
    if not callable(factory):
        raise ValueError("Model factory unavailable")
    binding = factory()
    if (not isinstance(binding, ModelBinding) or binding.billing not in {"paid", "unmetered"}
            or binding.billing != expected_billing or not callable(getattr(binding.llm, "ainvoke", None))
            or not isinstance(getattr(binding.llm, "model", None), str)
            or binding.measured_cost_usd is not None and not callable(binding.measured_cost_usd)
            or binding.next_call_cost_upper_bound is not None and not callable(binding.next_call_cost_upper_bound)):
        raise ValueError("Model binding metadata invalid")
    return binding
