"""CloudTec Center: one console for Active Directory and vSphere."""

import logging
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .auth import COOKIE, AdminPassword, PasswordError, Sessions
from .config import Settings
from .connectors import ConnectorError, open_connector
from .shell import PowerShellSession, find_powershell
from .store import Connection, Store, load_or_create_key

log = logging.getLogger("cloudtec")
STATIC = Path(__file__).parent / "static"

# Settings that are safe to store and return, per connection kind.
CONFIG_FIELDS = {
    "ad": ("host", "port", "use_ssl", "verify_ssl", "base_dn", "bind_user"),
    "vsphere": ("host", "port", "verify_ssl", "username"),
}


class LoginBody(BaseModel):
    password: str


class ChangePasswordBody(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=256)


class ShellBody(BaseModel):
    command: str = Field(min_length=1, max_length=100_000)


class ConnectionBody(BaseModel):
    kind: Literal["ad", "vsphere"]
    name: str = Field(min_length=1, max_length=80)
    config: dict = {}
    password: str = ""
    demo: bool = False


class DnBody(BaseModel):
    dn: str = Field(min_length=3)


class PasswordBody(DnBody):
    new_password: str = Field(min_length=8)


class PowerBody(BaseModel):
    action: Literal["start", "stop", "reset", "suspend", "shutdown", "reboot"]


def _clean_config(kind: str, config: dict) -> dict:
    return {k: config[k] for k in CONFIG_FIELDS[kind] if k in config and config[k] not in ("", None)}


def _short_name(name: str | None) -> str:
    return (name or "").split(".")[0].strip().lower()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    store = Store(settings.data_dir, load_or_create_key(settings.data_dir, settings.secret_key))
    admin = AdminPassword(settings.data_dir, settings.admin_password)
    sessions = Sessions(settings.session_hours * 3600)
    exe = find_powershell(settings.powershell)
    shell = PowerShellSession(exe, Path.home(), settings.data_dir / "shell_history.log") if exe else None

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        if shell:
            shell.close()

    app = FastAPI(title="CloudTec Center", docs_url="/api/docs", openapi_url="/api/openapi.json",
                  lifespan=lifespan)

    @app.exception_handler(ConnectorError)
    def connector_error(_: Request, exc: ConnectorError):
        return JSONResponse(status_code=502, content={"detail": str(exc)})

    def require_session(request: Request) -> None:
        if not sessions.valid(request.cookies.get(COOKIE)):
            raise HTTPException(401, "Not signed in")

    def get_conn(conn_id: str, kind: str) -> Connection:
        conn = store.get(conn_id)
        if not conn or conn.kind != kind:
            raise HTTPException(404, "Connection not found")
        return conn

    def gather(conns: list[Connection], fn) -> list[tuple[Connection, object, str | None]]:
        """Run fn(connector) against each connection in parallel; failures don't sink the rest."""
        def one(conn):
            try:
                with open_connector(conn) as c:
                    return conn, fn(c), None
            except ConnectorError as e:
                return conn, None, str(e)
            except Exception as e:  # noqa: BLE001 - surface unexpected backend errors per connection
                log.exception("Connection %s failed", conn.name)
                return conn, None, f"Unexpected error: {e}"

        if not conns:
            return []
        with ThreadPoolExecutor(max_workers=min(8, len(conns))) as pool:
            return list(pool.map(one, conns))

    # --- session -----------------------------------------------------------

    @app.post("/api/login")
    def login(body: LoginBody, response: Response):
        if not admin.check(body.password):
            raise HTTPException(401, "Wrong password")
        response.set_cookie(COOKIE, sessions.create(), httponly=True, samesite="strict",
                            secure=settings.secure_cookies, max_age=settings.session_hours * 3600)
        return {"ok": True}

    @app.post("/api/logout")
    def logout(request: Request, response: Response):
        sessions.revoke(request.cookies.get(COOKIE))
        response.delete_cookie(COOKIE)
        return {"ok": True}

    @app.get("/api/me")
    def me(request: Request):
        return {"signed_in": sessions.valid(request.cookies.get(COOKIE))}

    api = [Depends(require_session)]

    # --- settings ----------------------------------------------------------

    @app.get("/api/settings", dependencies=api)
    def get_settings():
        return {"password_from_env": admin.from_env}

    @app.post("/api/settings/password", dependencies=api)
    def change_password(body: ChangePasswordBody, request: Request):
        try:
            admin.change(body.current_password, body.new_password)
        except PasswordError as e:
            raise HTTPException(409 if admin.from_env else 400, str(e)) from e
        sessions.revoke_others(request.cookies.get(COOKIE))
        return {"ok": True}

    # --- connections -------------------------------------------------------

    @app.get("/api/connections", dependencies=api)
    def list_connections():
        return [c.public() for c in store.list()]

    @app.post("/api/connections", dependencies=api, status_code=201)
    def add_connection(body: ConnectionBody):
        if not body.demo and not body.password:
            raise HTTPException(422, "A password is required")
        conn = store.add(body.kind, body.name.strip(), _clean_config(body.kind, body.config),
                         body.password, body.demo)
        return conn.public()

    @app.post("/api/connections/test", dependencies=api)
    def test_unsaved(body: ConnectionBody):
        draft = Connection(id="draft", kind=body.kind, name=body.name,
                           config=_clean_config(body.kind, body.config),
                           secret=body.password, demo=body.demo, created_at="")
        with open_connector(draft) as c:
            return c.test()

    @app.post("/api/connections/{conn_id}/test", dependencies=api)
    def test_saved(conn_id: str):
        conn = store.get(conn_id)
        if not conn:
            raise HTTPException(404, "Connection not found")
        with open_connector(conn) as c:
            return c.test()

    @app.delete("/api/connections/{conn_id}", dependencies=api)
    def delete_connection(conn_id: str):
        if not store.delete(conn_id):
            raise HTTPException(404, "Connection not found")
        return {"ok": True}

    # --- overview ----------------------------------------------------------

    @app.get("/api/dashboard", dependencies=api)
    def dashboard():
        return [
            {**conn.public(), "summary": result, "error": error}
            for conn, result, error in gather(store.list(), lambda c: c.summary())
        ]

    @app.get("/api/search", dependencies=api)
    def search(q: str):
        q = q.strip()
        if len(q) < 2:
            raise HTTPException(422, "Search for at least 2 characters")

        def ad(c):
            return {"users": c.users(q, 50), "groups": c.groups(q, 50), "computers": c.computers(q, 50)}

        out = {"users": [], "groups": [], "computers": [], "vms": [], "errors": []}
        for conn, result, error in gather(store.list("ad"), ad) + gather(store.list("vsphere"), lambda c: {"vms": c.vms(q)}):
            if error:
                out["errors"].append({"connection": conn.name, "error": error})
                continue
            for key, items in result.items():
                out[key] += [{**i, "connection_id": conn.id, "connection": conn.name} for i in items]
        return out

    @app.get("/api/crosscheck", dependencies=api)
    def crosscheck():
        """Line up AD computer accounts with vSphere VMs by short hostname."""
        machines: dict[str, dict] = {}
        errors = []

        def entry(key):
            return machines.setdefault(key, {"name": key, "ad": [], "vms": []})

        for conn, computers, error in gather(store.list("ad"), lambda c: c.computers()):
            if error:
                errors.append({"connection": conn.name, "error": error})
                continue
            for comp in computers:
                entry(_short_name(comp["name"]))["ad"].append({**comp, "connection": conn.name})

        for conn, vms, error in gather(store.list("vsphere"), lambda c: c.vms()):
            if error:
                errors.append({"connection": conn.name, "error": error})
                continue
            for vm in vms:
                entry(_short_name(vm["name"]))["vms"].append(
                    {**vm, "connection": conn.name, "connection_id": conn.id})

        rows = []
        for m in machines.values():
            m["status"] = "matched" if m["ad"] and m["vms"] else ("vm_only" if m["vms"] else "ad_only")
            m["name"] = (m["vms"] or m["ad"])[0]["name"]
            rows.append(m)
        rows.sort(key=lambda m: ({"vm_only": 0, "ad_only": 1, "matched": 2}[m["status"]], m["name"].lower()))
        return {"machines": rows, "errors": errors}

    # --- PowerShell --------------------------------------------------------

    def shell_status(request: Request) -> dict:
        """Whether the PowerShell page may be used from this browser, and if not, why."""
        local = request.client is not None and request.client.host in ("127.0.0.1", "::1", "localhost")
        if settings.shell == "off":
            reason = "The PowerShell page is turned off (CLOUDTEC_SHELL=off)."
        elif settings.shell != "on" and not local:
            reason = ("For safety, PowerShell only works when CloudTec is opened on the computer it runs on "
                      "(http://localhost:8000). Set CLOUDTEC_SHELL=on to allow it from other computers.")
        elif not shell:
            reason = "PowerShell wasn't found on this computer."
        else:
            reason = None
        return {"available": reason is None, "reason": reason,
                "cwd": shell.cwd if shell else None, "executable": exe}

    def require_shell(request: Request) -> PowerShellSession:
        status = shell_status(request)
        if not status["available"]:
            raise HTTPException(403, status["reason"])
        return shell

    @app.get("/api/shell", dependencies=api)
    def get_shell(request: Request):
        return shell_status(request)

    @app.post("/api/shell/run", dependencies=api)
    def shell_run(body: ShellBody, request: Request):
        return require_shell(request).run(body.command)

    @app.post("/api/shell/reset", dependencies=api)
    def shell_reset(request: Request):
        session = require_shell(request)
        session.reset()
        return {"ok": True, "cwd": session.cwd}

    # --- Active Directory --------------------------------------------------

    @app.get("/api/ad/{conn_id}/users", dependencies=api)
    def ad_users(conn_id: str, q: str = ""):
        with open_connector(get_conn(conn_id, "ad")) as c:
            return c.users(q.strip())

    @app.get("/api/ad/{conn_id}/groups", dependencies=api)
    def ad_groups(conn_id: str, q: str = ""):
        with open_connector(get_conn(conn_id, "ad")) as c:
            return c.groups(q.strip())

    @app.get("/api/ad/{conn_id}/computers", dependencies=api)
    def ad_computers(conn_id: str, q: str = ""):
        with open_connector(get_conn(conn_id, "ad")) as c:
            return c.computers(q.strip())

    @app.post("/api/ad/{conn_id}/users/enable", dependencies=api)
    def ad_enable(conn_id: str, body: DnBody):
        with open_connector(get_conn(conn_id, "ad")) as c:
            c.set_enabled(body.dn, True)
        return {"ok": True}

    @app.post("/api/ad/{conn_id}/users/disable", dependencies=api)
    def ad_disable(conn_id: str, body: DnBody):
        with open_connector(get_conn(conn_id, "ad")) as c:
            c.set_enabled(body.dn, False)
        return {"ok": True}

    @app.post("/api/ad/{conn_id}/users/unlock", dependencies=api)
    def ad_unlock(conn_id: str, body: DnBody):
        with open_connector(get_conn(conn_id, "ad")) as c:
            c.unlock(body.dn)
        return {"ok": True}

    @app.post("/api/ad/{conn_id}/users/reset-password", dependencies=api)
    def ad_reset_password(conn_id: str, body: PasswordBody):
        with open_connector(get_conn(conn_id, "ad")) as c:
            c.reset_password(body.dn, body.new_password)
        return {"ok": True}

    # --- vSphere -----------------------------------------------------------

    @app.get("/api/vsphere/{conn_id}/vms", dependencies=api)
    def vs_vms(conn_id: str, q: str = ""):
        with open_connector(get_conn(conn_id, "vsphere")) as c:
            return c.vms(q.strip())

    @app.get("/api/vsphere/{conn_id}/vms/{vm_id}", dependencies=api)
    def vs_vm(conn_id: str, vm_id: str):
        with open_connector(get_conn(conn_id, "vsphere")) as c:
            return c.vm(vm_id)

    @app.post("/api/vsphere/{conn_id}/vms/{vm_id}/power", dependencies=api)
    def vs_power(conn_id: str, vm_id: str, body: PowerBody):
        with open_connector(get_conn(conn_id, "vsphere")) as c:
            c.power(vm_id, body.action)
        return {"ok": True}

    @app.get("/api/vsphere/{conn_id}/hosts", dependencies=api)
    def vs_hosts(conn_id: str):
        with open_connector(get_conn(conn_id, "vsphere")) as c:
            return c.hosts()

    @app.get("/api/vsphere/{conn_id}/datastores", dependencies=api)
    def vs_datastores(conn_id: str):
        with open_connector(get_conn(conn_id, "vsphere")) as c:
            return c.datastores()

    @app.get("/api/vsphere/{conn_id}/clusters", dependencies=api)
    def vs_clusters(conn_id: str):
        with open_connector(get_conn(conn_id, "vsphere")) as c:
            return c.clusters()

    # --- UI ----------------------------------------------------------------

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html")

    return app
