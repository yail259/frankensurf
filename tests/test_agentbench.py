import asyncio
import importlib.util
import json
import sys
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "agentbench", Path(__file__).resolve().parents[1] / "scripts" / "agentbench.py")
agentbench = importlib.util.module_from_spec(SPEC)
sys.modules["agentbench"] = agentbench  # dataclasses resolve their module here
SPEC.loader.exec_module(agentbench)

TASK = {"id": "t", "question": "q?", "check": {"type": "number", "value": 2018}}


def test_grading_reads_the_last_answer_line():
    assert agentbench.final_answer("thinking...\nANSWER: 2017\nANSWER: 2018") == "2018"
    assert agentbench.grade(TASK, "It was 2018.")
    assert not agentbench.grade(TASK, "2019")
    assert agentbench.grade({"check": {"type": "contains_any", "values": ["HTTP Semantics"]}}, "http semantics")
    assert agentbench.grade({"check": {"type": "regex", "pattern": r"\b3\.11\b"}}, "Python 3.11")
    assert not agentbench.grade(TASK, None)


class FakeWeb:
    def __init__(self, status="observed"):
        self.status, self.reads = status, []

    async def read(self, url, policy_overrides=None):
        self.reads.append((url, policy_overrides))
        return {"title": "Sony a7 III", "url": url, "text": "Announced in 2018.",
                "receipt": {"status": self.status, "cost_usd": 0.004,
                            **({} if self.status == "observed" else {"failure": {"code": "BLOCKED"}})}}

    async def search(self, query, limit=8, policy=None):
        return {"results": [], "receipt": {"status": "observed", "cost_usd": 0}}


class ScriptedAdapter:
    """Replays model turns: first a tool call, then a final answer."""
    label = "fake:model"

    def __init__(self, turns):
        self.turns = list(turns)
        self.results = []

    def start(self, question):
        return [question]

    async def step(self, messages):
        return self.turns.pop(0)

    def add_results(self, messages, results):
        self.results.extend(results)

    def cost(self, turns):
        return 0.01 * len(turns)


def turns(answer="ANSWER: 2018"):
    return [agentbench.Turn("", [("c1", "web_read", {"url": "https://en.wikipedia.org/wiki/Sony_a7_III"})], 100, 20),
            agentbench.Turn(answer, [], 300, 10)]


def test_task_passes_when_the_agent_browses_and_answers_correctly():
    web = FakeWeb()
    adapter = ScriptedAdapter(turns())
    row = asyncio.run(agentbench.run_task(adapter, TASK, web, paid=True))
    assert row["success"] and row["browsed"] and row["outcome"] == "answered"
    assert row["input_tokens"] == 400 and row["output_tokens"] == 30
    assert row["frankensurf_cost_usd"] == 0.004 and row["model_cost_usd"] == 0.02
    assert web.reads[0][1] == {"prefer_markdown": True, "allow_paid_fallbacks": True}
    assert "Announced in 2018" in adapter.results[0][1]


def test_answer_from_memory_without_a_successful_read_does_not_count():
    row = asyncio.run(agentbench.run_task(
        ScriptedAdapter([agentbench.Turn("ANSWER: 2018", [], 50, 5)]), TASK, FakeWeb(), paid=False))
    assert row["correct"] and not row["browsed"] and not row["success"]
    blocked = asyncio.run(agentbench.run_task(ScriptedAdapter(turns()), TASK, FakeWeb("failed"), paid=False))
    assert blocked["tool_failures"] == ["BLOCKED"] and not blocked["success"]


def test_one_format_reminder_then_answer():
    class Reminding(ScriptedAdapter):
        reminded = 0

        def remind(self, messages):
            self.reminded += 1

    adapter = Reminding(turns("The answer is 2018.") + [agentbench.Turn("ANSWER: 2018", [], 10, 2)])
    row = asyncio.run(agentbench.run_task(adapter, TASK, FakeWeb(), paid=False))
    assert adapter.reminded == 1 and row["reminded"] and row["success"]


def test_refusal_and_turn_limit_are_reported():
    refused = asyncio.run(agentbench.run_task(
        ScriptedAdapter([agentbench.Turn("", [], 10, 1, stop="refusal")]), TASK, FakeWeb(), paid=False))
    assert refused["outcome"] == "refusal"
    looping = [agentbench.Turn("", [("c", "web_read", {"url": "https://x.example"})], 1, 1)] * 3
    limited = asyncio.run(agentbench.run_task(ScriptedAdapter(looping), TASK, FakeWeb(), paid=False, max_turns=3))
    assert limited["outcome"] == "turn_limit"


def test_summary_reports_completion_tokens_and_model_gap():
    rows = [
        {"model": "a", "success": True, "correct": True, "browsed": True, "outcome": "answered",
         "input_tokens": 1000, "output_tokens": 100, "model_cost_usd": 0.01, "frankensurf_cost_usd": 0, "latency_ms": 900},
        {"model": "a", "success": False, "correct": True, "browsed": False, "outcome": "answered",
         "input_tokens": 500, "output_tokens": 50, "model_cost_usd": 0.01, "frankensurf_cost_usd": 0, "latency_ms": 100},
        {"model": "b", "success": True, "correct": True, "browsed": True, "outcome": "answered",
         "input_tokens": 3000, "output_tokens": 300, "model_cost_usd": None, "frankensurf_cost_usd": 0.004, "latency_ms": 2000},
    ]
    summary = agentbench.summarize(rows)
    assert summary["models"]["a"]["completion"] == 0.5 and summary["models"]["b"]["completion"] == 1.0
    assert summary["models"]["a"]["correct_without_browsing"] == 1
    assert summary["model_gap_points"] == 50.0
    assert agentbench.summarize(rows[:2])["model_gap_points"].startswith("unmeasured")


def test_openrouter_reports_its_own_cost_and_refuses_claude(monkeypatch):
    import pytest
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    adapter = agentbench.adapter_for("openrouter:openai/gpt-6.1-sol", "medium", False)
    assert adapter.label == "openrouter:openai/gpt-6.1-sol"
    assert adapter.cost([agentbench.Turn("", [], cost_usd=0.01), agentbench.Turn("", [], cost_usd=0.02)]) == 0.03
    assert adapter.cost([agentbench.Turn("", [])]) is None
    with pytest.raises(SystemExit):
        agentbench.adapter_for("openrouter:anthropic/claude-opus-5.5", "medium", False)


def test_task_file_is_well_formed():
    tasks = json.loads(agentbench.TASKS.read_text())["tasks"]
    assert len({t["id"] for t in tasks}) == len(tasks) >= 10
    for task in tasks:
        assert task["question"] and task["check"]["type"] in {"contains_any", "regex", "number"}
