"""Backend-controlled identity resolution for agent tools.

Tools call ``IdentityProvider.resolve(tool_context)`` on *every* invocation;
nothing about identity is cached in the conversation or chosen by the model.
Providers fail closed by raising ``IdentityError``.

* ``StaticIdentityProvider`` — the server binds one verified identity to an
  agent instance (used by the live evaluator and by tests).
* ``DevFixtureIdentityProvider`` — local ADK Web only: resolves the demo
  fixture named in ``DEV_AUTH_CONTEXT`` and refuses unless
  ``APP_ENV=development``.
* ``FailClosedIdentityProvider`` — default outside development until a real
  identity provider is integrated.
"""

from __future__ import annotations

from typing import Any, Protocol

from app.auth import AuthContext
from app.config import Settings


class IdentityError(PermissionError):
    """No valid, verified identity is available for this request."""


class IdentityProvider(Protocol):
    def resolve(self, tool_context: Any) -> AuthContext: ...


def _validated(ctx: object) -> AuthContext:
    if not isinstance(ctx, AuthContext):
        raise IdentityError("identity is not a verified AuthContext")
    if not ctx.allowed_property_ids:
        raise IdentityError("identity has no property scope")
    if ctx.role == "resident" and (not ctx.resident_id or not ctx.allowed_unit_ids):
        raise IdentityError("resident identity lacks resident or unit scope")
    return ctx


class StaticIdentityProvider:
    def __init__(self, auth: AuthContext | None):
        self._auth = auth

    def resolve(self, tool_context: Any) -> AuthContext:
        if self._auth is None:
            raise IdentityError("no identity bound to this agent")
        return _validated(self._auth)


class FailClosedIdentityProvider:
    def resolve(self, tool_context: Any) -> AuthContext:
        raise IdentityError("no identity provider configured for this environment")


class DevFixtureIdentityProvider:
    """Development-only. Re-reads settings on each call; never trusts session state."""

    def __init__(self, settings_loader):
        self._settings_loader = settings_loader

    def resolve(self, tool_context: Any) -> AuthContext:
        from app.demo_auth import load_demo_context

        s: Settings = self._settings_loader()
        if s.app_env != "development":
            raise IdentityError("demo identities are disabled outside APP_ENV=development")
        if not s.dev_auth_context:
            raise IdentityError("DEV_AUTH_CONTEXT is not set")
        try:
            return _validated(load_demo_context(s.dev_auth_context))
        except KeyError as e:
            raise IdentityError(str(e)) from None


def identity_provider_for(settings: Settings, settings_loader) -> IdentityProvider:
    if settings.app_env == "development":
        return DevFixtureIdentityProvider(settings_loader)
    return FailClosedIdentityProvider()
