import pytest
from frankensurf.runtime import WebPolicy


def test_caller_can_raise_image_and_settle_budgets():
    policy = WebPolicy(max_images=250, settle_ms=20000)
    assert policy.max_images == 250
    assert policy.settle_ms == 20000
    assert WebPolicy().max_images == 50
    assert WebPolicy().settle_ms == 400


@pytest.mark.parametrize("field", ["max_images", "settle_ms"])
@pytest.mark.parametrize("value", [-1, True, 1.5, "12", None])
def test_counts_require_nonnegative_integers(field, value):
    with pytest.raises(ValueError):
        WebPolicy(**{field: value})


@pytest.mark.parametrize("field", ["max_bytes", "max_image_bytes"])
@pytest.mark.parametrize("value", [0, -1, True, 1.5, None])
def test_byte_budgets_require_positive_integers(field, value):
    with pytest.raises(ValueError):
        WebPolicy(**{field: value})


@pytest.mark.parametrize("value", [0, -1, True, float("nan"), float("inf"), "25", None])
def test_timeout_requires_positive_finite_number(value):
    with pytest.raises(ValueError):
        WebPolicy(timeout_seconds=value)


@pytest.mark.parametrize("selector", ["", "   ", 1, "a\nb", "x" * 4097])
def test_content_readiness_selector_is_bounded_printable_css(selector):
    with pytest.raises(ValueError):
        WebPolicy(content_ready_selector=selector)


@pytest.mark.parametrize("seconds", [0, -1, True, float("nan"), float("inf"), "10", None])
def test_content_readiness_timeout_is_positive_and_fits_operation(seconds):
    with pytest.raises(ValueError):
        WebPolicy(content_ready_selector="#ready",
                  content_ready_timeout_seconds=seconds)


def test_content_readiness_timeout_cannot_outlive_operation():
    with pytest.raises(ValueError):
        WebPolicy(timeout_seconds=5, content_ready_selector="#ready",
                  content_ready_timeout_seconds=6)


@pytest.mark.parametrize("value", [0, -1, 1.5, True])
def test_provider_attempts_per_candidate_must_be_a_positive_integer(value):
    with pytest.raises(ValueError):
        WebPolicy(provider_max_attempts_per_candidate=value)


def test_nested_timeout_clamps_content_readiness_budget():
    from dataclasses import replace
    from types import SimpleNamespace
    from frankensurf.providers import _nested_policy
    parent = WebPolicy(content_ready_selector="#ready",
                       content_ready_timeout_seconds=20,
                       timeout_seconds=30)
    requested = replace(parent, timeout_seconds=5,
                        content_ready_timeout_seconds=5)
    nested = _nested_policy(
        SimpleNamespace(policy=parent, children=[]), requested, "camoufox")
    assert nested.timeout_seconds == 5
    assert nested.content_ready_timeout_seconds == 5


def test_nested_provider_cannot_reduce_parent_retry_delay():
    from dataclasses import replace
    from types import SimpleNamespace
    from frankensurf.providers import _nested_policy
    parent = WebPolicy(provider_retry_delay_seconds=0.2)
    requested = replace(parent, provider_retry_delay_seconds=0)
    nested = _nested_policy(
        SimpleNamespace(policy=parent, children=[]), requested, "camoufox")
    assert nested.provider_retry_delay_seconds == 0.2


def test_browser_agent_defaults_include_click_and_search_and_accept_a_task():
    policy = WebPolicy()
    assert {"click_element", "search_site"} <= set(policy.browser_agent_allowed_actions)
    assert policy.browser_agent_task is None
    assert WebPolicy(browser_agent_task="search for sony a7iii").browser_agent_task
    for bad in ("", "   ", "x" * 2001, 5):
        with pytest.raises(ValueError):
            WebPolicy(browser_agent_task=bad)


def test_default_retry_is_one_retry_of_free_providers_on_provider_down():
    policy = WebPolicy()
    assert policy.provider_max_attempts_per_candidate == 2
    assert policy.provider_retry_failures == ("PROVIDER_DOWN",)
