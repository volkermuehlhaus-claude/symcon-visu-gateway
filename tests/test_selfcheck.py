from __future__ import annotations

from visu_gateway.config import GatewayConfig
from visu_gateway.guard import GuardedClient
from visu_gateway.selfcheck import check


def cfg(target=500, **kw):
    return GatewayConfig(ips_url="http://x", ips_user="u", ips_password="SEKRIT", target_category_id=target, **kw)


async def test_selfcheck_ok_and_missing_parts(world):
    res = await check(cfg(), GuardedClient(world.fake, 500))
    text = "\n".join(m for _, m in res)
    assert "Kernel-Version 9.0" in text and "'Kachelvisu-Neu'" in text and "gesperrt" in text
    assert "SEKRIT" not in text
    assert [ok for ok, m in res if m.startswith("Token")] == [False]  # keine Token-Hashes konfiguriert


async def test_selfcheck_target_not_category(world):
    res = await check(cfg(110), GuardedClient(world.fake, 110))
    assert any(not ok and "keine Kategorie" in m for ok, m in res)


async def test_selfcheck_missing_target(world):
    res = await check(cfg(99999), GuardedClient(world.fake, 99999))
    assert any(not ok and "existiert nicht" in m for ok, m in res)
