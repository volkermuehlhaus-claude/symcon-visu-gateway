"""Liest die bestehende WebFront-Struktur (nur lesend, über das Gateway) und erzeugt daraus eine Build-Spec.

Regeln:
  * Dummy-Instanz unter einer Raum-Kategorie -> Gruppe, Inhalt = sichtbare Kinder
    (Links -> Ziel, Variablen -> sich selbst)
  * Link auf eine andere Instanz (z. B. KNX Shutter) -> Gruppe mit dem Namen der Instanz,
    Inhalt = deren sichtbare Kinder
  * Link auf eine Variable direkt im Raum -> Sammelgruppe "<Raumname>"
  * Popup-Instanz, Diagramm (Media), Ereignis (Event) und andere Instanzen (kein Dummy)
    -> Kachel direkt in der Kategorie
  * ausgeblendete Objekte werden übersprungen; Reihenfolge = ObjectPosition;
    namenlose Links erhalten den Namen des Ziels.
Icons werden aus dem Bestand übernommen (Symcon-Legacy-Namen funktionieren in der Kachelvisu), sonst nach
Stichwörtern im Namen vergeben (ICON_RULES, Font Awesome). Anleitung: ``docs/sanfte-migration.md``.

    python tools/inventory_to_spec.py --env prod --page Räume --root <ID der WebFront-Kategorie> \\
        --out raeume_spec.json --layouts-from examples/beispiel_spec.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_visu import add_connection_args, call, make_client

DUMMY = "{485D0419-BE97-4548-AA9C-C083EB82E61E}"
report: list[str] = []


# Font-Awesome-Icons (Light-Stil) nach Stichwörtern im Namen; erste passende Regel gewinnt, Bestands-Icons bleiben.
ICON_RULES: list[tuple[tuple[str, ...], str]] = [
    (("dimm",), "sliders"),
    (("steckdose",), "plug"),
    (("strom", "leistung", "energie", "verbrauch"), "bolt"),
    (("soll",), "temperature-arrow-up"),
    (("temperatur", "temp."), "temperature-half"),
    (("co2",), "cloud"),
    (("feuchte",), "droplet"),
    (("helligkeit", "lux", "sonne", "astro"), "sun"),
    (("mond",), "moon"),
    (("präsenz", "bewegung", "anwesen"), "person-walking"),
    (("verschluss", "fenster", "dachfenster"), "window-maximize"),
    (("tür",), "door-closed"),
    (("aktion",), "arrows-up-down"),
    (("position",), "sliders"),
    (("wind",), "wind"),
    (("regen",), "cloud-rain"),
    (("licht", "lampe", "leuchte", "spot", "decke"), "lightbulb"),
]


def auto_icon(name: str) -> str | None:
    lowered = name.lower()
    for keywords, icon in ICON_RULES:
        if any(k in lowered for k in keywords):
            return icon
    return None


def clean_name(link_name: str | None, target_name: str | None) -> str:
    name = (link_name or "").strip()
    if not name or name.startswith("Unnamed Object"):
        name = (target_name or "").strip() or "?"
    return name


def unique(name: str, used: set[str], target_name: str | None) -> str:
    candidate = name
    if candidate in used and target_name:
        candidate = f"{name} ({target_name})"
    i = 2
    while candidate in used:
        candidate = f"{name} {i}"
        i += 1
    used.add(candidate)
    return candidate


async def children(client: Any, oid: int) -> list[dict[str, Any]]:
    return (await call(client, "visu_list_children", object_id=oid))["children"]


async def links_of(client: Any, oid: int) -> dict[int, dict[str, Any]]:
    res = await call(client, "visu_get_links", parent_id=oid)
    return {lk["link_id"]: lk for lk in res.get("links", [])}


async def items_of(client: Any, container: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Sichtbare Zeilen eines Containers (Dummy-Instanz oder Zielinstanz) und Kacheln (Diagramm, Ereignis, Popup)."""
    kids = [k for k in await children(client, container) if not k.get("hidden")]
    links = await links_of(client, container) if any(k["type"] == "Link" for k in kids) else {}
    items: list[dict[str, Any]] = []
    tiles: list[dict[str, Any]] = []
    used: set[str] = set()
    for k in kids:
        item: dict[str, Any] | None = None
        as_tile = False
        if k["type"] == "Link" and k["id"] in links:
            lk = links[k["id"]]
            name = clean_name(lk.get("link_name"), lk.get("target_name"))
            if lk.get("target_type") == "Variable":
                item = {"name": unique(name, used, lk.get("target_name")), "target": lk["target_id"]}
            elif lk.get("target_type") in {"Media", "Event", "Instance", "Script"}:
                item = {"name": name, "target": lk["target_id"]}
                as_tile = True
            else:
                target_type = lk.get("target_type")
                report.append(f"  übersprungen: Link '{k['name']}' ({k['id']}) in {container} -> {target_type}")
        elif k["type"] == "Variable":
            item = {"name": unique(clean_name(k["name"], None), used, None), "target": k["id"]}
        elif k["type"] in {"Media", "Event", "Instance", "Script"}:
            item = {"name": clean_name(k["name"], None), "target": k["id"]}
            as_tile = True
        else:
            report.append(f"  übersprungen: {k['type']} '{k['name']}' ({k['id']}) in {container}")
        if item is None:
            continue
        icon = k.get("icon") or (None if as_tile else auto_icon(item["name"]))
        if icon:
            item["icon"] = icon
        (tiles if as_tile else items).append(item)
    return items, tiles


async def module_id(client: Any, instance_id: int) -> str:
    info = await call(client, "visu_get_instance", instance_id=instance_id)
    return str(info.get("ModuleInfo", {}).get("ModuleID", "")).upper()


POPUP = "{5EA439B8-FB5C-4B81-AA35-1D14F4EA9821}"


async def crawl_room(client: Any, room_id: int, room_name: str) -> dict[str, Any]:
    kids = [k for k in await children(client, room_id) if not k.get("hidden")]
    links = await links_of(client, room_id) if any(k["type"] == "Link" for k in kids) else {}
    groups: list[dict[str, Any]] = []
    tiles: list[dict[str, Any]] = []
    loose: list[dict[str, Any]] = []
    used_group_names: set[str] = set()
    used_tile_names: set[str] = set()

    def add_tile(name: str, target: int, icon: str | None = None) -> None:
        tile = {"name": unique(name.strip(), used_tile_names, None), "target": target}
        if icon:
            tile["icon"] = icon
        tiles.append(tile)

    for k in kids:
        if k["type"] == "Instance":
            module = await module_id(client, k["id"])
            if module == DUMMY:
                items, sub_tiles = await items_of(client, k["id"])
                for t in sub_tiles:
                    add_tile(t["name"], t["target"], t.get("icon"))
                if items:
                    grp = {"name": unique(k["name"], used_group_names, None), "links": items}
                    if k.get("icon"):
                        grp["icon"] = k["icon"]
                    groups.append(grp)
                elif not sub_tiles:
                    report.append(f"  leere Gruppe ausgelassen: {room_name} / {k['name']}")
            else:
                add_tile(k["name"], k["id"], k.get("icon"))
        elif k["type"] == "Link" and k["id"] in links:
            lk = links[k["id"]]
            name = clean_name(lk.get("link_name"), lk.get("target_name"))
            if lk.get("target_type") == "Instance":
                if await module_id(client, lk["target_id"]) == POPUP:
                    add_tile(name, lk["target_id"], k.get("icon"))
                    continue
                items, sub_tiles = await items_of(client, lk["target_id"])
                for t in sub_tiles:
                    add_tile(t["name"], t["target"], t.get("icon"))
                if items:
                    groups.append({"name": unique(name, used_group_names, None), "links": items})
                elif not sub_tiles:
                    report.append(f"  Instanz-Link ohne sichtbare Kinder: {room_name} / {name}")
            elif lk.get("target_type") == "Variable":
                item = {"name": name, "target": lk["target_id"]}
                icon = k.get("icon") or auto_icon(name)
                if icon:
                    item["icon"] = icon
                loose.append(item)
            elif lk.get("target_type") in {"Media", "Event", "Script"}:
                add_tile(name, lk["target_id"], k.get("icon"))
            else:
                report.append(f"  übersprungen: Link '{k['name']}' auf {lk.get('target_type')}")
        elif k["type"] in {"Media", "Event", "Script"}:
            add_tile(k["name"], k["id"], k.get("icon"))
        elif k["type"] == "Category":
            report.append(f"  Unterkategorie nicht übernommen: {room_name} / {k['name']} ({k['id']})")
        elif k["type"] == "Variable":
            loose.append({"name": clean_name(k["name"], None), "target": k["id"]})
    if loose:
        names: set[str] = set()
        for item in loose:
            item["name"] = unique(item["name"], names, None)
        groups.append({"name": unique(room_name, used_group_names, None), "links": loose})
    return {"name": room_name, "groups": groups, "tiles": tiles}


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--page", required=True, help="Name der Seite in der neuen Visu, z. B. Räume")
    add_connection_args(ap)
    ap.add_argument("--root", type=int, required=True, help="Objekt-ID der WebFront-Kategorie, z. B. „Räume“")
    ap.add_argument("--out", required=True)
    ap.add_argument("--skip", nargs="*", default=[], help="schon gebaute Kategorien (nur Position)")
    ap.add_argument("--layouts-from", help="Spec, aus der der Block 'layouts' übernommen wird")
    ap.add_argument(
        "--as-page",
        action="store_true",
        help="--root selbst ist die Seite: Gruppen/Kacheln direkt auf der Seite statt in Unterkategorien",
    )
    ap.add_argument("--page-position", type=int, help="Position der Seite (Reihenfolge der Reiter oben)")
    args = ap.parse_args()

    categories: list[dict[str, Any]] = []
    if args.as_page:
        async with make_client(args.env, args.url, args.config, args.cert) as client:
            page = await crawl_room(client, args.root, args.page)
        page["name"] = args.page
        if args.page_position is not None:
            page["position"] = args.page_position
        spec = {"description": f"Aus WebFront-Kategorie {args.root} erzeugt (Seite)", "pages": [page]}
        if args.layouts_from:
            spec["layouts"] = json.loads(Path(args.layouts_from).read_text(encoding="utf-8")).get("layouts", [])
        Path(args.out).write_text(json.dumps(spec, ensure_ascii=False, indent=1), encoding="utf-8")
        print(
            f"{args.page}: {len(page['groups'])} Gruppen, {sum(len(x['links']) for x in page['groups'])} Links, "
            f"{len(page['tiles'])} Kacheln"
        )
        print(("\n".join(["", "Hinweise:", *report])) if report else "Keine Hinweise.")
        print(f"Spec geschrieben: {args.out}")
        return 0
    async with make_client(args.env, args.url, args.config, args.cert) as client:
        for pos, k in enumerate(k for k in await children(client, args.root) if k["type"] == "Category"):
            if k.get("hidden"):
                continue
            if k["name"] in args.skip:
                categories.append({"name": k["name"], "position": pos, "groups": []})
                print(f"{k['name']}: übersprungen (bereits gebaut)", flush=True)
                continue
            cat = await crawl_room(client, k["id"], k["name"])
            cat["position"] = pos
            categories.append(cat)
            print(
                f"{k['name']}: {len(cat['groups'])} Gruppen, {sum(len(g['links']) for g in cat['groups'])} Links, "
                f"{len(cat['tiles'])} Kacheln",
                flush=True,
            )
    spec: dict[str, Any] = {
        "description": f"Aus WebFront-Kategorie {args.root} erzeugt",
        "pages": [
            {
                "name": args.page,
                "categories": categories,
                **({"position": args.page_position} if args.page_position is not None else {}),
            }
        ],
    }
    if args.layouts_from:
        spec["layouts"] = json.loads(Path(args.layouts_from).read_text(encoding="utf-8")).get("layouts", [])
    Path(args.out).write_text(json.dumps(spec, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n".join(["", "Hinweise:", *report]) if report else "Keine Hinweise.")
    print(f"Spec geschrieben: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
