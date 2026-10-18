"""MCP-Server (Gateway) für die Kachelvisu-Erstellung.

Anders als der Upstream-Server gibt es hier keinen generischen ``ips_call`` und keine Skript-,
Konfigurations- oder Export-Werkzeuge. Jedes Werkzeug ruft ausschließlich ``GuardedClient.call``
auf (Whitelist, Scope, Filter, Audit – siehe guard.py).
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import httpx
from fastmcp import FastMCP
from fastmcp.server.auth import AccessToken, TokenVerifier

from .client import IPSConfigError, IPSError
from .guard import GuardedClient, PolicyError

OBJECT_TYPES = {0: "Category", 1: "Instance", 2: "Variable", 3: "Script", 4: "Event", 5: "Media", 6: "Link"}
VARIABLE_TYPES = {0: "Boolean", 1: "Integer", 2: "Float", 3: "String"}
MAX_TREE_NODES = 1500

INSTRUCTIONS = """\
Gateway zu einem IP-Symcon-System, um eine Kachelvisualisierung aufzubauen.
Lesen: Objektbaum, Variablen (Metadaten, Werte, formatiert), Modulinfo, Profile, WebFront-Items.
Schreiben (nur wenn freigegeben): ausschließlich unterhalb der Zielkategorie – Kategorien und Links anlegen,
umbenennen, verschieben, ausblenden, löschen. Bestehende Objekte außerhalb werden nie verändert.
Nicht möglich (absichtlich): Instanzkonfiguration, Skripte lesen/ausführen, Werte setzen, Geräte schalten.
Antworten sind gefiltert: Maskierte Stellen erscheinen als ***REDACTED***; nicht versuchen, sie zu umgehen.
Schreiben immer erst planen und dem Nutzer zeigen, dann ausführen.
"""


class HashedTokenVerifier(TokenVerifier):
    """Prüft Bearer-Token gegen SHA-256-Hashes. Im Klartext steht kein Token in der Konfiguration."""

    def __init__(self, token_sha256: tuple[str, ...] | list[str]) -> None:
        super().__init__()
        self._hashes = [h.strip().lower() for h in token_sha256 if h.strip()]

    async def verify_token(self, token: str) -> AccessToken | None:
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        ok = False
        for h in self._hashes:  # kein frühes Beenden: gleiche Laufzeit für alle Hashes
            ok |= hmac.compare_digest(digest, h)
        if not ok:
            return None
        return AccessToken(token=token, client_id="claude", scopes=[])


def _dumps(obj: Any) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False, default=str)


def _error(e: Exception) -> str:
    if isinstance(e, PolicyError):
        return f"Abgelehnt: {e}"
    if isinstance(e, IPSError):
        return f"Fehler von IP-Symcon ({e.code}): {e.message}"
    if isinstance(e, IPSConfigError):
        return f"Konfigurationsfehler: {e}"
    if isinstance(e, httpx.HTTPStatusError):
        return f"IP-Symcon antwortete mit HTTP {e.response.status_code}."
    if isinstance(e, httpx.TransportError):
        return "IP-Symcon ist nicht erreichbar (Verbindung/Zeitüberschreitung)."
    return f"Unerwarteter Fehler: {type(e).__name__}"


def _kind(obj: dict[str, Any]) -> Any:
    return OBJECT_TYPES.get(obj.get("ObjectType"), obj.get("ObjectType"))


def _display(obj: dict[str, Any]) -> dict[str, Any]:
    """Darstellungsrelevante Felder eines Objekts: Position, ausgeblendet, Icon (leer = Standard)."""
    out: dict[str, Any] = {"position": obj.get("ObjectPosition", 0)}
    if obj.get("ObjectIsHidden"):
        out["hidden"] = True
    if obj.get("ObjectIcon"):
        out["icon"] = obj["ObjectIcon"]
    return out


def _order(entry: dict[str, Any]) -> tuple[int, str]:
    """Reihenfolge wie im WebFront: Position, dann Name."""
    return entry.get("position", 0), str(entry.get("name") or entry.get("link_name") or "")


def build_server(guard: GuardedClient, auth: TokenVerifier | None = None) -> FastMCP:
    mcp = FastMCP("symcon_visu_gateway", instructions=INSTRUCTIONS, auth=auth)
    ro = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
    rw = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
    rd = {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False}

    async def _find_child(parent_id: int, name: str, object_type: int) -> int | None:
        for cid in await guard.call("IPS_GetChildrenIDs", [parent_id]) or []:
            obj = await guard.call("IPS_GetObject", [cid])
            if isinstance(obj, dict) and obj.get("ObjectName") == name and obj.get("ObjectType") == object_type:
                return cid
        return None

    async def _require_scope(parent_id: int) -> None:
        if not await guard.in_scope(parent_id, include_root=True):
            raise PolicyError(f"Eltern-Objekt {parent_id} liegt außerhalb der Zielkategorie {guard.target}.")

    async def _create_under(creator: str, deleter: str, parent_id: int, name: str) -> int:
        """Anlegen + einhängen + benennen; bei Fehler das frische Objekt wieder entfernen."""
        await _require_scope(parent_id)
        new_id = await guard.call(creator, [])
        try:
            await guard.call("IPS_SetParent", [new_id, parent_id])
            await guard.call("IPS_SetName", [new_id, name])
        except Exception:
            await guard.call(deleter, [new_id])
            raise
        return new_id

    # --- Lesen ----------------------------------------------------------------

    @mcp.tool(name="visu_info", annotations={"title": "Gateway-Status", **ro})
    async def visu_info() -> str:
        """Kernel-Version, Zielkategorie und ob Schreiben freigegeben ist."""
        try:
            return _dumps({
                "kernel_version": await guard.call("IPS_GetKernelVersion", []),
                "target_category_id": guard.target,
                "write_enabled": guard.enable_write,
            })
        except Exception as e:  # noqa: BLE001
            return _error(e)

    @mcp.tool(name="visu_get_object", annotations={"title": "Objekt-Metadaten", **ro})
    async def visu_get_object(object_id: int) -> str:
        """Metadaten eines Objekts (IPS_GetObject), inkl. ParentID und ChildrenIDs."""
        try:
            obj = await guard.call("IPS_GetObject", [object_id])
            if isinstance(obj, dict):
                obj = {**obj, "ObjectTypeName": _kind(obj)}
            return _dumps(obj)
        except Exception as e:  # noqa: BLE001
            return _error(e)

    @mcp.tool(name="visu_list_children", annotations={"title": "Kinder auflisten", **ro})
    async def visu_list_children(object_id: int = 0) -> str:
        """Direkte Kinder eines Objekts mit id, name, type. Wurzel = 0."""
        try:
            children = []
            for cid in await guard.call("IPS_GetChildrenIDs", [object_id]) or []:
                obj = await guard.call("IPS_GetObject", [cid])
                obj = obj if isinstance(obj, dict) else {}
                children.append({"id": cid, "name": obj.get("ObjectName"), "type": _kind(obj), **_display(obj)})
            children.sort(key=_order)
            return _dumps({"parent_id": object_id, "count": len(children), "children": children})
        except Exception as e:  # noqa: BLE001
            return _error(e)

    @mcp.tool(name="visu_get_tree", annotations={"title": "Teilbaum", **ro})
    async def visu_get_tree(root_id: int = 0, max_depth: int = 3) -> str:
        """Teilbaum als verschachteltes {id,name,type,children}. Tiefe 1..10, höchstens 1500 Knoten."""
        max_depth = max(1, min(int(max_depth), 10))
        budget = [MAX_TREE_NODES]
        truncated = [False]

        async def build(oid: int, depth: int) -> dict[str, Any]:
            obj = await guard.call("IPS_GetObject", [oid])
            obj = obj if isinstance(obj, dict) else {}
            node: dict[str, Any] = {"id": oid, "name": obj.get("ObjectName"), "type": _kind(obj)}
            if depth < max_depth:
                kids = []
                for cid in await guard.call("IPS_GetChildrenIDs", [oid]) or []:
                    if budget[0] <= 0:
                        truncated[0] = True
                        break
                    budget[0] -= 1
                    kids.append(await build(cid, depth + 1))
                if kids:
                    node["children"] = kids
            return node

        try:
            tree = await build(root_id, 0)
            if truncated[0]:
                tree["truncated"] = f"Knotenlimit {MAX_TREE_NODES} erreicht; Teilbaum gezielter abfragen."
            return _dumps(tree)
        except Exception as e:  # noqa: BLE001
            return _error(e)

    @mcp.tool(name="visu_get_variable", annotations={"title": "Variable (Metadaten + Wert)", **ro})
    async def visu_get_variable(variable_id: int) -> str:
        """Metadaten, Wert und formatierten Wert einer Variablen."""
        try:
            meta = await guard.call("IPS_GetVariable", [variable_id])
            meta = meta if isinstance(meta, dict) else {}
            value = await guard.call("GetValue", [variable_id])
            try:
                formatted = await guard.call("GetValueFormatted", [variable_id])
            except IPSError:
                formatted = None
            return _dumps({
                "variable_id": variable_id,
                "name": await guard.call("IPS_GetName", [variable_id]),
                "type": VARIABLE_TYPES.get(meta.get("VariableType"), meta.get("VariableType")),
                "value": value,
                "formatted": formatted,
                "metadata": meta,
            })
        except Exception as e:  # noqa: BLE001
            return _error(e)

    @mcp.tool(name="visu_get_values", annotations={"title": "Mehrere Werte lesen", **ro})
    async def visu_get_values(variable_ids: list[int]) -> str:
        """Werte (roh und formatiert) mehrerer Variablen; höchstens 200 pro Aufruf."""
        if len(variable_ids) > 200:
            return "Abgelehnt: höchstens 200 IDs pro Aufruf."
        out: dict[str, Any] = {}
        try:
            for vid in variable_ids:
                try:
                    out[str(vid)] = {
                        "value": await guard.call("GetValue", [vid]),
                        "formatted": await guard.call("GetValueFormatted", [vid]),
                    }
                except IPSError as e:
                    out[str(vid)] = {"error": e.message}
            return _dumps(out)
        except Exception as e:  # noqa: BLE001
            return _error(e)

    @mcp.tool(name="visu_get_links", annotations={"title": "Links unter einem Objekt auflösen", **ro})
    async def visu_get_links(parent_id: int) -> str:
        """Alle direkten Link-Kinder von `parent_id` mit Ziel (ID, Name, Typ, Eltern) und bei Variablen
        Profil, Typ und formatiertem Wert. Höchstens 200 Links pro Aufruf."""
        try:
            out = []
            for cid in await guard.call("IPS_GetChildrenIDs", [parent_id]) or []:
                obj = await guard.call("IPS_GetObject", [cid])
                if not isinstance(obj, dict) or obj.get("ObjectType") != 6:
                    continue
                if len(out) >= 200:
                    out.sort(key=_order)
                    return _dumps({"parent_id": parent_id, "links": out, "truncated": "Limit 200 erreicht."})
                entry: dict[str, Any] = {"link_id": cid, "link_name": obj.get("ObjectName"), **_display(obj)}
                try:
                    target = (await guard.call("IPS_GetLink", [cid]) or {}).get("TargetID")
                    entry["target_id"] = target
                    tobj = await guard.call("IPS_GetObject", [target])
                    tobj = tobj if isinstance(tobj, dict) else {}
                    entry.update(target_name=tobj.get("ObjectName"), target_type=_kind(tobj),
                                 target_parent=tobj.get("ParentID"))
                    if tobj.get("ObjectType") == 2:
                        meta = await guard.call("IPS_GetVariable", [target])
                        meta = meta if isinstance(meta, dict) else {}
                        entry["variable_type"] = VARIABLE_TYPES.get(meta.get("VariableType"), meta.get("VariableType"))
                        entry["profile"] = meta.get("VariableCustomProfile") or meta.get("VariableProfile")
                        try:
                            entry["formatted"] = await guard.call("GetValueFormatted", [target])
                        except IPSError:
                            entry["formatted"] = None
                except IPSError as e:
                    entry["error"] = e.message
                out.append(entry)
            out.sort(key=_order)
            return _dumps({"parent_id": parent_id, "count": len(out), "links": out})
        except Exception as e:  # noqa: BLE001
            return _error(e)

    @mcp.tool(name="visu_find_by_name", annotations={"title": "Objekt über Namen finden", **ro})
    async def visu_find_by_name(name: str, parent_id: int = 0) -> str:
        """Objekt-ID über den exakten Namen unter einem Eltern-Objekt (IPS_GetObjectIDByName)."""
        try:
            oid = await guard.call("IPS_GetObjectIDByName", [name, parent_id])
            return _dumps({"name": name, "parent_id": parent_id, "object_id": oid})
        except Exception as e:  # noqa: BLE001
            return _error(e)

    @mcp.tool(name="visu_get_instance", annotations={"title": "Modulinfo einer Instanz", **ro})
    async def visu_get_instance(instance_id: int) -> str:
        """Nur Modulinfo (ID, Name, Typ) und Status einer Instanz – nie die Konfiguration."""
        try:
            return _dumps(await guard.call("IPS_GetInstance", [instance_id]))
        except Exception as e:  # noqa: BLE001
            return _error(e)

    @mcp.tool(name="visu_get_profile", annotations={"title": "Variablenprofil", **ro})
    async def visu_get_profile(profile_name: str) -> str:
        """Definition eines Variablenprofils (Einheit, Min/Max, Assoziationen, Icon)."""
        try:
            if not await guard.call("IPS_VariableProfileExists", [profile_name]):
                return f"Profil nicht gefunden: {profile_name}"
            return _dumps(await guard.call("IPS_GetVariableProfile", [profile_name]))
        except Exception as e:  # noqa: BLE001
            return _error(e)

    @mcp.tool(name="visu_wfc_items", annotations={"title": "WebFront-Items", **ro})
    async def visu_wfc_items(wfc_instance_id: int) -> str:
        """Items (Seiten/Struktur) eines WebFront-Konfigurators; Konfigurations-JSON ist gefiltert."""
        try:
            return _dumps(await guard.call("WFC_GetItems", [wfc_instance_id]))
        except Exception as e:  # noqa: BLE001
            return _error(e)

    # --- Schreiben (nur unterhalb der Zielkategorie) -------------------------------

    @mcp.tool(name="visu_ensure_category", annotations={"title": "Kategorie sicherstellen", **rw})
    async def visu_ensure_category(parent_id: int, name: str) -> str:
        """Kategorie `name` unter `parent_id` anlegen, falls sie fehlt (idempotent). Eltern in der Zielkategorie."""
        try:
            await _require_scope(parent_id)
            existing = await _find_child(parent_id, name, 0)
            if existing is not None:
                return _dumps({"category_id": existing, "created": False})
            new_id = await _create_under("IPS_CreateCategory", "IPS_DeleteCategory", parent_id, name)
            return _dumps({"category_id": new_id, "created": True})
        except Exception as e:  # noqa: BLE001
            return _error(e)

    @mcp.tool(name="visu_ensure_link", annotations={"title": "Link sicherstellen", **rw})
    async def visu_ensure_link(parent_id: int, name: str, target_id: int) -> str:
        """Link `name` unter `parent_id` auf `target_id` anlegen oder dessen Ziel anpassen (idempotent)."""
        try:
            await _require_scope(parent_id)
            if not await guard.call("IPS_ObjectExists", [target_id]):
                return f"Abgelehnt: Ziel-Objekt {target_id} existiert nicht."
            existing = await _find_child(parent_id, name, 6)
            if existing is not None:
                link = await guard.call("IPS_GetLink", [existing])
                current = link.get("TargetID") if isinstance(link, dict) else None
                if current != target_id:
                    await guard.call("IPS_SetLinkTargetID", [existing, target_id])
                return _dumps({"link_id": existing, "created": False, "target_changed": current != target_id})
            new_id = await _create_under("IPS_CreateLink", "IPS_DeleteLink", parent_id, name)
            try:
                await guard.call("IPS_SetLinkTargetID", [new_id, target_id])
            except Exception:
                await guard.call("IPS_DeleteLink", [new_id])
                raise
            return _dumps({"link_id": new_id, "created": True, "target_changed": True})
        except Exception as e:  # noqa: BLE001
            return _error(e)

    @mcp.tool(name="visu_update_object", annotations={"title": "Objekt anpassen", **rw})
    async def visu_update_object(
        object_id: int,
        name: str | None = None,
        info: str | None = None,
        icon: str | None = None,
        position: int | None = None,
        hidden: bool | None = None,
        disabled: bool | None = None,
    ) -> str:
        """Name, Info, Icon, Position, versteckt/deaktiviert eines Objekts in der Zielkategorie setzen."""
        steps = [("IPS_SetName", name), ("IPS_SetInfo", info), ("IPS_SetIcon", icon),
                 ("IPS_SetPosition", position), ("IPS_SetHidden", hidden), ("IPS_SetDisabled", disabled)]
        try:
            done = []
            for method, value in steps:
                if value is not None:
                    await guard.call(method, [object_id, value])
                    done.append(method)
            return _dumps({"object_id": object_id, "applied": done})
        except Exception as e:  # noqa: BLE001
            return _error(e)

    @mcp.tool(name="visu_move_object", annotations={"title": "Objekt verschieben", **rw})
    async def visu_move_object(object_id: int, new_parent_id: int) -> str:
        """Objekt innerhalb der Zielkategorie verschieben (Quelle und Ziel müssen darin liegen)."""
        try:
            await guard.call("IPS_SetParent", [object_id, new_parent_id])
            return _dumps({"object_id": object_id, "parent_id": new_parent_id})
        except Exception as e:  # noqa: BLE001
            return _error(e)

    @mcp.tool(name="visu_delete_object", annotations={"title": "Kategorie oder Link löschen", **rd})
    async def visu_delete_object(object_id: int) -> str:
        """Kategorie (muss leer sein) oder Link in der Zielkategorie löschen. Die Zielkategorie selbst nie."""
        try:
            obj = await guard.call("IPS_GetObject", [object_id])
            otype = obj.get("ObjectType") if isinstance(obj, dict) else None
            method = {0: "IPS_DeleteCategory", 6: "IPS_DeleteLink"}.get(otype)
            if method is None:
                return f"Abgelehnt: Objekttyp {OBJECT_TYPES.get(otype, otype)} wird nicht gelöscht."
            await guard.call(method, [object_id])
            return _dumps({"object_id": object_id, "deleted": True})
        except Exception as e:  # noqa: BLE001
            return _error(e)

    return mcp
