"""FastAPI app: resident chat UI, approve/cancel endpoint, and work-order list.

python -m scripts.serve            # local (see scripts/serve.py)
uvicorn app.web.server:create_app --factory   # deployment entry point
"""

import logging
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.agent.identity import IdentityDirectory
from app.agent.work_orders import (
    InvalidStatusFilter,
    WorkOrderRepository,
    list_visible_work_orders,
)
from app.auth import AuthContext, AuthorizationError
from app.config import Settings, get_settings
from app.web.auth import AuthenticationError, Authenticator
from app.web.chat import ChatError, ChatService

log = logging.getLogger("propertyops.web")
STATIC_DIR = Path(__file__).parent / "static"


class ChatRequest(BaseModel):
    session_id: str = Field(min_length=8, max_length=64)
    message: str = Field(min_length=1, max_length=2000)


class ConfirmRequest(BaseModel):
    session_id: str = Field(min_length=8, max_length=64)
    confirmation_id: str = Field(min_length=1, max_length=200)
    approve: bool


def _security_headers(response, google_signin: bool):
    script_src = "'self' https://accounts.google.com" if google_signin else "'self'"
    response.headers["Content-Security-Policy"] = (
        f"default-src 'self'; script-src {script_src}; style-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; frame-src https://accounts.google.com; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    return response


def _fault_config(s: Settings) -> dict[str, float]:
    import os

    from app.faults import parse

    spec = os.environ.get("FAULT_INJECTION")
    if not spec:
        return {}
    if s.app_env != "development":
        raise RuntimeError("FAULT_INJECTION is only allowed with APP_ENV=development")
    return parse(spec)


def create_app(
    settings: Settings | None = None,
    *,
    chat: ChatService | None = None,
    directory: IdentityDirectory | None = None,
    work_orders: WorkOrderRepository | None = None,
    authenticator: Authenticator | None = None,
) -> FastAPI:
    s = settings or get_settings()
    if s.app_env == "production":
        from app.logging_config import configure_logging

        configure_logging()
    authenticator = authenticator or Authenticator(s)  # refuses demo auth outside development

    if chat is None or directory is None or work_orders is None:
        from app.agent.factory import build_agent, firestore_factories
        from app.agent.identity import (
            DemoDirectory,
            FirestoreIdentityDirectory,
            SubjectDirectoryIdentityProvider,
        )
        from app.gcp import firestore_client

        f = firestore_factories(s)
        if directory is None:
            directory = (
                DemoDirectory()
                if s.auth_mode == "demo"
                else FirestoreIdentityDirectory(firestore_client(s))
            )
        work_orders = work_orders or f.work_orders()
        retriever_factory = f.retriever
        if faults := _fault_config(s):
            from app.faults import FaultyRetriever, FaultyWorkOrders

            log.warning("FAULT INJECTION ENABLED", extra={"event": "faults", "faults": faults})
            work_orders = FaultyWorkOrders(work_orders, faults, s.tool_timeout_seconds)
            base_retriever = retriever_factory

            def retriever_factory():
                return FaultyRetriever(base_retriever(), faults, s.tool_timeout_seconds)

        if chat is None:
            agent = build_agent(
                s,
                SubjectDirectoryIdentityProvider(directory),
                retriever_factory,
                lambda: work_orders,
                f.proposals if s.enable_work_order_creation else None,
            )
            chat = ChatService(s, agent)

    app = FastAPI(title="PropertyOps AI", docs_url=None, redoc_url=None, openapi_url=None)
    google_signin = s.auth_mode == "google_id_token"

    @app.middleware("http")
    async def headers(request: Request, call_next):
        import time

        from app.observability import new_request_context, request_id_var, trace_var

        rid_token, trace_token = new_request_context(
            request.headers.get("X-Cloud-Trace-Context"), s.project
        )
        started = time.monotonic()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            response.headers["X-Request-ID"] = request_id_var.get() or ""
            return _security_headers(response, google_signin)
        finally:
            if request.url.path.startswith("/api/"):
                log.info(
                    "http_request",
                    extra={
                        "event": "http_request",
                        "method": request.method,
                        "path": request.url.path,
                        "status": status,
                        "latency_ms": round((time.monotonic() - started) * 1000),
                    },
                )
            request_id_var.reset(rid_token)
            trace_var.reset(trace_token)

    def current_user(request: Request) -> AuthContext:
        try:
            principal = authenticator.authenticate(request.headers)
        except AuthenticationError as e:
            log.warning("auth_failed", extra={"event": "auth_failed", "reason": str(e)})
            raise HTTPException(401, "authentication required") from None
        try:
            ctx = directory.lookup(principal.subject)
        except Exception:
            log.exception("identity lookup failed")
            raise HTTPException(503, "identity service unavailable") from None
        if ctx is None:
            log.warning(
                "auth_unprovisioned", extra={"event": "authz_denied", "subject": principal.subject}
            )
            raise HTTPException(403, "account is not provisioned")
        return ctx

    User = Annotated[AuthContext, Depends(current_user)]

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/api/config")
    def config():
        out = {
            "auth_mode": s.auth_mode,
            "creation_enabled": s.enable_work_order_creation,
            "oauth_client_id": s.oauth_audience if google_signin else None,
        }
        if s.auth_mode == "demo":
            import json

            from app.demo_auth import DEMO_AUTH_PATH

            out["demo_identities"] = [
                {"id": f["context_id"], "role": f["role"]}
                for f in json.loads(DEMO_AUTH_PATH.read_text())
            ]
        return out

    @app.get("/api/me")
    def me(user: User):
        return {"subject": user.subject, "role": user.role}

    @app.post("/api/chat")
    async def chat_endpoint(body: ChatRequest, user: User):
        try:
            result = await chat.send(user.subject, body.session_id, body.message)
        except ChatError as e:
            raise HTTPException(400, str(e)) from None
        return result.to_dict()

    @app.post("/api/confirm")
    async def confirm(body: ConfirmRequest, user: User):
        try:
            result = await chat.respond(
                user.subject, body.session_id, body.confirmation_id, body.approve
            )
        except ChatError as e:
            raise HTTPException(409, str(e)) from None
        return result.to_dict()

    @app.get("/api/work-orders")
    def list_work_orders(user: User, status: str | None = None):
        try:
            return {"work_orders": list_visible_work_orders(user, work_orders, status)}
        except InvalidStatusFilter as e:
            raise HTTPException(400, str(e)) from None
        except AuthorizationError:
            raise HTTPException(403, "not permitted") from None
        except Exception:
            log.exception("work-order list failed")
            return JSONResponse(
                {"error": "unavailable", "message": "Work orders could not be loaded."}, 503
            )

    @app.get("/")
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
