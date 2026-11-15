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


# --- Request-bound identity (web backend) -----------------------------------------


class IdentityDirectory(Protocol):
    """Maps a verified subject to its authorization scope (server-side data)."""

    def lookup(self, subject: str) -> AuthContext | None: ...


class DemoDirectory:
    """Demo fixtures keyed by subject. Development only (enforced by the web app)."""

    def lookup(self, subject: str) -> AuthContext | None:
        import json

        from app.demo_auth import DEMO_AUTH_PATH, context_from_fixture

        for f in json.loads(DEMO_AUTH_PATH.read_text()):
            if f["subject"] == subject:
                return context_from_fixture(f)
        return None


class FirestoreIdentityDirectory:
    """``user_identities/{subject}`` documents maintained by administrators:
    role, resident_id, allowed_property_ids, allowed_unit_ids, active."""

    COLLECTION = "user_identities"

    def __init__(self, client):
        self._db = client

    def lookup(self, subject: str) -> AuthContext | None:
        snap = self._db.collection(self.COLLECTION).document(subject).get()
        if not snap.exists or not snap.get("active"):
            return None
        d = snap.to_dict()
        return AuthContext(
            subject=subject,
            role=d["role"],
            resident_id=d.get("resident_id"),
            allowed_property_ids=frozenset(d.get("allowed_property_ids") or ()),
            allowed_unit_ids=frozenset(d.get("allowed_unit_ids") or ()),
        )


class SubjectDirectoryIdentityProvider:
    """Resolves the actor from ``tool_context.user_id`` — set by the backend when it
    calls ``Runner.run_async(user_id=<verified subject>)``, never by the model —
    and looks up the current scope on every call (revocations apply immediately)."""

    def __init__(self, directory: IdentityDirectory):
        self._directory = directory

    def resolve(self, tool_context: Any) -> AuthContext:
        subject = getattr(tool_context, "user_id", None)
        if not subject:
            raise IdentityError("no authenticated subject on this invocation")
        try:
            ctx = self._directory.lookup(subject)
        except Exception as e:
            raise IdentityError(f"identity lookup failed: {type(e).__name__}") from None
        if ctx is None:
            raise IdentityError("subject is not provisioned")
        return _validated(ctx)
