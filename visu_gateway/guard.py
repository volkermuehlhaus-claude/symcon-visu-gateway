"""Der einzige Weg zu IP-Symcon: Methoden-Whitelist, Scope-Prüfung, Antwortfilter, Audit.

Jeder Aufruf der Tools läuft durch ``GuardedClient.call``. Was hier nicht gelistet ist, wird nie an
Symcon weitergegeben. Bewusst NICHT enthalten: ``IPS_GetConfiguration``, ``IPS_SetProperty``,
``IPS_SetConfiguration``, ``IPS_RunScript*``, ``IPS_GetScriptContent``, ``IPS_SetScriptContent``,
``IPS_CreateScript``, ``IPS_CreateInstance``, ``SetValue``, ``RequestAction``, ``IPS_DeleteObject`` …
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from . import secret_filter as sf
from .client import IPSError

log = logging.getLogger("visu_gateway")


class PolicyError(RuntimeError):
    """Aufruf von der Richtlinie abgelehnt (nicht an Symcon weitergegeben)."""


class RawClient(Protocol):
    async def call(self, method: str, params: list[Any] | None = None) -> Any: ...


# --- Whitelist ---------------------------------------------------------------

#: Reine Lesefunktionen. Antworten laufen durch Reducer + Secret-Filter.
READ_METHODS: frozenset[str] = frozenset({
    "IPS_GetKernelVersion",
    "IPS_ObjectExists",
    "IPS_GetObject",
    "IPS_GetObjectList",
    "IPS_GetChildrenIDs",
    "IPS_GetObjectIDByName",
    "IPS_GetName",
    "IPS_GetParent",
    "IPS_GetVariable",
    "GetValue",
    "GetValueFormatted",
    "IPS_GetLink",
    "IPS_GetInstance",  # nur Modulinfo, siehe _reduce_instance
    "IPS_GetInstanceListByModuleID",
    "IPS_VariableProfileExists",
    "IPS_GetVariableProfile",
    "IPS_GetVariableProfileList",
    "WFC_GetItems",
    "VISU_GetSnapshot",  # nur mit allow_layout, nur die konfigurierte Instanz, nur gridConfig, siehe _reduce_snapshot
})


@dataclass(frozen=True)
class WriteSpec:
    """Welche Parameter eines Schreibaufrufs im Gültigkeitsbereich liegen müssen."""

    object_arg: int | None = None  # Index des Objekt-ID-Parameters, der unterhalb der Zielkategorie liegen muss
    parent_arg: int | None = None  # Index eines Eltern-Parameters; muss in der Zielkategorie (oder sie selbst) liegen
    creates: bool = False  # legt ein Objekt an; die neue ID wird bis zum Verschieben als "frisch" gemerkt
    deletes: bool = False  # darf die Zielkategorie selbst nicht treffen
    visu_instance: bool = False  # trifft die konfigurierte Kachelvisu-Instanz (nur Layout, nur mit allow_layout)


WRITE_SPECS: dict[str, WriteSpec] = {
    "IPS_CreateCategory": WriteSpec(creates=True),
    "IPS_CreateLink": WriteSpec(creates=True),
    "IPS_SetParent": WriteSpec(object_arg=0, parent_arg=1),
    "IPS_SetName": WriteSpec(object_arg=0),
    "IPS_SetInfo": WriteSpec(object_arg=0),
    "IPS_SetIcon": WriteSpec(object_arg=0),
    "IPS_SetPosition": WriteSpec(object_arg=0),
    "IPS_SetHidden": WriteSpec(object_arg=0),
    "IPS_SetDisabled": WriteSpec(object_arg=0),
    "IPS_SetLinkTargetID": WriteSpec(object_arg=0),
    "IPS_DeleteCategory": WriteSpec(object_arg=0, deletes=True),
    "IPS_DeleteLink": WriteSpec(object_arg=0, deletes=True),
    "IPS_CreateInstance": WriteSpec(creates=True),
    "IPS_DeleteInstance": WriteSpec(object_arg=0, deletes=True),
    "VISU_SaveGridConfiguration": WriteSpec(visu_instance=True),
}

LAYOUT_METHODS = frozenset({"VISU_GetSnapshot", "VISU_SaveGridConfiguration"})
MAX_GRID_JSON = 200_000

#: Gruppen-Instanzen (Sammelkacheln): nur das Dummy Module, nur wenn ``allow_groups`` gesetzt ist.
DUMMY_MODULE_ID = "{485D0419-BE97-4548-AA9C-C083EB82E61E}"
GROUP_METHODS = frozenset({"IPS_CreateInstance", "IPS_DeleteInstance"})

# Nur Objekttypen, die das Gateway selbst anlegt, dürfen als "frisch" gelten.
_OBJ_CATEGORY, _OBJ_INSTANCE, _OBJ_LINK = 0, 1, 6
_DELETE_TYPE = {"IPS_DeleteCategory": _OBJ_CATEGORY, "IPS_DeleteLink": _OBJ_LINK, "IPS_DeleteInstance": _OBJ_INSTANCE}


def _is_id(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


class GuardedClient:
    def __init__(
        self,
        raw: RawClient,
        target_category_id: int,
        enable_write: bool = False,
        audit_log: Path | None = None,
        max_string: int = sf.DEFAULT_MAX_STRING,
        allow_groups: bool = False,
        visu_instance_id: int = 0,
        allow_layout: bool = False,
    ) -> None:
        self._raw = raw
        self.target = target_category_id
        self.enable_write = enable_write
        self.allow_groups = allow_groups
        self.visu_instance_id = visu_instance_id
        self.allow_layout = allow_layout and visu_instance_id > 0
        self._audit_path = audit_log
        self._max = max_string
        self._fresh: set[int] = set()  # selbst angelegt, noch nicht in die Zielkategorie verschoben
        self._names: dict[int, str] = {}

    # --- Scope ---------------------------------------------------------------

    async def in_scope(self, oid: int, include_root: bool = True) -> bool:
        """Liegt ``oid`` in der Zielkategorie (oder ist sie selbst, wenn ``include_root``)?"""
        if not _is_id(oid) or oid <= 0:
            return False
        current = oid
        for _ in range(64):  # Schutz gegen kaputte Eltern-Ketten
            if current == self.target:
                return include_root or current != oid
            if current <= 0:
                return False
            try:
                obj = await self._raw.call("IPS_GetObject", [current])
            except IPSError:  # Objekt existiert nicht
                return False
            if not isinstance(obj, dict):
                return False
            current = obj.get("ParentID", 0)
        return False

    async def _allowed_object(self, oid: Any) -> bool:
        return _is_id(oid) and (oid in self._fresh or await self.in_scope(oid, include_root=True))

    # --- Aufruf ---------------------------------------------------------------

    def _check_layout(self, method: str, params: list[Any]) -> None:
        """Layout-Aufrufe nur mit allow_layout und nur für die konfigurierte Kachelvisu-Instanz."""
        if not self.allow_layout:
            raise PolicyError(f"{method}: Layout-Zugriff ist im Gateway nicht freigegeben (allow_layout: false).")
        if not params or params[0] != self.visu_instance_id or not _is_id(params[0]):
            raise PolicyError(
                f"{method}: nur die konfigurierte Kachelvisu-Instanz {self.visu_instance_id} ist erlaubt.")
        if method == "VISU_SaveGridConfiguration":
            doc = params[1] if len(params) == 2 else None
            if not isinstance(doc, str) or len(doc) > MAX_GRID_JSON:
                raise PolicyError(f"{method}: erwartet (Instanz-ID, JSON-Text bis {MAX_GRID_JSON} Zeichen).")
            try:
                parsed = json.loads(doc)
            except ValueError:
                raise PolicyError(f"{method}: das Layout ist kein gültiges JSON.") from None
            if not isinstance(parsed, dict):
                raise PolicyError(f"{method}: das Layout muss ein JSON-Objekt sein.")
        elif len(params) != 1:
            raise PolicyError(f"{method}: erwartet nur die Instanz-ID.")

    async def call(self, method: str, params: list[Any] | None = None) -> Any:
        params = list(params or [])
        if method in LAYOUT_METHODS:
            self._check_layout(method, params)
        if method in READ_METHODS:
            return await self._read(method, params)
        spec = WRITE_SPECS.get(method)
        if spec is None:
            raise PolicyError(f"Methode nicht erlaubt: {method}")
        return await self._write(method, spec, params)

    # --- Lesen ----------------------------------------------------------------

    async def _read(self, method: str, params: list[Any]) -> Any:
        result = await self._raw.call(method, params)
        redactions: list[tuple[str, str]] = []
        out = await self._filter_read(method, params, result, redactions)
        if redactions:
            log.info("%s %s: %d Stellen maskiert (%s)", method, params, len(redactions),
                     ", ".join(f"{p}: {why}" for p, why in redactions[:5]))
        return out

    async def _object_name(self, oid: Any) -> str | None:
        if not _is_id(oid):
            return None
        if oid not in self._names:
            try:
                self._names[oid] = str(await self._raw.call("IPS_GetName", [oid]))
            except IPSError:
                return None
        return self._names[oid]

    async def _filter_read(self, method: str, params: list[Any], result: Any,
                           red: list[tuple[str, str]]) -> Any:
        mx = self._max
        if method in {"GetValue", "GetValueFormatted"}:
            oid = params[0] if params else None
            return sf.clean_value(result, await self._object_name(oid), f"{method}[{oid}]", red, mx)
        if method == "IPS_GetInstance":
            return self._reduce_instance(result)
        if method == "VISU_GetSnapshot":
            return sf.clean_recursive(self._reduce_snapshot(result), method, red, mx)
        if method == "IPS_GetVariable" and isinstance(result, dict):
            result = {k: v for k, v in result.items() if k != "VariableValue"}  # Wert nur über GetValue
        if method == "WFC_GetItems" and isinstance(result, list):
            result = [sf.decode_json_fields(i) if isinstance(i, dict) else i for i in result]
        return sf.clean_recursive(result, method, red, mx)

    @staticmethod
    def _reduce_snapshot(result: Any) -> Any:
        """Vom Snapshot der Kachelvisu nur die Layout-Konfiguration (gridConfig), nie den Rest."""
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except ValueError:
                return {}
        grid = result.get("gridConfig", "") if isinstance(result, dict) else ""
        if isinstance(grid, str):  # als Objekt zurückgeben, damit kein Textfilter das JSON kürzt
            try:
                grid = json.loads(grid) if grid else {}
            except ValueError:
                grid = {}
        return {"gridConfig": grid if isinstance(grid, dict) else {}}

    @staticmethod
    def _reduce_instance(result: Any) -> Any:
        """Nur Modulinfo und Status – nie Konfiguration."""
        if not isinstance(result, dict):
            return result
        info = result.get("ModuleInfo") if isinstance(result.get("ModuleInfo"), dict) else {}
        return {
            "InstanceID": result.get("InstanceID"),
            "InstanceStatus": result.get("InstanceStatus"),
            "ConnectionID": result.get("ConnectionID"),
            "ModuleInfo": {k: info.get(k) for k in ("ModuleID", "ModuleName", "ModuleType") if k in info},
        }

    # --- Schreiben ------------------------------------------------------------

    async def _write(self, method: str, spec: WriteSpec, params: list[Any]) -> Any:
        if not self.enable_write:
            raise PolicyError("Schreiben ist im Gateway nicht freigegeben (enable_write: false).")

        if method in GROUP_METHODS and not self.allow_groups:
            raise PolicyError(f"{method}: Gruppen-Instanzen sind im Gateway nicht freigegeben (allow_groups: false).")
        if method == "IPS_CreateInstance" and params != [DUMMY_MODULE_ID]:
            raise PolicyError(f"IPS_CreateInstance: nur das Dummy Module {DUMMY_MODULE_ID} darf angelegt werden.")

        if spec.object_arg is not None:
            oid = params[spec.object_arg] if len(params) > spec.object_arg else None
            if not await self._allowed_object(oid):
                raise PolicyError(f"{method}: Objekt {oid!r} liegt außerhalb der Zielkategorie {self.target}.")
            if spec.deletes:
                if oid == self.target:
                    raise PolicyError("Die Zielkategorie selbst wird nicht über das Gateway gelöscht.")
                obj = await self._raw.call("IPS_GetObject", [oid])
                if not isinstance(obj, dict) or obj.get("ObjectType") != _DELETE_TYPE[method]:
                    raise PolicyError(f"{method}: Objekt {oid} hat nicht den passenden Typ.")
                if method == "IPS_DeleteInstance":
                    inst = await self._raw.call("IPS_GetInstance", [oid])
                    module = (inst.get("ModuleInfo") or {}).get("ModuleID") if isinstance(inst, dict) else None
                    if str(module).upper() != DUMMY_MODULE_ID:
                        raise PolicyError(f"IPS_DeleteInstance: Instanz {oid} ist kein Dummy Module.")
                    if obj.get("ChildrenIDs"):
                        raise PolicyError(f"IPS_DeleteInstance: Instanz {oid} ist nicht leer.")
            if method == "IPS_SetParent" and oid == self.target:
                raise PolicyError("Die Zielkategorie selbst wird nicht verschoben.")

        if spec.parent_arg is not None:
            parent = params[spec.parent_arg] if len(params) > spec.parent_arg else None
            if not _is_id(parent) or not await self.in_scope(parent, include_root=True):
                raise PolicyError(f"{method}: Ziel-Elternobjekt {parent!r} liegt außerhalb der Zielkategorie.")

        if method == "IPS_CreateLink" and params:
            raise PolicyError("IPS_CreateLink erwartet keine Parameter; Ziel und Eltern werden danach gesetzt.")
        if method == "IPS_CreateCategory" and params:
            raise PolicyError("IPS_CreateCategory erwartet keine Parameter.")

        if method == "IPS_SetLinkTargetID" and not _is_id(params[1] if len(params) > 1 else None):
            raise PolicyError("IPS_SetLinkTargetID: Ziel-ID fehlt.")

        self._audit("request", method, params)  # schlägt das Protokoll fehl, wird nicht geschrieben
        result = await self._raw.call(method, params)
        if spec.creates and _is_id(result):
            self._fresh.add(result)
        if method == "IPS_SetParent":
            self._fresh.discard(params[0])
        if spec.deletes:
            self._fresh.discard(params[0])
        if method == "IPS_SetName":
            self._names.pop(params[0], None)
        self._audit("result", method, params, result)
        return result

    # --- Audit ----------------------------------------------------------------

    def _audit(self, phase: str, method: str, params: list[Any], result: Any = None) -> None:
        entry = {"ts": datetime.now(UTC).isoformat(), "phase": phase, "method": method, "params": params}
        if phase == "result":
            entry["result"] = result
        line = json.dumps(entry, ensure_ascii=False, default=str)
        log.info("WRITE %s", line)
        if self._audit_path:
            try:
                with self._audit_path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError:
                log.exception("Audit-Log nicht schreibbar")
                if phase == "request":
                    raise PolicyError("Audit-Log nicht schreibbar – Schreibvorgang wird nicht ausgeführt.") from None
