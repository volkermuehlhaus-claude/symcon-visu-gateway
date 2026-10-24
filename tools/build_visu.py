"""Baut die Kachelvisu-Struktur aus einer Spec-Datei über das Gateway auf (idempotent).

Ohne ``--execute`` wird nur der Plan gezeigt (nichts geschrieben). Mit ``--execute`` prüft das Skript zuerst
``visu_info`` (Schreiben freigegeben? Zielkategorie = ``root`` der ID-Datei?) und legt dann Seiten, Kategorien,
Gruppen (Dummy-Instanzen) und Links an bzw. gleicht sie an (Name, Ziel, Position, Icon).
Vorhandenes wird nicht dupliziert. Anleitung: ``docs/sanfte-migration.md``.

    python tools/build_visu.py --env test --spec meine_spec.json --ids ids_test.json [--execute]
    python tools/build_visu.py --env prod --spec meine_spec.json --ids ids_prod.json [--execute]   # nur nach Backup

``--env test``: startet das Gateway lokal (stdio, ``--config``, Standard ``config.test.yaml``) gegen das
Docker-Testsystem.
``--env prod``: Gateway auf dem Symcon-Rechner (https). Adresse aus ``--url`` oder VISU_GATEWAY_URL, Token aus
VISU_GATEWAY_TOKEN, selbstsigniertes Zertifikat über ``--cert`` oder SSL_CERT_FILE.
ID-Datei: ``{"root": <Zielkategorie>, "variables": {"schlüssel": <Objekt-ID>, ...}}``. Ziele in der Spec sind entweder
Schlüssel dieser Datei oder direkt Objekt-IDs (so erzeugt es ``inventory_to_spec.py``); dann genügt ``--root``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from fastmcp import Client
from fastmcp.client.transports import StdioTransport, StreamableHttpTransport

REPO = Path(__file__).resolve().parent.parent  # Wurzel des Gateway-Repos


def load_ids(path: str | None, root: int | None) -> tuple[int, dict[str, int]]:
    variables: dict[str, int] = {}
    if path:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        variables = data.get("variables", {})
        root = root or int(data["root"])
    if not root:
        sys.exit('Zielkategorie fehlt: --ids mit "root" oder --root angeben.')
    return root, variables


TITLE_PX, ROW_PX = 76, 40  # Kachel mit Überschrift; Höhe einer Listenzeile (aus Screenshots der Kachelvisu)


def group_cells(rows: int, cell_px: int) -> int:
    """Kachelhöhe in Zellen, damit alle Zeilen einer Gruppe sichtbar sind (mindestens 2)."""
    return max(2, -(-(TITLE_PX + ROW_PX * rows) // cell_px))


def layout_plan(spec: dict[str, Any]) -> list[dict[str, Any]]:
    """Je Profil/Ausrichtung: Spalten, Standardgröße und Größe jeder Gruppe und jeder Kachel mit ``rows``
    (Pfad -> Breite/Höhe in Zellen). Popup-Kacheln zeigen ihre Kinder als Liste, daher wie Gruppen."""
    groups = {path: len(node["links"]) for level, path, node in walk(spec) if level == "group"}
    groups |= {path: node["rows"] for level, path, node in walk(spec) if level == "tile" and node.get("rows")}
    plans = []
    for lay in spec.get("layouts", []):
        sizes = {
            path: {"width": lay["width"], "height": group_cells(rows, lay["cell_px"])} for path, rows in groups.items()
        }
        plans.append({**lay, "sizes": sizes})
    return plans


def resolve(target: Any, ids: dict[str, int]) -> int | None:
    """Ziel einer Link-Zeile: Schlüssel der ID-Datei oder direkt eine Objekt-ID."""
    return target if isinstance(target, int) else ids.get(target)


def walk(spec: dict[str, Any]):
    """Liefert (Ebene, Pfad, Knoten) in Aufbau-Reihenfolge: page, category, group, link, tile.

    Gruppen und Kacheln können direkt an der Seite (Pfad mit leerem Kategorienamen) oder in Kategorien hängen.
    """

    def container(prefix: tuple[str, ...], node: dict[str, Any]):
        groups = node.get("groups", [])
        for gi, grp in enumerate(groups):
            gpath = (*prefix, grp["name"])
            yield "group", gpath, {**grp, "position": grp.get("position", gi)}
            for li, link in enumerate(grp["links"]):
                yield "link", (*gpath, link["name"]), {**link, "position": link.get("position", li)}
        for ti, tile in enumerate(node.get("tiles", [])):  # Popup, Diagramm, Ereignis direkt in der Kategorie/Seite
            pos = tile.get("position", len(groups) + ti)
            yield "tile", (*prefix, "tile:" + tile["name"]), {**tile, "position": pos}

    for pi, page in enumerate(spec["pages"]):
        yield "page", (page["name"],), {**page, "position": page.get("position", pi)}
        yield from container((page["name"], ""), page)
        for ci, cat in enumerate(page.get("categories", [])):
            yield "category", (page["name"], cat["name"]), {**cat, "position": cat.get("position", ci)}
            yield from container((page["name"], cat["name"]), cat)


def print_plan(spec: dict[str, Any], ids: dict[str, int], root: int) -> int:
    counts = {"page": 0, "category": 0, "group": 0, "link": 0, "tile": 0}
    missing = []
    print(f"Zielkategorie: {root}")
    for level, path, node in walk(spec):
        counts[level] += 1
        indent = "  " * (len(path) - 1)
        if level in {"link", "tile"}:
            target = resolve(node["target"], ids)
            if target is None:
                missing.append(node["target"])
            icon = f"  [{node['icon']}]" if node.get("icon") else ""
            mark = (f" (Kachel, {node['rows']} Zeilen)" if node.get("rows") else " (Kachel)") if level == "tile" else ""
            print(f"{indent}- {node['position']}: {node['name']}{mark}  -> {node['target']} ({target}){icon}")
        else:
            tag = {"page": "Seite", "category": "Kategorie", "group": "Gruppe"}[level]
            print(f"{indent}{node['position']}: {node['name']}  ({tag})")
    print(
        f"\n{counts['page']} Seiten, {counts['category']} Kategorien, {counts['group']} Gruppen, "
        f"{counts['link']} Links, {counts['tile']} Kacheln"
    )
    for plan in layout_plan(spec):
        heights = sorted({s["height"] for s in plan["sizes"].values()})
        print(
            f"Layout {plan['label']}: {plan['profile']} {plan['orientation']}, {plan['cross_axis']} Spalten, "
            f"Kachelbreite {plan['width']}, Höhen {heights} Zellen (je nach Zeilenzahl)"
        )
    if missing:
        print("FEHLT in der ID-Datei:", sorted(set(missing)), file=sys.stderr)
    return len(missing)


async def call(client: Client, tool: str, **args: Any) -> dict[str, Any]:
    res = await client.call_tool(tool, args, raise_on_error=False)
    text = res.content[0].text
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        raise RuntimeError(f"{tool} {args}: {text}") from None


async def apply_layout(
    client: Client, spec: dict[str, Any], group_ids: dict[tuple[str, ...], int], only: str | None
) -> None:
    tools = {t.name for t in await client.list_tools()}
    if "visu_set_grid_layout" not in tools:
        print("Layout übersprungen: Gateway hat allow_layout nicht freigegeben.")
        return
    for plan in layout_plan(spec):
        if only and only not in {plan["orientation"], plan["profile"]}:
            continue
        sizes = {str(group_ids[path]): size for path, size in plan["sizes"].items()}
        res = await call(
            client,
            "visu_set_grid_layout",
            profile=plan["profile"],
            orientation=plan["orientation"],
            cross_axis=plan["cross_axis"],
            default_width=plan["width"],
            default_height=3,
            individual_dimensions=sizes,
            clear_positions=bool(plan.get("clear_positions")),
            custom_dimensions=plan.get("custom") or None,
        )
        print(
            f"Layout {plan['label']}: {len(sizes)} Gruppen/Kacheln gesetzt",
            "ok" if "layout" in res else res,
            flush=True,
        )


async def execute(
    client: Client, spec: dict[str, Any], ids: dict[str, int], root: int, layout: str | None, layout_only: bool = False
) -> None:
    info = await call(client, "visu_info")
    print("Gateway:", info)
    if not info.get("write_enabled"):
        sys.exit("Abbruch: Schreiben ist im Gateway nicht freigegeben.")
    if info.get("target_category_id") != root:
        sys.exit(f"Abbruch: Zielkategorie des Gateways {info.get('target_category_id')} != {root}.")

    created = changed = 0
    parents: dict[tuple[str, ...], int] = {(): root}
    for level, path, node in walk(spec):
        if layout_only and level == "link":
            continue
        parent = parents[path[:-1]]
        if level in {"page", "category"}:
            res = await call(client, "visu_ensure_category", parent_id=parent, name=node["name"])
            oid = res["category_id"]
        elif level == "group":
            res = await call(client, "visu_ensure_group", parent_id=parent, name=node["name"])
            oid = res["group_id"]
        else:  # link oder tile
            res = await call(
                client, "visu_ensure_link", parent_id=parent, name=node["name"], target_id=resolve(node["target"], ids)
            )
            oid = res["link_id"]
        parents[path] = oid
        if level == "page":
            parents[(*path, "")] = oid  # Gruppen/Kacheln direkt an der Seite
        created += bool(res.get("created"))
        changed += bool(res.get("target_changed")) and not res.get("created")
        if layout_only:
            print(f"ID   {level:8} {' > '.join(path)}  [{oid}]", flush=True)
            continue
        upd: dict[str, Any] = {"object_id": oid, "position": node["position"]}
        if node.get("icon"):
            upd["icon"] = node["icon"]
        await call(client, "visu_update_object", **upd)
        print(f"{'neu ' if res.get('created') else 'ok  '} {level:8} {' > '.join(path)}  [{oid}]", flush=True)
    print(f"\nFertig: {created} neu angelegt, {changed} Ziele angepasst.")
    if layout != "none":
        group_ids = {path: oid for path, oid in parents.items() if len(path) == 3}
        await apply_layout(client, spec, group_ids, None if layout in (None, "all") else layout)


def make_client(env: str, url: str | None = None, config: str | None = None, cert: str | None = None) -> Client:
    if env == "test":
        transport = StdioTransport(
            command=sys.executable,  # Python des Gateway-venv, mit dem dieses Skript gestartet wurde
            args=["-m", "visu_gateway", "--transport", "stdio", "--config", config or str(REPO / "config.test.yaml")],
            env={**os.environ, "PYTHONPATH": str(REPO)},
        )
        return Client(transport)
    token = os.environ.get("VISU_GATEWAY_TOKEN")
    if not token:
        sys.exit("VISU_GATEWAY_TOKEN ist nicht gesetzt.")
    url = url or os.environ.get("VISU_GATEWAY_URL")
    if not url:
        sys.exit("Gateway-Adresse fehlt: --url oder VISU_GATEWAY_URL (z. B. https://symcon-pc:8765/mcp).")
    if cert:
        os.environ["SSL_CERT_FILE"] = cert
    return Client(StreamableHttpTransport(url, headers={"Authorization": f"Bearer {token}"}))


def add_connection_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--env", choices=["test", "prod"], required=True)
    ap.add_argument("--url", help="Gateway-URL für --env prod (sonst VISU_GATEWAY_URL)")
    ap.add_argument("--cert", help="Zertifikat des Gateways für --env prod (sonst SSL_CERT_FILE)")
    ap.add_argument("--config", help="Gateway-Konfiguration für --env test (Standard config.test.yaml)")


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_connection_args(ap)
    ap.add_argument("--spec", required=True, help="Spec-Datei (Aufbau der neuen Visu)")
    ap.add_argument("--ids", help="ID-Datei mit {root, variables}")
    ap.add_argument("--root", type=int, help="Objekt-ID der Zielkategorie (statt root aus der ID-Datei)")
    ap.add_argument(
        "--layout",
        default="all",
        help="Kachelgrößen: all (Standard), none, oder Filter auf eine Ausrichtung/ein Profil "
        "(portrait, landscape, ~Desktop, ~Tablet, ~Phone)",
    )
    ap.add_argument(
        "--layout-only",
        action="store_true",
        help="nur Kachelgrößen setzen (Kategorien/Gruppen nur nachschlagen, Links nicht anfassen)",
    )
    ap.add_argument("--execute", action="store_true", help="wirklich schreiben (sonst nur Plan)")
    args = ap.parse_args()

    spec = json.loads(Path(args.spec).read_text(encoding="utf-8"))
    root, ids = load_ids(args.ids, args.root)
    if print_plan(spec, ids, root):
        return 2
    if not args.execute:
        print("\nNur Plan (nichts geschrieben). Zum Ausführen --execute angeben.")
        return 0
    async with make_client(args.env, args.url, args.config, args.cert) as client:
        await execute(client, spec, ids, root, args.layout, args.layout_only)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
