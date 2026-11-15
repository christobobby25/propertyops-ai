"""Request authentication for the web app.

* ``demo`` (development only): the identity is a demo fixture chosen with the
  ``X-Demo-Identity`` header (or DEV_AUTH_CONTEXT). Refused when
  APP_ENV != development.
* ``google_id_token``: ``Authorization: Bearer <ID token>`` verified against
  Google's keys and OAUTH_AUDIENCE. The token's ``sub`` is the subject; scope
  comes from the identity directory, never from the token's claims or the client.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.config import Settings

DEMO_HEADER = "X-Demo-Identity"


class AuthenticationError(PermissionError):
    pass


class StartupConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Principal:
    subject: str
    auth_mode: str


def check_auth_config(s: Settings) -> None:
    if s.auth_mode == "demo" and s.app_env != "development":
        raise StartupConfigError("AUTH_MODE=demo is only allowed with APP_ENV=development")
    if s.auth_mode == "google_id_token" and not s.oauth_audience:
        raise StartupConfigError("AUTH_MODE=google_id_token requires OAUTH_AUDIENCE")


class Authenticator:
    def __init__(self, settings: Settings, verify_token=None):
        check_auth_config(settings)
        self.s = settings
        self._verify = verify_token or _verify_google_id_token

    def authenticate(self, headers) -> Principal:
        if self.s.auth_mode == "demo":
            return self._demo(headers)
        return self._google(headers)

    def _demo(self, headers) -> Principal:
        from app.demo_auth import load_demo_context

        context_id = headers.get(DEMO_HEADER) or self.s.dev_auth_context
        if not context_id:
            raise AuthenticationError("no demo identity selected")
        try:
            return Principal(load_demo_context(context_id).subject, "demo")
        except KeyError:
            raise AuthenticationError("unknown demo identity") from None

    def _google(self, headers) -> Principal:
        auth = headers.get("Authorization", "")
        scheme, _, token = auth.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise AuthenticationError("missing bearer token")
        try:
            claims = self._verify(token, self.s.oauth_audience)
        except Exception:
            raise AuthenticationError("invalid token") from None
        if not claims.get("sub") or claims.get("email_verified") is False:
            raise AuthenticationError("token missing a verified subject")
        return Principal(f"google:{claims['sub']}", "google_id_token")


def _verify_google_id_token(token: str, audience: str) -> dict:
    from google.auth.transport import requests as ga_requests
    from google.oauth2 import id_token

    return id_token.verify_oauth2_token(token, ga_requests.Request(), audience=audience)
