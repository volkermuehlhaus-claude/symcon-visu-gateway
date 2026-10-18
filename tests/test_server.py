from __future__ import annotations

import hashlib
import json

import pytest
from fastmcp import Client

from visu_gateway.config import ConfigError, load_config
from visu_gateway.guard import GuardedClient
from visu_gateway.server import HashedTokenVerifier, build_server


async def call(mcp, tool: str, **args):
    async with Client(mcp) as c:
        res = await c.call_tool(tool, args, raise_on_error=False)
    return res.content[0].text


@pytest.fixture
def mcp(guard):
    return build_server(guard)


async def test_tool_list_has_no_dangerous_tools(mcp):
    async with Client(mcp) as c:
        names = {t.name for t in await c.list_tools()}
    assert names, "keine Tools registriert"
    assert all(n.startswith("visu_") for n in names)
    for bad in ("ips_call", "script", "config", "run", "set_value", "request_action", "export", "import"):
        assert not any(bad in n for n in names), bad


async def test_read_tools(mcp):
    out = json.loads(await call(mcp, "visu_list_children", object_id=100))
    assert out["count"] == 1 and out["children"][0]["name"] == "Büro"
    tree = json.loads(await call(mcp, "visu_get_tree", root_id=500, max_depth=3))
    assert tree["children"][0]["children"][0]["type"] == "Link"
    var = json.loads(await call(mcp, "visu_get_variable", variable_id=110))
    assert var["value"] == "21,5" and "VariableValue" not in var["metadata"]


async def test_get_links_resolves_targets(mcp):
    out = json.loads(await call(mcp, "visu_get_links", parent_id=501))
    assert out["count"] == 1
    link = out["links"][0]
    assert link["target_id"] == 110 and link["target_name"] == "Temperatur" and link["formatted"] == "21,5"


async def test_secret_variable_never_visible_through_tools(mcp):
    for tool, args in [("visu_get_variable", {"variable_id": 111}), ("visu_get_values", {"variable_ids": [111]})]:
        text = await call(mcp, tool, **args)
        assert "geheim" not in text and "REDACTED" in text


async def test_instance_tool_hides_configuration(mcp):
    text = await call(mcp, "visu_get_instance", instance_id=120)
    assert "hunter2" not in text and "Password" not in text


async def test_ensure_category_idempotent(mcp, world):
    first = json.loads(await call(mcp, "visu_ensure_category", parent_id=500, name="Heizung"))
    second = json.loads(await call(mcp, "visu_ensure_category", parent_id=500, name="Heizung"))
    assert first["created"] is True and second["created"] is False
    assert first["category_id"] == second["category_id"]
    assert world.fake.objs[first["category_id"]]["ParentID"] == 500


async def test_ensure_category_outside_scope_creates_nothing(mcp, world):
    before = set(world.fake.objs)
    text = await call(mcp, "visu_ensure_category", parent_id=100, name="Böse")
    assert text.startswith("Abgelehnt")
    assert set(world.fake.objs) == before
    assert not any(m == "IPS_CreateCategory" for m, _ in world.fake.calls)


async def test_create_failure_removes_fresh_object(mcp, world):
    before = set(world.fake.objs)
    world.fake.fail_methods.add("IPS_SetName")
    text = await call(mcp, "visu_ensure_category", parent_id=500, name="Heizung")
    assert "Fehler" in text
    assert set(world.fake.objs) == before  # kein verwaistes Objekt an der Wurzel


async def test_ensure_link_create_retarget_and_unchanged(mcp, world):
    first = json.loads(await call(mcp, "visu_ensure_link", parent_id=501, name="Neu", target_id=110))
    assert first["created"] and world.fake.objs[first["link_id"]]["TargetID"] == 110
    again = json.loads(await call(mcp, "visu_ensure_link", parent_id=501, name="Neu", target_id=110))
    assert again == {"link_id": first["link_id"], "created": False, "target_changed": False}
    moved = json.loads(await call(mcp, "visu_ensure_link", parent_id=501, name="Neu", target_id=112))
    assert moved["target_changed"] and world.fake.objs[first["link_id"]]["TargetID"] == 112


async def test_ensure_link_missing_target(mcp, world):
    before = set(world.fake.objs)
    assert (await call(mcp, "visu_ensure_link", parent_id=501, name="X", target_id=99999)).startswith("Abgelehnt")
    assert set(world.fake.objs) == before


async def test_update_object_outside_scope_refused(mcp, world):
    text = await call(mcp, "visu_update_object", object_id=101, name="gehackt")
    assert text.startswith("Abgelehnt") and world.fake.objs[101]["ObjectName"] == "Büro"


async def test_delete_tool(mcp, world):
    assert json.loads(await call(mcp, "visu_delete_object", object_id=502))["deleted"]
    assert (await call(mcp, "visu_delete_object", object_id=500)).startswith("Abgelehnt")
    assert (await call(mcp, "visu_delete_object", object_id=130)).startswith("Abgelehnt")  # Skript, außerhalb
    assert 500 in world.fake.objs and 130 in world.fake.objs


async def test_write_tools_refused_when_disabled(ro_guard, world):
    m = build_server(ro_guard)
    text = await call(m, "visu_ensure_category", parent_id=500, name="X")
    assert text.startswith("Abgelehnt")
    assert not any(m_ in {"IPS_CreateCategory"} for m_, _ in world.fake.calls)


# --- Auth ---------------------------------------------------------------------


async def test_token_verifier():
    good = "s3cret-token"
    v = HashedTokenVerifier([hashlib.sha256(good.encode()).hexdigest()])
    assert await v.verify_token(good) is not None
    assert await v.verify_token("falsch") is None
    assert await v.verify_token("") is None
    assert await HashedTokenVerifier([]).verify_token(good) is None


# --- Konfiguration ----------------------------------------------------------------


def test_config_roundtrip(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(
        "symcon: {url: 'http://localhost:3777/api/', user: gw, password: pw}\n"
        "target_category_id: 500\nenable_write: yes\n"
        "server: {host: 0.0.0.0, port: 9000, token_sha256: ['AB12']}\n", encoding="utf-8")
    cfg = load_config(p)
    assert cfg.target_category_id == 500 and cfg.enable_write and cfg.token_sha256 == ("ab12",)


@pytest.mark.parametrize("body", [
    "symcon: {url: x}\n", "symcon: {url: x}\ntarget_category_id: 0\n", "target_category_id: 5\n",
])
def test_config_rejects_bad(tmp_path, body):
    p = tmp_path / "c.yaml"
    p.write_text(body, encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(p)


def test_guard_type():
    assert GuardedClient
