"""Work-order access rules and response shaping.

Scope is derived only from the trusted ``AuthContext``:

* residents: their own resident_id AND one of their units AND their property
  (same-property, other-unit records are never returned);
* property managers: assigned properties AND assigned units.

Responses are built from an explicit field allowlist, so new or private
fields (e.g. ``internal_notes``) are never returned unless added here.
"""

from __future__ import annotations

from typing import Protocol

from app.auth import AuthContext, AuthorizationError

WORK_ORDER_STATUSES = ("open", "scheduled", "in_progress", "completed", "cancelled")
ACTIVE_STATUSES = frozenset({"open", "scheduled", "in_progress"})
STATUS_FILTERS = (*WORK_ORDER_STATUSES, "active")

RESIDENT_VISIBLE_FIELDS = (
    "work_order_id",
    "category",
    "summary",
    "description",
    "priority",
    "status",
    "created_at",
    "updated_at",
    "completed_at",
    "scheduled_window",
    "entry_permission",
    "resident_visible_update",
)
# Managers also need to know which unit a record is for; still no internal notes.
MANAGER_VISIBLE_FIELDS = (*RESIDENT_VISIBLE_FIELDS, "unit_number")

FIRESTORE_IN_LIMIT = 30


class InvalidStatusFilter(ValueError):
    pass


class WorkOrderRepository(Protocol):
    def list_for_units(self, unit_ids: list[str]) -> list[dict]: ...


class FirestoreWorkOrderRepository:
    def __init__(self, client, timeout: float | None = None):
        self._client = client
        self._timeout = timeout

    def list_for_units(self, unit_ids: list[str]) -> list[dict]:
        from google.cloud.firestore_v1.base_query import FieldFilter

        rows: list[dict] = []
        unit_numbers = self._unit_numbers(unit_ids)
        for i in range(0, len(unit_ids), FIRESTORE_IN_LIMIT):
            batch = unit_ids[i : i + FIRESTORE_IN_LIMIT]
            query = self._client.collection("work_orders").where(
                filter=FieldFilter("unit_id", "in", batch)
            )
            for snap in query.stream(timeout=self._timeout):
                row = snap.to_dict()
                row["unit_number"] = unit_numbers.get(row.get("unit_id"))
                rows.append(row)
        return rows

    def _unit_numbers(self, unit_ids: list[str]) -> dict[str, str]:
        refs = [self._client.collection("units").document(u) for u in unit_ids]
        return {
            s.id: s.get("unit_number")
            for s in self._client.get_all(refs, timeout=self._timeout)
            if s.exists
        }


def normalize_status(status: str | None) -> str | None:
    if status is None:
        return None
    value = status.strip().lower().replace("-", "_").replace(" ", "_")
    if not value:
        return None
    if value not in STATUS_FILTERS:
        raise InvalidStatusFilter(
            f"invalid status {status!r}; use one of: {', '.join(STATUS_FILTERS)} or omit it"
        )
    return value


def _in_scope(auth: AuthContext, row: dict) -> bool:
    if row.get("property_id") not in auth.allowed_property_ids:
        return False
    if row.get("unit_id") not in auth.allowed_unit_ids:
        return False
    if auth.role == "resident":
        return row.get("resident_id") == auth.resident_id
    return auth.role == "property_manager"


def _matches(row: dict, status: str | None) -> bool:
    if status is None:
        return True
    if status == "active":
        return row.get("status") in ACTIVE_STATUSES
    return row.get("status") == status


def project(row: dict, role: str) -> dict:
    fields = RESIDENT_VISIBLE_FIELDS if role == "resident" else MANAGER_VISIBLE_FIELDS
    return {f: row.get(f) for f in fields}


def list_visible_work_orders(
    auth: AuthContext, repo: WorkOrderRepository, status: str | None = None
) -> list[dict]:
    """Return allowlisted work orders the actor may see, newest first."""
    status = normalize_status(status)
    if not auth.allowed_unit_ids:
        raise AuthorizationError(f"{auth.subject} has no unit scope")
    rows = repo.list_for_units(sorted(auth.allowed_unit_ids))
    visible = [r for r in rows if _in_scope(auth, r) and _matches(r, status)]
    visible.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return [project(r, auth.role) for r in visible]
