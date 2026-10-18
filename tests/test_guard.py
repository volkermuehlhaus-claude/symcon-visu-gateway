from __future__ import annotations

import json

import pytest

from visu_gateway.guard import GuardedClient, PolicyError

FORBIDDEN = [
    "IPS_GetConfiguration", "IPS_SetConfiguration", "IPS_SetProperty", "IPS_ApplyChanges",
    "IPS_RunScript", "IPS_RunScriptEx", "IPS_RunScriptWait", "IPS_RunScriptWaitEx", "IPS_RunScriptText",
    "IPS_GetScriptContent", "IPS_SetScriptContent", "IPS_GetScript", "IPS_CreateScript", "IPS_CreateInstance",
    "IPS_CreateVariable", "IPS_CreateEvent", "IPS_DeleteObject", "IPS_DeleteInstance", "IPS_DeleteVariable",
    "IPS_DeleteScript", "SetValue", "RequestAction", "IPS_SetEventScript", "IPS_GetEvent",
    "IPS_ExecuteEx", "IPS_Execute", "IPS_GetOption", "IPS_GetKernelDir", "IPS_LogMessage", "ips_call",
    "", "iPS_getobject",
]


@pytest.mark.parametrize("method", FORBIDDEN)
@pytest.mark.parametrize("fixture", ["guard", "ro_guard"])
async def test_forbidden_methods_never_reach_symcon(request, world, method, fixture):
    g = request.getfixturevalue(fixture)
    with pytest.raises(PolicyError):
        await g.call(method, [110])
    assert world.fake.calls == []


async def test_write_disabled(ro_guard, world):
    with pytest.raises(PolicyError, match="nicht freigegeben"):
        await ro_guard.call("IPS_SetName", [501, "x"])
    assert world.fake.calls == []


async def test_reads_work_without_write_flag(ro_guard):
    assert await ro_guard.call("IPS_GetKernelVersion") == "9.0"


# --- Scope -----------------------------------------------------------------


async def test_rename_inside_scope(guard, world):
    await guard.call("IPS_SetName", [501, "Büro neu"])
    assert world.fake.objs[501]["ObjectName"] == "Büro neu"


@pytest.mark.parametrize("oid", [101, 100, 110, 0, -1, 999999, True, "501", None, 500.0])
async def test_set_name_outside_scope_refused(guard, world, oid):
    with pytest.raises(PolicyError):
        await guard.call("IPS_SetName", [oid, "x"])
    assert not any(m == "IPS_SetName" for m, _ in world.fake.calls)


async def test_cannot_pull_existing_object_into_target(guard, world):
    with pytest.raises(PolicyError):
        await guard.call("IPS_SetParent", [101, 501])  # Quelle außerhalb
    assert world.fake.objs[101]["ParentID"] == 100


async def test_cannot_move_scope_object_out(guard, world):
    with pytest.raises(PolicyError):
        await guard.call("IPS_SetParent", [501, 100])  # Ziel außerhalb
    assert world.fake.objs[501]["ParentID"] == 500


async def test_cannot_move_target_itself(guard):
    with pytest.raises(PolicyError):
        await guard.call("IPS_SetParent", [500, 501])


async def test_move_inside_scope(guard, world):
    new = await guard.call("IPS_CreateCategory", [])
    await guard.call("IPS_SetParent", [new, 500])
    await guard.call("IPS_SetParent", [new, 501])
    assert world.fake.objs[new]["ParentID"] == 501


async def test_fresh_object_can_only_move_into_scope(guard, world):
    new = await guard.call("IPS_CreateCategory", [])
    with pytest.raises(PolicyError):
        await guard.call("IPS_SetParent", [new, 100])
    await guard.call("IPS_SetParent", [new, 501])
    # nach dem Einhängen gilt normaler Scope, nicht mehr "frisch"
    assert new not in guard._fresh
    with pytest.raises(PolicyError):
        await guard.call("IPS_SetParent", [new, 100])


async def test_unmoved_foreign_object_not_fresh(guard, world):
    # ein vom Gateway nicht angelegtes Objekt an der Wurzel bleibt gesperrt
    with pytest.raises(PolicyError):
        await guard.call("IPS_SetName", [130, "x"])


async def test_delete_target_refused(guard, world):
    with pytest.raises(PolicyError, match="Zielkategorie"):
        await guard.call("IPS_DeleteCategory", [500])
    assert 500 in world.fake.objs


async def test_delete_outside_scope_refused(guard, world):
    with pytest.raises(PolicyError):
        await guard.call("IPS_DeleteCategory", [101])
    assert 101 in world.fake.objs


async def test_delete_wrong_type_refused(guard, world):
    with pytest.raises(PolicyError, match="Typ"):
        await guard.call("IPS_DeleteCategory", [502])  # ist ein Link
    with pytest.raises(PolicyError, match="Typ"):
        await guard.call("IPS_DeleteLink", [501])  # ist eine Kategorie


async def test_delete_link_inside_scope(guard, world):
    await guard.call("IPS_DeleteLink", [502])
    assert 502 not in world.fake.objs


async def test_create_with_params_refused(guard):
    with pytest.raises(PolicyError):
        await guard.call("IPS_CreateCategory", [100])


async def test_link_target_may_be_outside_scope_but_link_not(guard, world):
    await guard.call("IPS_SetLinkTargetID", [502, 111])  # Link in Scope, Ziel egal
    assert world.fake.objs[502]["TargetID"] == 111
    with pytest.raises(PolicyError):
        await guard.call("IPS_SetLinkTargetID", [110, 111])


async def test_parent_loop_does_not_hang(world):
    world.fake.objs[200] = {"ObjectID": 200, "ObjectName": "a", "ObjectType": 0, "ParentID": 201}
    world.fake.objs[201] = {"ObjectID": 201, "ObjectName": "b", "ObjectType": 0, "ParentID": 200}
    g = GuardedClient(world.fake, 500, True)
    assert await g.in_scope(200) is False


# --- Audit -------------------------------------------------------------------


async def test_audit_written_before_and_after(world, tmp_path):
    log = tmp_path / "audit.jsonl"
    g = GuardedClient(world.fake, 500, True, audit_log=log)
    await g.call("IPS_SetName", [501, "neu"])
    lines = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert [e["phase"] for e in lines] == ["request", "result"]
    assert lines[0]["method"] == "IPS_SetName" and lines[0]["params"] == [501, "neu"]


async def test_unwritable_audit_blocks_write(world, tmp_path):
    g = GuardedClient(world.fake, 500, True, audit_log=tmp_path / "missing_dir" / "audit.jsonl")
    with pytest.raises(PolicyError, match="Audit"):
        await g.call("IPS_SetName", [501, "neu"])
    assert world.fake.objs[501]["ObjectName"] == "Büro"


# --- Filter über den Guard ------------------------------------------------------


async def test_value_of_suspicious_variable_redacted(guard):
    assert await guard.call("GetValue", [111]) == "***REDACTED***"


async def test_token_like_value_redacted(guard):
    assert await guard.call("GetValue", [112]) == "***REDACTED***"


async def test_url_credentials_masked(guard):
    v = await guard.call("GetValue", [113])
    assert "pw" not in v and "zzz" not in v and "a=1" in v


async def test_harmless_value_passes(guard):
    assert await guard.call("GetValue", [110]) == "21,5"
    assert await guard.call("GetValueFormatted", [110]) == "21,5"


async def test_instance_reduced_to_module_info(guard):
    info = await guard.call("IPS_GetInstance", [120])
    assert "Configuration" not in info and "hunter2" not in json.dumps(info)
    assert info["ModuleInfo"]["ModuleName"] == "Viessmann"


async def test_variable_meta_has_no_value(guard):
    assert "VariableValue" not in await guard.call("IPS_GetVariable", [111])


async def test_wfc_configuration_decoded_and_filtered(guard):
    items = await guard.call("WFC_GetItems", [1])
    cfg = items[0]["Configuration"]
    assert isinstance(cfg, dict) and cfg["title"] == "Haus"
    assert "u:p" not in cfg["url"] and "abc" not in cfg["url"]
