"""Minimaler Symcon-Simulator für die Gateway-Tests (nur die Methoden, die das Gateway benutzen darf)."""

from __future__ import annotations

from typing import Any

from visu_gateway.client import IPSError

CATEGORY, INSTANCE, VARIABLE, SCRIPT, EVENT, MEDIA, LINK = range(7)


class FakeIPS:
    """Objektbaum im Speicher. ``calls`` protokolliert jeden Aufruf, der „Symcon“ erreicht."""

    def __init__(self) -> None:
        self.objs: dict[int, dict[str, Any]] = {}
        self.calls: list[tuple[str, list[Any]]] = []
        self.values: dict[int, Any] = {}
        self.fail_methods: set[str] = set()
        self._next = 1000
        self.add(0, "root", CATEGORY, parent=0)

    def add(self, oid: int | None, name: str, otype: int, parent: int, **extra: Any) -> int:
        if oid is None:
            self._next += 1
            oid = self._next
        self.objs[oid] = {"ObjectID": oid, "ObjectName": name, "ObjectType": otype, "ParentID": parent,
                          "ObjectInfo": "", "ObjectSummary": "", **extra}
        return oid

    def children(self, oid: int) -> list[int]:
        return [i for i, o in self.objs.items() if o["ParentID"] == oid and i != oid]

    def _get(self, oid: Any) -> dict[str, Any]:
        if oid not in self.objs:
            raise IPSError(-32000, f"Object #{oid} does not exist")
        return self.objs[oid]

    async def call(self, method: str, params: list[Any] | None = None) -> Any:
        params = list(params or [])
        self.calls.append((method, params))
        if method in self.fail_methods:
            raise IPSError(-32000, f"{method} failed (simuliert)")
        handler = getattr(self, f"m_{method}", None)
        if handler is None:
            raise IPSError(-32601, f"Method not found: {method}")
        return handler(*params)

    # --- lesen ---
    def m_IPS_GetKernelVersion(self):
        return "9.0"

    def m_IPS_ObjectExists(self, oid):
        return oid in self.objs

    def m_IPS_GetObject(self, oid):
        o = dict(self._get(oid))
        o["ChildrenIDs"] = self.children(oid)
        return o

    def m_IPS_GetChildrenIDs(self, oid):
        self._get(oid)
        return self.children(oid)

    def m_IPS_GetName(self, oid):
        return self._get(oid)["ObjectName"]

    def m_IPS_GetObjectIDByName(self, name, parent):
        for i in self.children(parent):
            if self.objs[i]["ObjectName"] == name:
                return i
        raise IPSError(-32000, "Object not found")

    def m_GetValue(self, oid):
        self._get(oid)
        return self.values[oid]

    def m_GetValueFormatted(self, oid):
        return str(self.m_GetValue(oid))

    def m_IPS_GetVariable(self, oid):
        return {"VariableType": 3, "VariableValue": self.values.get(oid), "VariableProfile": ""}

    def m_IPS_GetLink(self, oid):
        return {"LinkID": oid, "TargetID": self._get(oid)["TargetID"]}

    def m_IPS_GetInstance(self, oid):
        return {
            "InstanceID": oid, "InstanceStatus": 102, "ConnectionID": 0,
            "ModuleInfo": {"ModuleID": self._get(oid).get("ModuleID", "{ABC}"), "ModuleName": "Viessmann",
                           "ModuleType": 3},
            "Configuration": '{"Password": "hunter2"}',  # darf nie ankommen
        }

    def m_WFC_GetItems(self, oid):
        return [{"ID": "root", "Configuration": '{"title": "Haus", "url": "http://u:p@host/x?token=abc"}'}]

    # --- schreiben ---
    def m_IPS_CreateCategory(self):
        return self.add(None, "Unnamed Object", CATEGORY, parent=0)

    def m_IPS_CreateLink(self):
        return self.add(None, "Unnamed Object", LINK, parent=0, TargetID=0)

    def m_IPS_CreateInstance(self, module_id):
        return self.add(None, "Unnamed Object", INSTANCE, parent=0, ModuleID=module_id)

    def m_IPS_DeleteInstance(self, oid):
        if self.children(oid):
            raise IPSError(-32000, "Instance not empty")
        del self.objs[oid]
        return True

    def m_IPS_SetParent(self, oid, parent):
        self._get(oid)["ParentID"] = parent
        return True

    def m_IPS_SetName(self, oid, name):
        self._get(oid)["ObjectName"] = name
        return True

    def m_IPS_SetInfo(self, oid, v):
        self._get(oid)["ObjectInfo"] = v
        return True

    m_IPS_SetIcon = m_IPS_SetPosition = m_IPS_SetHidden = m_IPS_SetDisabled = m_IPS_SetInfo

    def m_IPS_SetLinkTargetID(self, oid, target):
        self._get(oid)["TargetID"] = target
        return True

    def m_IPS_DeleteCategory(self, oid):
        if self.children(oid):
            raise IPSError(-32000, "Category not empty")
        del self.objs[oid]
        return True

    m_IPS_DeleteLink = m_IPS_DeleteCategory
