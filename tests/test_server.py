from __future__ import annotations

import hashlib
import json

import pytest
from fake_ips import LINK
from fastmcp import Client

from visu_gateway.config import ConfigError, load_config
from visu_gateway.guard import GuardedClient, PolicyError
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


async def test_links_and_children_ordered_like_webfront_with_hidden_and_icon(mcp, world):
    f = world.fake
    f.objs[502]["ObjectPosition"] = 5
    f.add(None, "Versteckt", LINK, 501, TargetID=110, ObjectPosition=1, ObjectIsHidden=True)
    f.add(None, "Erster", LINK, 501, TargetID=110, ObjectPosition=0, ObjectIcon="Temperature")
    links = json.loads(await call(mcp, "visu_get_links", parent_id=501))["links"]
    assert [lk["link_name"] for lk in links] == ["Erster", "Versteckt", "Link Temp"]
    assert links[0]["icon"] == "Temperature" and "hidden" not in links[0]
    assert links[1]["hidden"] is True and links[2]["position"] == 5
    kids = json.loads(await call(mcp, "visu_list_children", object_id=501))["children"]
    assert [k["name"] for k in kids] == ["Erster", "Versteckt", "Link Temp"]


DUMMY = "{485D0419-BE97-4548-AA9C-C083EB82E61E}"


@pytest.fixture
def group_mcp(world):
    return build_server(GuardedClient(world.fake, target_category_id=500, enable_write=True, allow_groups=True))


async def test_group_tool_absent_unless_allowed(mcp, group_mcp):
    async with Client(mcp) as c:
        assert "visu_ensure_group" not in {t.name for t in await c.list_tools()}
    async with Client(group_mcp) as c:
        assert "visu_ensure_group" in {t.name for t in await c.list_tools()}


async def test_ensure_group_creates_dummy_idempotently_with_links(group_mcp, world):
    first = json.loads(await call(group_mcp, "visu_ensure_group", parent_id=501, name="Licht"))
    second = json.loads(await call(group_mcp, "visu_ensure_group", parent_id=501, name="Licht"))
    assert first["created"] is True and second["created"] is False and first["group_id"] == second["group_id"]
    obj = world.fake.objs[first["group_id"]]
    assert obj["ObjectType"] == 1 and obj["ModuleID"] == DUMMY and obj["ParentID"] == 501
    link = json.loads(await call(group_mcp, "visu_ensure_link", parent_id=first["group_id"], name="Temp", target_id=110))
    assert world.fake.objs[link["link_id"]]["ParentID"] == first["group_id"]


async def test_ensure_group_outside_scope_creates_nothing(group_mcp, world):
    before = len(world.fake.objs)
    text = await call(group_mcp, "visu_ensure_group", parent_id=101, name="Böse")
    assert "außerhalb" in text and len(world.fake.objs) == before


async def test_group_delete_only_empty_dummy(group_mcp, world):
    gid = json.loads(await call(group_mcp, "visu_ensure_group", parent_id=501, name="Weg"))["group_id"]
    json.loads(await call(group_mcp, "visu_ensure_link", parent_id=gid, name="L", target_id=110))
    assert "nicht leer" in await call(group_mcp, "visu_delete_object", object_id=gid)
    other = world.fake.add(None, "Fremd", 1, 501, ModuleID="{ABC}")
    assert "kein Dummy" in await call(group_mcp, "visu_delete_object", object_id=other)
    assert other in world.fake.objs


async def test_group_methods_refused_by_default_guard(guard):
    with pytest.raises(PolicyError, match="allow_groups"):
        await guard.call("IPS_CreateInstance", [DUMMY])


async def test_only_dummy_module_can_be_created(world):
    g = GuardedClient(world.fake, target_category_id=500, enable_write=True, allow_groups=True)
    with pytest.raises(PolicyError, match="Dummy Module"):
        await g.call("IPS_CreateInstance", ["{00000000-0000-0000-0000-000000000000}"])
    assert not any(m == "IPS_CreateInstance" for m, _ in world.fake.calls)


@pytest.fixture
def layout_mcp(world):
    return build_server(GuardedClient(world.fake, target_category_id=500, enable_write=True,
                                      visu_instance_id=140, allow_layout=True))


async def test_layout_tools_absent_unless_allowed(mcp, layout_mcp):
    async with Client(mcp) as c:
        assert not {t.name for t in await c.list_tools()} & {"visu_get_grid_config", "visu_set_grid_layout"}
    async with Client(layout_mcp) as c:
        assert {"visu_get_grid_config", "visu_set_grid_layout"} <= {t.name for t in await c.list_tools()}


async def test_grid_config_read_returns_only_the_layout(layout_mcp, world):
    world.fake.grid = json.dumps({"~Desktop": {"landscape": {"crossAxis": 12}}})
    text = await call(layout_mcp, "visu_get_grid_config")
    assert json.loads(text) == {"~Desktop": {"landscape": {"crossAxis": 12}}}
    assert "hunter2" not in text and "children" not in text


async def test_set_grid_layout_merges_and_keeps_user_settings(layout_mcp, world):
    world.fake.grid = json.dumps({
        "~Desktop": {"landscape": {"crossAxis": 12, "individualConfig": {"9": {"widget": "x"}},
                                   "individualDimensions": {"501": {"width": 2, "height": 2}}}},
        "~Phone": {"portrait": {"crossAxis": 6}},
    })
    out = json.loads(await call(layout_mcp, "visu_set_grid_layout", default_width=4, default_height=3,
                                custom_dimensions={"WidgetConfigType.instanceContainer": {"width": 4, "height": 4}},
                                individual_dimensions={"502": {"width": 6, "height": 3}}))
    saved = json.loads(world.fake.grid)
    land = saved["~Desktop"]["landscape"]
    assert land["defaultDimensions"] == {"width": 4, "height": 3} and land["crossAxis"] == 12
    assert land["individualConfig"] == {"9": {"widget": "x"}}  # Benutzereinstellung bleibt
    assert land["individualDimensions"] == {"501": {"width": 2, "height": 2}, "502": {"width": 6, "height": 3}}
    assert saved["~Phone"] == {"portrait": {"crossAxis": 6}} and out["layout"] == land


async def test_set_grid_layout_clear_positions_keeps_the_rest(layout_mcp, world):
    world.fake.grid = json.dumps({"~Desktop": {"landscape": {
        "crossAxis": 12, "individualPositions": {"501": {"502": {"left": 4, "top": 2}}},
        "individualDimensions": {"502": {"width": 4, "height": 2}}, "individualConfig": {"9": {"w": 1}}}}})
    await call(layout_mcp, "visu_set_grid_layout", clear_positions=True,
               individual_dimensions={"502": {"width": 3, "height": 3}})
    land = json.loads(world.fake.grid)["~Desktop"]["landscape"]
    assert land["individualPositions"] == {} and land["individualDimensions"] == {"502": {"width": 3, "height": 3}}
    assert land["crossAxis"] == 12 and land["individualConfig"] == {"9": {"w": 1}}


@pytest.mark.parametrize("args", [
    {"profile": "~TV"}, {"orientation": "diagonal"}, {"cross_axis": 25}, {"default_width": 3},
    {"custom_dimensions": {"instanceContainer": {"width": 4, "height": 4}}},
    {"custom_dimensions": {"WidgetConfigType.instanceContainer": {"width": 0, "height": 4}}},
    {"individual_dimensions": {"110": {"width": 4, "height": 4}}},  # außerhalb der Zielkategorie
])
async def test_set_grid_layout_rejects_bad_input(layout_mcp, world, args):
    world.fake.grid = "{}"
    text = await call(layout_mcp, "visu_set_grid_layout", **args)
    assert text.startswith("Abgelehnt") and world.fake.grid == "{}"


async def test_layout_calls_only_for_the_configured_instance(world):
    g = GuardedClient(world.fake, target_category_id=500, enable_write=True, visu_instance_id=140, allow_layout=True)
    with pytest.raises(PolicyError, match="konfigurierte Kachelvisu"):
        await g.call("VISU_GetSnapshot", [120])
    with pytest.raises(PolicyError, match="konfigurierte Kachelvisu"):
        await g.call("VISU_SaveGridConfiguration", [120, "{}"])
    with pytest.raises(PolicyError, match="JSON-Objekt"):
        await g.call("VISU_SaveGridConfiguration", [140, "[]"])
    with pytest.raises(PolicyError, match="allow_layout"):
        await GuardedClient(world.fake, target_category_id=500, enable_write=True).call("VISU_GetSnapshot", [140])
    assert not any(m.startswith("VISU_") for m, _ in world.fake.calls)


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
