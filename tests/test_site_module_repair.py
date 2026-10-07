"""A failing site module: the agent proposes the next version, Core validates, the owner promotes."""
import copy

import pytest

from frankensurf.site_modules import SiteModuleError
from frankensurf.runtime import Runtime

from test_site_modules import MODULE, ORIGIN, PAGE, POLICY, install

# The site renamed its embedded results object: version 1 finds nothing.
MOVED = PAGE.replace("window.__RESULTS__", "window.__SEARCH_STATE__")


def next_version(marker="window.__SEARCH_STATE__ =", version="2"):
    module = copy.deepcopy(MODULE)
    module["version"] = version
    module["sources"]["hits"]["marker"] = marker
    return module


async def failing_read(web):
    web.site_modules.put(MODULE)
    result = await web.read(ORIGIN + "/search?q=camera", policy_overrides={**POLICY, "completeness_ladder": []})
    assert result["receipt"]["module"]["status"] == "failed"
    assert "propose_module_repair" in result["receipt"]["module"]["repair"]
    return result["receipt"]["trace_id"]


async def test_propose_validate_promote_and_roll_back(tmp_path, monkeypatch):
    install(monkeypatch, MOVED, MOVED)
    async with Runtime(tmp_path) as web:
        trace_id = await failing_read(web)
        proposed = await web.propose_module_repair(trace_id, next_version())
        assert proposed["status"] == "proposal_ready"
        assert proposed["proposal"]["state"] == "canary_validated"
        checks = proposed["validation"]["checks"]
        assert checks and checks[0]["status"] == "passed" and checks[0]["base"] == "failed"
        assert proposed["validation"]["live_canary"]["status"] == "passed"
        # Proposing changes nothing: the saved module is still version 1.
        assert web.site_modules.get(MODULE["id"]).version == "1"

        promoted = web.promote_repair(proposed["proposal"]["id"], proposed["proposal"]["sha256"])
        assert promoted["status"] == "promoted" and promoted["module"]["version"] == "2"
        assert web.site_modules.get(MODULE["id"]).version == "2"
        healed = await web.read(ORIGIN + "/search?q=camera", policy_overrides={**POLICY, "freshness": "now"})
        assert healed["receipt"]["module"]["status"] == "passed" and len(healed["items"]) == 6

        web.disable_repair(proposed["proposal"]["id"], "checking the rollback")
        assert web.site_modules.get(MODULE["id"]).version == "1"


async def test_a_fix_is_held_to_the_old_versions_assertions(tmp_path, monkeypatch):
    install(monkeypatch, MOVED, MOVED)
    async with Runtime(tmp_path) as web:
        trace_id = await failing_read(web)
        # Dropping the assertions doesn't help: the base module's checks still apply.
        weaker = next_version(marker="window.__NOTHING__ =")
        weaker.pop("assertions")
        proposed = await web.propose_module_repair(trace_id, weaker)
        assert proposed["status"] == "validation_failed"
        with pytest.raises(ValueError):
            web.promote_repair(proposed["proposal"]["id"], proposed["proposal"]["sha256"])
        assert web.site_modules.get(MODULE["id"]).version == "1"


async def test_repairs_need_a_failed_module_a_new_version_and_the_same_id(tmp_path, monkeypatch):
    install(monkeypatch, MOVED, MOVED)
    async with Runtime(tmp_path) as web:
        trace_id = await failing_read(web)
        with pytest.raises(SiteModuleError):
            await web.propose_module_repair(trace_id, next_version(version="1"))
        with pytest.raises(SiteModuleError):
            await web.propose_module_repair(trace_id, {**next_version(), "id": "other.module"})
        # Once the module changes, the old failure no longer describes it.
        web.site_modules.put(next_version(version="3"))
        with pytest.raises(SiteModuleError):
            await web.propose_module_repair(trace_id, next_version(version="4"))


async def test_a_passing_read_is_not_repairable(tmp_path, monkeypatch):
    install(monkeypatch, PAGE, PAGE)
    async with Runtime(tmp_path) as web:
        web.site_modules.put(MODULE)
        result = await web.read(ORIGIN + "/search?q=camera", policy_overrides=POLICY)
        assert "repair" not in result["receipt"]["module"]
        with pytest.raises(SiteModuleError):
            await web.propose_module_repair(result["receipt"]["trace_id"], next_version())
