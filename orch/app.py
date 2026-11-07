"""DryDock orchestrator API (plan Task 6). Contract for the UI: docs/api.md.

Run with ONE process (uvicorn orch.app:app, no --workers): run state, the one-sandbox lock and the rate-limit counters
live in this process. Bind to 127.0.0.1 behind Caddy: X-Forwarded-For counts only from a loopback peer (runs.client_ip).
Errors are always {"card", "message"} (web/src/api.js); no stack trace or upstream body ever reaches a response.
"""
import logging
import os
import re
import threading
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import Depends, FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, model_validator
from starlette.exceptions import HTTPException as StarletteHTTPException

from llm.client import api_key, base_url, load_env, validate_env

load_env()  # first: orch.apply and orch.runs read DRYDOCK_* at import. Real env vars win. Prints nothing.

from orch import apply  # noqa: E402
from orch import runs as R  # noqa: E402

log = logging.getLogger(__name__)
RECEIPT_ID = re.compile(r"[0-9a-f]{32}")
MAX_BODY = 16 * 1024  # 2,000 chars of text in the worst JSON escaping is 12 KB
# Must match what the page loads: web/src/motion.js (GSAP from cdnjs), web/index.html (Fontshare). No inline script in it.
CSP = ("default-src 'self'; script-src 'self' https://cdnjs.cloudflare.com; "
       "style-src 'self' 'unsafe-inline' https://api.fontshare.com; font-src 'self' https://cdn.fontshare.com; "
       "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


@asynccontextmanager
async def lifespan(_app):
    load_env()  # the inference key lives in this process's env only; sandbox containers never get it
    validate_env()  # a key with no base URL or model stops the process here, not on a visitor's first click
    R.load_usage()
    stop = threading.Event()
    threading.Thread(target=R.reaper, args=(stop,), daemon=True, name="reaper").start()  # pin: schedule reap_idle
    yield
    stop.set()


app = FastAPI(title="DryDock", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


class BodyLimit:
    """Refuse before a body sits in memory: FastAPI reads it all before validation and before the rate limit. Caddy has
    the same limit (deploy/Caddyfile); this one holds if the app is ever reached another way."""

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.inner(scope, receive, send)
        length = dict(scope["headers"]).get(b"content-length", b"0")
        if not length.isdigit() or int(length) > MAX_BODY:
            return await err(413, "invalid", "Request too large")(scope, receive, send)
        seen = 0

        async def counted():  # a chunked body has no length to read up front
            nonlocal seen
            message = await receive()
            seen += len(message.get("body", b""))
            if seen > MAX_BODY:
                raise StarletteHTTPException(413)
            return message

        return await self.inner(scope, counted, send)


app.add_middleware(BodyLimit)


class CreateRun(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ticket_id: str | None = None
    text: str | None = Field(None, max_length=R.MAX_TEXT)

    @model_validator(mode="after")
    def exactly_one(self):
        if (self.ticket_id is None) == (self.text is None):
            raise ValueError("send exactly one of ticket_id or text")
        if self.ticket_id is not None and self.ticket_id not in R.TICKETS:
            raise ValueError("unknown ticket_id")
        if self.text is not None and not self.text.strip():
            raise ValueError("text is empty")
        if self.text is not None and "\x00" in self.text:
            raise ValueError("text contains a NUL byte")
        return self


class Approve(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["ops", "engineer"]


class Close(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: Literal["close", "reject"] = "close"


def err(status: int, card: str, message: str, headers=None) -> JSONResponse:
    return JSONResponse({"card": card, "message": message}, status, headers=headers)


@app.exception_handler(R.ApiError)
async def api_error(_req, e: R.ApiError):
    return err(e.status, e.card, e.message, e.headers)


@app.exception_handler(RequestValidationError)
async def validation_error(_req, e: RequestValidationError):
    first = e.errors()[0] if e.errors() else {}
    where = ".".join(str(x) for x in first.get("loc", ()) if x != "body")
    msg = str(first.get("msg", "invalid request")).removeprefix("Value error, ")
    return err(422, "invalid", f"{where}: {msg}" if where else msg)


NOT_FOUND_PAGE = ('<!doctype html><meta charset="utf-8"><title>DryDock: not found</title>'
                  '<h1>No such page</h1><p><a href="/">Back to DryDock</a></p>')


@app.exception_handler(StarletteHTTPException)
async def http_error(req, e: StarletteHTTPException):
    if e.status_code == 404 and not req.url.path.startswith("/api"):  # a person typed a page: give them a way back
        return HTMLResponse(NOT_FOUND_PAGE, 404)
    message = {404: "Not found", 413: "Request too large"}.get(e.status_code, "Request not allowed")
    return err(e.status_code, "invalid", message)


@app.exception_handler(Exception)
async def internal_error(_req, e: Exception):
    log.exception("unhandled error")  # detail in the log only
    return err(500, "error", "Something went wrong, try again")


@app.middleware("http")
async def headers(request: Request, call_next):
    resp = await call_next(request)
    hashed = request.url.path.startswith("/assets/")  # Vite puts a content hash in these names
    resp.headers.update({"X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
                         "Cache-Control": "public, max-age=31536000, immutable" if hashed else "no-store",
                         "Content-Security-Policy": CSP})
    return resp


def client_ip(request: Request) -> str:
    return R.client_ip(request.headers.get("x-forwarded-for"), request.client and request.client.host)


def limited(request: Request) -> None:
    R.rate_limit(client_ip(request))


def read_limited(request: Request) -> None:
    R.read_limit(client_ip(request))


def ok(obj, status: int = 200) -> JSONResponse:
    return JSONResponse(jsonable_encoder(obj), status)


@app.get("/api/health")
def health():
    """ok: this process answers (pages and samples serve). docker: runs can start. 200 either way, so Caddy keeps
    serving the site while the sandbox host is down (deploy/docker-compose.yml waits on this route).
    model: what serves live runs, for the landing header; None when no key is set, so the page never names a model
    that cannot run."""
    from llm.agent import _inference_label
    model = os.environ.get("DRYDOCK_MODEL")
    return {"ok": True, "docker": R.docker_up(),
            "model": _inference_label(base_url(), model) if api_key() and model else None}


@app.get("/api/tickets", dependencies=[Depends(read_limited)])
def tickets():
    return ok(R.inbox())


@app.post("/api/runs")
def create_run(body: CreateRun, request: Request):
    limited(request)  # after body validation, so a 422 does not use up the visitor's runs (bugs.md F4)
    return ok({"run_id": R.create_run(body.ticket_id, body.text, client_ip(request))}, 201)


@app.get("/api/runs/{run_id}", dependencies=[Depends(read_limited)])
def get_run(run_id: str):
    return ok(R.view(run_id))


@app.post("/api/runs/{run_id}/approve", dependencies=[Depends(read_limited)])
def approve(run_id: str, body: Approve):
    return ok(R.approve(run_id, body.role))


@app.post("/api/runs/{run_id}/close", dependencies=[Depends(read_limited)])
def close(run_id: str, body: Close = Close()):
    return ok(R.end(run_id, body.reason))


@app.post("/api/runs/{run_id}/undo", dependencies=[Depends(read_limited)])
def undo(run_id: str):
    return ok(R.undo(run_id))


@app.get("/api/runs/{run_id}/receipt", dependencies=[Depends(read_limited)])
def receipt(run_id: str):
    if not RECEIPT_ID.fullmatch(run_id):  # only real run ids (uuid4 hex): no other file in the receipts folder is served
        raise R.ApiError(404, "invalid", "No receipt for this run")
    try:
        body = apply.receipt(run_id)  # validates run_id (RUN_ID_RE) before building the path
    except (ValueError, FileNotFoundError):
        raise R.ApiError(404, "invalid", "No receipt for this run") from None
    return JSONResponse(body, headers={"Content-Disposition": f'attachment; filename="receipt-{run_id}.json"'})


# Last, so /api/* wins. ?app and ?fixture are query strings on /, read by web/src/main.jsx. A missing file under /api is
# a 404 through http_error, so it is JSON and never index.html.
ui = StaticFiles(directory=R.ROOT / "web" / "dist", html=True, check_dir=False)
ui.config_checked = True  # no web/dist (an API-only laptop run) is a 404, not a crash on the first request
app.mount("/", ui, name="ui")
