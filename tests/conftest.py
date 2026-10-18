from __future__ import annotations

import pytest
from fake_ips import CATEGORY, INSTANCE, LINK, SCRIPT, VARIABLE, FakeIPS

from visu_gateway.guard import GuardedClient


class World:
    """Fester Testbaum: Zielkategorie 500 mit Unterkategorie 501; Altbestand 100 (Raum) mit Variablen."""

    def __init__(self) -> None:
        self.fake = FakeIPS()
        f = self.fake
        f.add(100, "Räume", CATEGORY, 0)
        f.add(101, "Büro", CATEGORY, 100)
        f.add(110, "Temperatur", VARIABLE, 101)
        f.values[110] = "21,5"
        f.add(111, "API Token", VARIABLE, 101)
        f.values[111] = "geheim"
        f.add(112, "Notiz", VARIABLE, 101)
        f.values[112] = "a" * 40  # sieht aus wie ein Token
        f.add(113, "Link", VARIABLE, 101)
        f.values[113] = "http://user:pw@nas/x?apikey=zzz&a=1"
        f.add(120, "Viessmann", INSTANCE, 0)
        f.add(130, "Skript", SCRIPT, 0)
        f.add(140, "Kachelvisu", INSTANCE, 0)
        f.add(500, "Kachelvisu-Neu", CATEGORY, 0)
        f.add(501, "Büro", CATEGORY, 500)
        f.add(502, "Link Temp", LINK, 501, TargetID=110)


@pytest.fixture
def world() -> World:
    return World()


@pytest.fixture
def guard(world: World) -> GuardedClient:
    return GuardedClient(world.fake, target_category_id=500, enable_write=True)


@pytest.fixture
def ro_guard(world: World) -> GuardedClient:
    return GuardedClient(world.fake, target_category_id=500, enable_write=False)
