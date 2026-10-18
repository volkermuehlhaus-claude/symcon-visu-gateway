"""Integrationstest gegen ein echtes Symcon (Docker-Testsystem). Wird ohne VISU_LIVE_URL übersprungen.

Voraussetzung: Kategorie "Kachelvisu-Neu" (VISU_LIVE_TARGET), "Altbestand" (VISU_LIVE_OLD) mit
einer Variablen "Temperatur" und einer String-Variablen "WLAN Passwort". Nie gegen Produktion starten.
"""

from __future__ import annotations

import json
import os

import pytest
from fastmcp import Client

from visu_gateway.client import IPSClient
from visu_gateway.guard import GuardedClient, PolicyError
from visu_gateway.server import build_server

URL = os.environ.get("VISU_LIVE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="VISU_LIVE_URL nicht gesetzt")

TARGET = int(os.environ.get("VISU_LIVE_TARGET", "0"))
OLD = int(os.environ.get("VISU_LIVE_OLD", "0"))


@pytest.fixture
async def live():
    raw = IPSClient(url=URL)
    g = GuardedClient(raw, TARGET, enable_write=True)
    yield g

    async def purge(parent: int) -> None:  # aufräumen, Kinder zuerst
        for cid in await raw.call("IPS_GetChildrenIDs", [parent]):
            await purge(cid)
            obj = await raw.call("IPS_GetObject", [cid])
            await raw.call("IPS_DeleteLink" if obj["ObjectType"] == 6 else "IPS_DeleteCategory", [cid])

    await purge(TARGET)


async def test_kernel_version_matches_production():
    assert (await GuardedClient(IPSClient(url=URL), TARGET).call("IPS_GetKernelVersion")).startswith("9.")


async def test_read_and_filter(live):
    kids = {c: (await live.call("IPS_GetObject", [c]))["ObjectName"] for c in await live.call("IPS_GetChildrenIDs", [OLD])}
    by_name = {v: k for k, v in kids.items()}
    assert await live.call("GetValue", [by_name["Temperatur"]]) == 21.5
    assert await live.call("GetValue", [by_name["WLAN Passwort"]]) == "***REDACTED***"
    assert "VariableValue" not in await live.call("IPS_GetVariable", [by_name["Temperatur"]])


async def test_instance_info_only(live):
    for iid in await live.call("IPS_GetChildrenIDs", [0]):
        obj = await live.call("IPS_GetObject", [iid])
        if obj["ObjectType"] == 1:
            info = await live.call("IPS_GetInstance", [iid])
            assert set(info) == {"InstanceID", "InstanceStatus", "ConnectionID", "ModuleInfo"}
            return
    pytest.skip("keine Instanz vorhanden")


async def test_get_links_tool_on_real_symcon(live):
    temp = await live.call("IPS_GetObjectIDByName", ["Temperatur", OLD])
    link = await live.call("IPS_CreateLink", [])
    await live.call("IPS_SetParent", [link, TARGET])
    await live.call("IPS_SetName", [link, "Temperatur"])
    await live.call("IPS_SetLinkTargetID", [link, temp])
    async with Client(build_server(live)) as c:
        res = await c.call_tool("visu_get_links", {"parent_id": TARGET}, raise_on_error=False)
    out = json.loads(res.content[0].text)
    entry = next(e for e in out["links"] if e["link_id"] == link)
    assert entry["target_id"] == temp and entry["target_type"] == "Variable"
    assert entry["variable_type"] == "Float" and "21" in entry["formatted"]


async def test_build_and_scope_on_real_symcon(live):
    cat = await live.call("IPS_CreateCategory", [])
    await live.call("IPS_SetParent", [cat, TARGET])
    await live.call("IPS_SetName", [cat, "Büro"])
    link = await live.call("IPS_CreateLink", [])
    await live.call("IPS_SetParent", [link, cat])
    await live.call("IPS_SetName", [link, "Temperatur"])
    temp = await live.call("IPS_GetObjectIDByName", ["Temperatur", OLD])
    await live.call("IPS_SetLinkTargetID", [link, temp])
    assert (await live.call("IPS_GetLink", [link]))["TargetID"] == temp
    # Altbestand bleibt unantastbar
    for method, params in [("IPS_SetName", [temp, "x"]), ("IPS_SetParent", [temp, cat]),
                           ("IPS_SetParent", [cat, OLD]), ("IPS_DeleteCategory", [TARGET])]:
        with pytest.raises(PolicyError):
            await live.call(method, params)
    assert (await live.call("IPS_GetObject", [temp]))["ObjectName"] == "Temperatur"
