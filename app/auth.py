"""Trusted authorization context and property-scope enforcement.

An ``AuthContext`` is constructed by the backend from a *verified* identity
(an identity provider in production; ``app.demo_auth`` fixtures in local dev
and tests). It must never be built from model output or user-supplied tool
arguments. Retrieval and data-access code take an ``AuthContext`` and use
``authorize_property`` to decide which property a request may touch.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Role = Literal["resident", "property_manager"]
ROLES: frozenset[str] = frozenset({"resident", "property_manager"})


class AuthorizationError(PermissionError):
    """The authenticated actor is not permitted to perform the request."""


class PropertyScopeRequired(AuthorizationError):
    """The actor can access several properties; the request must name one."""


@dataclass(frozen=True)
class AuthContext:
    subject: str
    role: Role
    allowed_property_ids: frozenset[str]
    allowed_unit_ids: frozenset[str] = frozenset()
    resident_id: str | None = None

    def __post_init__(self) -> None:
        if not self.subject:
            raise ValueError("subject is required")
        if self.role not in ROLES:
            raise ValueError(f"unknown role: {self.role!r}")
        if self.role == "resident" and not self.resident_id:
            raise ValueError("resident contexts require resident_id")
        # Normalise to frozensets so callers cannot mutate scope after creation.
        object.__setattr__(self, "allowed_property_ids", frozenset(self.allowed_property_ids))
        object.__setattr__(self, "allowed_unit_ids", frozenset(self.allowed_unit_ids))


def authorize_property(auth: AuthContext, requested_property_id: str | None = None) -> str:
    """Return the property ID this request is allowed to use.

    ``requested_property_id`` is an untrusted hint (for example a model tool
    argument). It is only honoured if it is inside the actor's trusted scope.
    """
    allowed = auth.allowed_property_ids
    if not allowed:
        raise AuthorizationError(f"{auth.subject} has no property access")

    if requested_property_id is None:
        if len(allowed) == 1:
            return next(iter(allowed))
        raise PropertyScopeRequired(
            f"{auth.subject} can access {len(allowed)} properties; specify one of {sorted(allowed)}"
        )

    if requested_property_id not in allowed:
        raise AuthorizationError(
            f"{auth.subject} is not authorized for property {requested_property_id!r}"
        )
    return requested_property_id
