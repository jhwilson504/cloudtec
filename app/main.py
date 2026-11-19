"""KasTec Center: one console for Active Directory, vSphere, iDRAC, SolarWinds and VNX storage."""

import json
import logging
import re
import smtplib
import secrets
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import certgen, certwatch, dcrestart
from . import shell as powershell
from .auth import COOKIE, AdminPassword, LoginThrottle, PasswordError, Sessions, hash_password, verify_hash
from .config import Settings
from .connectors import ConnectorError, open_connector
from .connectors.ad import ADConnector
from .connectors.idrac import parse_hosts
from .connectors.vnx import link_datastores
from . import activity, metrics, netmap, provision, remote
from .shell import PowerShellSession, connect_script, find_powershell
from .store import Connection, Store, load_or_create_key
from .tls import cert_summary, read_network

log = logging.getLogger("cloudtec")
STATIC = Path(__file__).parent / "static"

# Settings that are safe to store and return, per connection kind.
CONFIG_FIELDS = {
    "ad": ("host", "port", "use_ssl", "verify_ssl", "base_dn", "bind_user"),
    "vsphere": ("host", "port", "verify_ssl", "username"),
    "idrac": ("hosts", "verify_ssl", "username"),
    "solarwinds": ("host", "port", "verify_ssl", "username"),
    "vnx": ("host", "port", "username", "sp_a", "sp_b", "host_key"),
}


ADMIN = "admin"  # the built-in administrator account
USERNAME = re.compile(r"^[A-Za-z0-9._-]{2,40}$")


class LoginBody(BaseModel):
    username: str = ADMIN
    password: str


class NewUserBody(BaseModel):
    username: str
    display_name: str = Field(default="", max_length=80)
    role: Literal["admin", "operator"] = "operator"
    password: str = Field(min_length=8, max_length=256)


class UpdateUserBody(BaseModel):
    display_name: str | None = Field(default=None, max_length=80)
    role: Literal["admin", "operator"] | None = None
    disabled: bool | None = None
    password: str | None = Field(default=None, min_length=8, max_length=256)


class ChangePasswordBody(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=256)


class ShellBody(BaseModel):
    command: str = Field(min_length=1, max_length=100_000)


class RenameBody(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class ShellConnectBody(BaseModel):
    connection_id: str
    dc: str = ""   # Active Directory only: which of the domain's DCs to use (default: the configured one)


class ConnectionBody(BaseModel):
    kind: Literal["ad", "vsphere", "idrac", "solarwinds", "vnx"]
    name: str = Field(min_length=1, max_length=80)
    config: dict = {}
    password: str = ""
    demo: bool = False


class CertWatchBody(BaseModel):
    targets: list[dict] = Field(default=[], max_length=500)
    email_to: list[str] = Field(default=[], max_length=50)


class CertRequestBody(BaseModel):
    names: list[str] = Field(min_length=1, max_length=100)
    details: dict[str, str] = {}
    key_size: int = 2048


class SignedCertBody(BaseModel):
    files: list[str] = Field(min_length=1, max_length=10)  # base64 file contents
    pfx_password: str = Field(max_length=200)


class DnBody(BaseModel):
    dn: str = Field(min_length=3)


class PasswordBody(DnBody):
    new_password: str = Field(min_length=8, max_length=256)
    unlock: bool = False
    must_change: bool = False


class RestartDCBody(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    confirm: str
    # Required to restart when a pre-check fails (e.g. no other DC in the site is answering); recorded.
    override_reason: str = Field(default="", max_length=500)


class DatastoreBody(BaseModel):
    connection_id: str
    cluster: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=80)
    vmfs: Literal["auto", "5", "6"] = "auto"


class ProvisionBody(BaseModel):
    name: str = Field(min_length=1, max_length=63)
    pool: str = Field(min_length=1, max_length=200)
    size_gb: int = Field(ge=1, le=65536)
    thin: bool = True
    sp: Literal["auto", "a", "b"] = "auto"
    storage_groups: list[str] = Field(default_factory=list, max_length=32)
    hlu: int | None = Field(default=None, ge=0, le=255)
    datastore: DatastoreBody | None = None
    confirm: str = ""


class VNXCommandBody(BaseModel):
    command: str = Field(min_length=1, max_length=20_000)


class AgentReportBody(BaseModel):
    user: str = Field(min_length=1, max_length=200)
    computer: str = Field(min_length=1, max_length=63)
    agent: str = Field(default="", max_length=20)
    samples: list[dict] = Field(default_factory=list, max_length=5000)


class SSHOpenBody(BaseModel):
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(default=22, ge=1, le=65535)
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)
    trust_fingerprint: str = ""


class SSHWriteBody(BaseModel):
    data: str = Field(max_length=65536)


class GuestRunBody(BaseModel):
    command: str = Field(min_length=1, max_length=20000)
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=1024)
    script_type: Literal["Bash", "PowerShell", "Bat"] = "Bash"


class DCNameBody(BaseModel):
    name: str = Field(min_length=1, max_length=64)


class RestartCompleteBody(DCNameBody):
    outcome: Literal["healthy", "issues", "not back"]
    notes: str = Field(default="", max_length=1000)


class PowerBody(BaseModel):
    action: Literal["start", "stop", "reset", "suspend", "shutdown", "reboot"]


def _clean_config(kind: str, config: dict) -> dict:
    out = {k: config[k] for k in CONFIG_FIELDS[kind] if k in config and config[k] not in ("", None)}
    if kind == "idrac":
        try:
            out["hosts"] = parse_hosts(out.get("hosts"))
        except ConnectorError as e:
            raise HTTPException(422, str(e)) from e
    return out


# Ports checked on each domain controller, from the computer KasTec runs on.
DC_PORTS = {389: "LDAP", 636: "LDAPS", 3268: "Global Catalog", 9389: "AD Web Services"}


def port_open(host: str, port: int, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def this_computer_addresses() -> set[str]:
    """Addresses a browser on this same computer may connect from."""
    addresses = {"127.0.0.1", "::1", "localhost"}
    try:
        addresses.update(socket.gethostbyname_ex(socket.gethostname())[2])
    except OSError:
        pass
    return addresses


def _short_name(name: str | None) -> str:
    return (name or "").split(".")[0].strip().lower()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    store = Store(settings.data_dir, load_or_create_key(settings.data_dir, settings.secret_key))
    admin = AdminPassword(settings.data_dir, settings.admin_password)
    sessions = Sessions(settings.session_hours * 3600)
    throttle = LoginThrottle()
    audit_path = settings.data_dir / "audit.log"
    activity_store = activity.ActivityStore(settings.data_dir)
    metrics_store = metrics.MetricsStore(settings.data_dir)
    last_totals: dict = {}
    audit_lock = threading.Lock()
    local_addresses = this_computer_addresses()
    exe = find_powershell(settings.powershell)
    netmaps = netmap.NetMaps(exe)
    ssh = remote.SSHSessions(settings.data_dir)
    shell = PowerShellSession(exe, Path.home(), settings.data_dir / "shell_history.log") if exe else None
    watcher = certwatch.Watcher(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        watcher.start()
        yield
        watcher.stop()
        ssh.close_all()
        netmaps.close()
        if shell:
            shell.close()

    app = FastAPI(title="KasTec Center", docs_url="/api/docs", openapi_url="/api/openapi.json",
                  lifespan=lifespan)

    @app.exception_handler(ConnectorError)
    def connector_error(request: Request, exc: ConnectorError):
        # Also show the reason in the KasTec window, not just on the page.
        log.warning("Problem with %s %s: %s", request.method, request.url.path, exc)
        return JSONResponse(status_code=502, content={"detail": str(exc)})

    def lookup(username: str | None) -> dict | None:
        """The signed-in user's details, or None if the account is gone or disabled."""
        if not username:
            return None
        if username.lower() == ADMIN:
            return {"username": ADMIN, "display_name": "Administrator", "role": "admin", "builtin": True}
        user = store.get_user(username)
        if not user or user.disabled:
            return None
        return {"username": user.username, "display_name": user.display_name or user.username,
                "role": user.role, "builtin": False}

    def require_session(request: Request) -> dict:
        token = request.cookies.get(COOKIE)
        user = lookup(sessions.user(token))
        if not user:
            sessions.revoke(token)
            raise HTTPException(401, "Not signed in")
        request.state.user = user
        return user

    def require_admin(request: Request) -> dict:
        user = require_session(request)
        if user["role"] != "admin":
            raise HTTPException(403, "Only administrators can do this.")
        return user

    def audit(request: Request, action: str, target: str = "", connection: Connection | None = None) -> None:
        """Append who did what to <data dir>/audit.log (one JSON object per line)."""
        user = getattr(request.state, "user", None)
        entry = {
            "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "user": user["username"] if user else None,
            "action": action,
            "target": target,
            "connection": connection.name if connection else None,
            "address": request.client.host if request.client else None,
        }
        with audit_lock, audit_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

    def get_conn(conn_id: str, kind: str) -> Connection:
        conn = store.get(conn_id)
        if not conn or conn.kind != kind:
            raise HTTPException(404, "Connection not found")
        return conn

    def remember(conn: Connection, connector) -> None:
        """Save what a connector learned about its server (e.g. the VNX Control Station's SSH key)."""
        learned = getattr(connector, "learned_config", None) or getattr(getattr(connector, "inner", None), "learned_config", None)
        if learned and not conn.demo and any(conn.config.get(k) != v for k, v in learned.items()):
            fresh = store.get(conn.id)
            if fresh:
                store.update_config(conn.id, {**fresh.config, **learned})

    def gather(conns: list[Connection], fn) -> list[tuple[Connection, object, str | None]]:
        """Run fn(connector) against each connection in parallel; failures don't sink the rest."""
        def one(conn):
            try:
                with open_connector(conn) as c:
                    result = fn(c)
                    remember(conn, c)
                    return conn, result, None
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

    def password_ok(username: str, password: str) -> bool:
        if username.lower() == ADMIN:
            return admin.check(password)
        user = store.get_user(username)
        if not user or user.disabled:
            verify_hash(password, hash_password("timing"))  # same work either way, so timing reveals nothing
            return False
        return verify_hash(password, user.password_hash)

    @app.post("/api/login")
    def login(body: LoginBody, request: Request, response: Response):
        username = body.username.strip() or ADMIN
        address = request.client.host if request.client else ""
        wait = throttle.blocked_for(username, address)
        if wait:
            raise HTTPException(429, f"Too many wrong passwords. Try again in {max(1, round(wait / 60))} minute(s).")
        if not password_ok(username, body.password):
            throttle.failed(username, address)
            request.state.user = {"username": username}
            audit(request, "sign-in failed")
            raise HTTPException(401, "Wrong username or password")
        throttle.succeeded(username, address)
        user = lookup(username)
        request.state.user = user
        audit(request, "signed in")
        response.set_cookie(COOKIE, sessions.create(user["username"]), httponly=True, samesite="strict",
                            secure=settings.secure_cookies, max_age=settings.session_hours * 3600)
        return {"ok": True, **user}

    @app.post("/api/logout")
    def logout(request: Request, response: Response):
        sessions.revoke(request.cookies.get(COOKIE))
        response.delete_cookie(COOKIE)
        return {"ok": True}

    @app.get("/api/me")
    def me(request: Request):
        user = lookup(sessions.user(request.cookies.get(COOKIE)))
        return {"signed_in": True, **user} if user else {"signed_in": False}

    api = [Depends(require_session)]
    admin_only = [Depends(require_admin)]

    # --- settings ----------------------------------------------------------

    @app.get("/api/settings")
    def get_settings(user: dict = Depends(require_session)):
        out = {"password_from_env": user["builtin"] and admin.from_env}
        if user["role"] == "admin":
            net = read_network(settings.data_dir)
            out["network"] = {"enabled": bool(net.get("enabled")), "port": net.get("port"),
                              "certificate": cert_summary(settings.data_dir)}
        return out

    @app.post("/api/settings/password")
    def change_password(body: ChangePasswordBody, request: Request, user: dict = Depends(require_session)):
        if user["builtin"]:
            try:
                admin.change(body.current_password, body.new_password)
            except PasswordError as e:
                raise HTTPException(409 if admin.from_env else 400, str(e)) from e
        else:
            if not password_ok(user["username"], body.current_password):
                raise HTTPException(400, "Current password is incorrect")
            store.update_user(user["username"], password_hash=hash_password(body.new_password))
        sessions.revoke_user(user["username"], keep=request.cookies.get(COOKIE))
        audit(request, "changed own password")
        return {"ok": True}

    # --- console users -----------------------------------------------------

    def clean_username(name: str) -> str:
        name = name.strip()
        if not USERNAME.match(name):
            raise HTTPException(422, "Usernames are 2-40 letters, numbers, dots, dashes or underscores.")
        if name.lower() == ADMIN:
            raise HTTPException(422, "'admin' is the built-in administrator account.")
        return name

    @app.get("/api/users", dependencies=admin_only)
    def list_users():
        builtin = {"username": ADMIN, "display_name": "Administrator", "role": "admin",
                   "disabled": False, "created_at": None, "builtin": True}
        return [builtin] + [u.public() for u in store.list_users()]

    @app.post("/api/users", dependencies=admin_only, status_code=201)
    def add_user(body: NewUserBody, request: Request):
        username = clean_username(body.username)
        if store.get_user(username):
            raise HTTPException(409, f"There's already a user called {username}.")
        user = store.add_user(username, body.display_name.strip(), body.role, hash_password(body.password))
        audit(request, f"added user ({body.role})", username)
        return user.public()

    @app.patch("/api/users/{username}", dependencies=admin_only)
    def update_user(username: str, body: UpdateUserBody, request: Request):
        if username.lower() == ADMIN:
            raise HTTPException(422, "The built-in admin account can't be changed here. Use Settings to change its password.")
        if not store.get_user(username):
            raise HTTPException(404, "User not found")
        fields = {}
        if body.display_name is not None:
            fields["display_name"] = body.display_name.strip()
        if body.role is not None:
            fields["role"] = body.role
        if body.disabled is not None:
            fields["disabled"] = body.disabled
        if body.password is not None:
            fields["password_hash"] = hash_password(body.password)
        user = store.update_user(username, **fields)
        if body.disabled or body.password is not None:
            sessions.revoke_user(username)  # sign them out everywhere
        changes = ", ".join(("new password" if k == "password_hash" else f"{k}={v}")
                            for k, v in fields.items())
        audit(request, f"updated user ({changes})", username)
        return user.public()

    @app.delete("/api/users/{username}", dependencies=admin_only)
    def delete_user(username: str, request: Request):
        if username.lower() == ADMIN:
            raise HTTPException(422, "The built-in admin account can't be deleted.")
        if not store.delete_user(username):
            raise HTTPException(404, "User not found")
        sessions.revoke_user(username)
        audit(request, "deleted user", username)
        return {"ok": True}

    @app.get("/api/audit", dependencies=admin_only)
    def audit_log(limit: int = 200):
        if not audit_path.exists():
            return []
        lines = audit_path.read_text(encoding="utf-8").splitlines()[-max(1, min(limit, 2000)):]
        out = []
        for line in reversed(lines):
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    # --- connections -------------------------------------------------------

    @app.get("/api/connections", dependencies=api)
    def list_connections():
        return [c.public() for c in store.list()]

    @app.post("/api/connections", dependencies=admin_only, status_code=201)
    def add_connection(body: ConnectionBody, request: Request):
        if not body.demo and not body.password:
            raise HTTPException(422, "A password is required")
        conn = store.add(body.kind, body.name.strip(), _clean_config(body.kind, body.config),
                         body.password, body.demo)
        audit(request, "added connection", conn.name, conn)
        return conn.public()

    @app.post("/api/connections/test", dependencies=admin_only)
    def test_unsaved(body: ConnectionBody):
        draft = Connection(id="draft", kind=body.kind, name=body.name,
                           config=_clean_config(body.kind, body.config),
                           secret=body.password, demo=body.demo, created_at="")
        with open_connector(draft) as c:
            return c.test()

    @app.post("/api/connections/{conn_id}/test", dependencies=admin_only)
    def test_saved(conn_id: str):
        conn = store.get(conn_id)
        if not conn:
            raise HTTPException(404, "Connection not found")
        with open_connector(conn) as c:
            result = c.test()
            remember(conn, c)
            return result

    @app.patch("/api/connections/{conn_id}", dependencies=admin_only)
    def rename_connection(conn_id: str, body: RenameBody, request: Request):
        conn = store.get(conn_id)
        name = body.name.strip()
        if not conn or not name:
            raise HTTPException(404 if not conn else 422, "Connection not found" if not conn else "Enter a name")
        store.rename(conn_id, name)
        audit(request, "renamed connection", f"{conn.name} -> {name}", conn)
        return store.get(conn_id).public()

    @app.delete("/api/connections/{conn_id}", dependencies=admin_only)
    def delete_connection(conn_id: str, request: Request):
        conn = store.get(conn_id)
        if not conn or not store.delete(conn_id):
            raise HTTPException(404, "Connection not found")
        audit(request, "removed connection", conn.name, conn)
        return {"ok": True}

    # --- certificate expiry ------------------------------------------------------

    def certificates_out(request: Request, results: list[dict]) -> dict:
        out = {"certificates": results, "alerts": certwatch.alerts(results), "checked": watcher.checked,
               "alert_days": list(certwatch.ALERT_DAYS)}
        if request.state.user["role"] == "admin":
            config = certwatch.read_config(settings.data_dir)
            out.update(config=config, email=certwatch.email_enabled(settings), email_error=watcher.last_error)
        return out

    @app.get("/api/certificates", dependencies=api)
    def certificates(request: Request):
        return certificates_out(request, watcher.latest())

    @app.post("/api/certificates/check", dependencies=admin_only)
    def certificates_check(request: Request):
        return certificates_out(request, watcher.check())

    @app.put("/api/certificates/config", dependencies=admin_only)
    def certificates_config(body: CertWatchBody, request: Request):
        try:
            config = certwatch.save_config(settings.data_dir, body.targets, body.email_to)
        except certwatch.WatchError as e:
            raise HTTPException(400, str(e))
        audit(request, "certificates.config", ", ".join(t["target"] for t in config["targets"]))
        return certificates_out(request, watcher.check())

    @app.post("/api/certificates/test-email", dependencies=admin_only)
    def certificates_test_email(request: Request):
        config = certwatch.read_config(settings.data_dir)
        to = list(dict.fromkeys(config["email_to"] + [o for t in config["targets"] for o in t["owners"]]))
        if not to:
            raise HTTPException(400, "Add at least one email address to send alerts to.")
        try:
            certwatch.send_email(settings, to, "KasTec test: certificate alerts",
                                 "Certificate expiry alerts from KasTec will arrive at this address.\n")
        except (OSError, smtplib.SMTPException, certwatch.WatchError) as e:
            raise HTTPException(502, f"Couldn't send the email: {e}")
        audit(request, "certificates.test-email", ", ".join(to))
        return {"sent_to": to}

    # --- certificate requests (CSR + key, then a PFX once the CA signs it) ---------

    def cert_request(fn, *args):
        try:
            return fn(settings.data_dir, *args)
        except certgen.CertGenError as e:
            raise HTTPException(400, str(e))

    @app.get("/api/cert-requests", dependencies=admin_only)
    def cert_requests():
        return certgen.list_requests(settings.data_dir)

    @app.post("/api/cert-requests", dependencies=admin_only, status_code=201)
    def cert_request_create(body: CertRequestBody, request: Request):
        out = cert_request(certgen.create_request, body.names, body.details, body.key_size)
        audit(request, "cert-request.create", ", ".join(out["names"]))
        return out

    @app.post("/api/cert-requests/{req_id}/certificate", dependencies=admin_only)
    def cert_request_signed(req_id: str, body: SignedCertBody, request: Request):
        files = [cert_request(lambda _, f: certgen.decode_upload(f), f) for f in body.files]
        out = cert_request(certgen.add_certificate, req_id, files, body.pfx_password)
        audit(request, "cert-request.certificate", out["common_name"])
        return out

    @app.get("/api/cert-requests/{req_id}/{kind}", dependencies=admin_only)
    def cert_request_file(req_id: str, kind: Literal["csr", "key", "crt", "pfx"], request: Request):
        data, name, media = cert_request(certgen.download, req_id, kind)
        if kind in ("key", "pfx"):
            audit(request, f"cert-request.download-{kind}", req_id)
        return Response(data, media_type=media, headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.delete("/api/cert-requests/{req_id}", dependencies=admin_only)
    def cert_request_delete(req_id: str, request: Request):
        cert_request(certgen.delete_request, req_id)
        audit(request, "cert-request.delete", req_id)
        return {"ok": True}

    # --- overview ----------------------------------------------------------

    def learn_dcs(conn: Connection, names: list[str] | None) -> None:
        """Remember an AD connection's DCs, so KasTec can switch to another if its usual one is down."""
        names = sorted({n for n in names or [] if n}, key=str.lower)
        if conn.kind == "ad" and not conn.demo and names and names != conn.config.get("known_dcs"):
            store.update_config(conn.id, {**conn.config, "known_dcs": names})

    @app.get("/api/dashboard", dependencies=api)
    def dashboard():
        def summary(c):
            learned = None
            if isinstance(c, ADConnector):
                try:
                    learned = c.dc_hostnames()
                except ConnectorError:
                    pass  # learning DCs is a bonus; never let it spoil the summary
            return c.summary(), learned

        out = []
        for conn, result, error in gather(store.list(), summary):
            if result:
                learn_dcs(conn, result[1])
            out.append({**conn.public(), "summary": result[0] if result else None, "error": error})
        if out:
            t = metrics.totals(out)
            metrics_store.record(t)
            last_totals.clear()
            last_totals.update(t)
        return out

    @app.get("/api/dashboard/history", dependencies=api)
    def dashboard_history(days: int = 30):
        """Daily snapshots for the dashboard's trends, and today's totals with the health score's parts."""
        return {**metrics_store.history(max(2, min(365, days))), "today": dict(last_totals)}

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
        local = request.client is not None and request.client.host in local_addresses
        if settings.shell == "off":
            reason = "The PowerShell page is turned off (KASTEC_SHELL=off)."
        elif settings.shell != "on" and not local:
            reason = ("For safety, PowerShell only works in a browser on the computer KasTec runs on. "
                      "Set KASTEC_SHELL=on to allow it from other computers.")
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

    @app.get("/api/shell", dependencies=admin_only)
    def get_shell(request: Request):
        return shell_status(request)

    @app.post("/api/shell/run", dependencies=admin_only)
    def shell_run(body: ShellBody, request: Request):
        return require_shell(request).run(body.command)

    @app.post("/api/shell/connect", dependencies=admin_only)
    def shell_connect(body: ShellConnectBody, request: Request):
        session = require_shell(request)
        conn = store.get(body.connection_id)
        if not conn:
            raise HTTPException(404, "Connection not found")
        if conn.demo:
            raise HTTPException(422, f"{conn.name} is a demo connection, so there's no real server to connect to.")
        if conn.kind not in ("ad", "vsphere"):
            raise HTTPException(422, "PowerShell can only sign in to Active Directory and vCenter connections.")
        host = None
        if body.dc and conn.kind == "ad":
            known = [conn.config.get("host", "")] + list(conn.config.get("known_dcs") or [])
            want = body.dc.strip().split(".")[0].lower()
            host = next((h for h in known if h and h.split(".")[0].lower() == want), None)
            if not host:
                raise HTTPException(422, f"{body.dc} isn't one of {conn.name}'s known domain controllers.")
        target = f"{conn.name} via {host}" if host else conn.name
        audit(request, "PowerShell: signed session in", target, conn)
        return session.run(connect_script(conn, host), timeout=120, log_as=f"[connect] {target} ({conn.kind})")

    @app.post("/api/shell/reset", dependencies=admin_only)
    def shell_reset(request: Request):
        session = require_shell(request)
        session.reset()
        return {"ok": True, "cwd": session.cwd}

    # --- Active Directory --------------------------------------------------

    @app.get("/api/ad/{conn_id}/domain-controllers", dependencies=api)
    def ad_domain_controllers(conn_id: str):
        conn = get_conn(conn_id, "ad")
        with open_connector(conn) as c:
            dcs = c.domain_controllers()
            via = getattr(c, "server_host", None)
        learn_dcs(conn, [d.get("dns_name") for d in dcs])
        checks = [(dc, port) for dc in dcs if "ports" not in dc for port in DC_PORTS]
        if checks:
            with ThreadPoolExecutor(max_workers=min(32, len(checks))) as pool:
                results = list(pool.map(lambda x: port_open(x[0]["dns_name"] or x[0]["name"], x[1]), checks))
            for (dc, port), ok in zip(checks, results):
                dc.setdefault("ports", {})[str(port)] = ok
        return {"dcs": dcs, "ports": {str(p): label for p, label in DC_PORTS.items()},
                "configured_dc": conn.config.get("host"), "connected_via": via}

    def find_dc(conn: Connection, name: str) -> tuple[dict, list[dict]]:
        name = name.strip()
        with open_connector(conn) as c:
            dcs = c.domain_controllers()
        learn_dcs(conn, [d.get("dns_name") for d in dcs])
        dc = next((d for d in dcs if d["name"].lower() == name.lower()), None)
        if not dc:
            raise HTTPException(404, f"{name} isn't a domain controller in {conn.name}.")
        return dc, dcs

    def replication_for(conn: Connection, dc: dict, dcs: list[dict]):
        """The DC's replication partners, or why they couldn't be read (None: no PowerShell here)."""
        if conn.demo:
            return dcrestart.demo_partners(dc, dcs)
        if not exe:
            return None
        return powershell.replication_status(exe, conn, dcrestart.host_of(dc))[1]

    @app.post("/api/ad/{conn_id}/domain-controllers/precheck", dependencies=admin_only)
    def ad_restart_precheck(conn_id: str, body: DCNameBody):
        """The automatic "before you restart" checks for one DC."""
        conn = get_conn(conn_id, "ad")
        dc, dcs = find_dc(conn, body.name)
        return {"dc": dc, "checks": [
            dcrestart.partner_check(dc, dcs, port_open, conn.demo),
            dcrestart.replication_check(replication_for(conn, dc, dcs), dc["name"]),
            dcrestart.fsmo_check(dc, dcs),
            dcrestart.kastec_dc_check(dc, conn.config.get("host", "")),
        ]}

    @app.get("/api/ad/{conn_id}/domain-controllers/{name}/health", dependencies=admin_only)
    def ad_dc_health(conn_id: str, name: str, replication: bool = False):
        """Is this DC back after a restart? Polled by the restart window, so it only talks to that DC."""
        conn = get_conn(conn_id, "ad")
        if conn.demo:
            ports, dse, error = dcrestart.demo_state(conn_id, name)
            host = name
        else:
            # Only DCs KasTec has seen in this domain: the saved password is never sent anywhere else.
            known = [conn.config.get("host", "")] + list(conn.config.get("known_dcs") or [])
            host = next((h for h in known if h and h.split(".")[0].lower() == name.split(".")[0].lower()), None)
            if not host:
                raise HTTPException(404, f"{name} isn't a domain controller KasTec knows in {conn.name}.")
            ports = {str(p): port_open(host, p) for p in dcrestart.PARTNER_PORTS}
            dse, error = dcrestart.read_dse(conn, host) if all(ports.values()) else (None, "Not answering on its ports yet.")
        checks = dcrestart.back_checks(ports, dse, error)
        if replication and dse:
            if conn.demo:
                dc, dcs = find_dc(conn, name)
                partners = dcrestart.demo_partners(dc, dcs)
            else:
                partners = powershell.replication_status(exe, conn, host)[1] if exe else None
            checks.append(dcrestart.replication_check(partners, name))
        return {"host": host, "ready": all(c["status"] in ("ok", "warn") for c in checks), "checks": checks}

    @app.post("/api/ad/{conn_id}/domain-controllers/restart", dependencies=admin_only)
    def ad_restart_domain_controller(conn_id: str, body: RestartDCBody, request: Request):
        conn = get_conn(conn_id, "ad")
        name = body.name.strip()
        if body.confirm.strip().lower() != name.lower():
            raise HTTPException(422, "Type the domain controller's name exactly to confirm the restart.")
        dc, dcs = find_dc(conn, name)
        # Never leave a site without a working DC unless someone says why.
        partner = dcrestart.partner_check(dc, dcs, port_open, conn.demo)
        reason = body.override_reason.strip()
        if partner["status"] == "fail" and not reason:
            raise HTTPException(409, partner["detail"] + " Give a reason to restart anyway.")
        note = f" (pre-check overridden: {reason})" if partner["status"] == "fail" else ""
        if conn.demo:
            dcrestart.demo_restart(conn_id, dc["name"])
            audit(request, f"restarted domain controller (demo){note}", dc["name"], conn)
            return {"ok": True, "message": f"{dc['name']} is a demo DC: KasTec simulates the restart."}
        if not exe:
            raise HTTPException(409, "PowerShell wasn't found on the computer KasTec runs on, so it can't restart DCs.")
        ok, output = powershell.restart_domain_controller(exe, conn, dcrestart.host_of(dc))
        audit(request, ("restarted domain controller" if ok else "domain controller restart failed") + note, dc["name"], conn)
        if not ok:
            raise HTTPException(502, f"{dc['name']} wasn't restarted: {output}")
        return {"ok": True, "message": f"Restart sent to {dc['name']}. A domain controller is usually back within 5-10 minutes."}

    @app.post("/api/ad/{conn_id}/domain-controllers/restart-complete", dependencies=admin_only)
    def ad_restart_complete(conn_id: str, body: RestartCompleteBody, request: Request):
        """Record how the restart went, from the after-restart checks in the restart window."""
        conn = get_conn(conn_id, "ad")
        notes = f": {body.notes.strip()}" if body.notes.strip() else ""
        audit(request, f"DC restart checked: {body.outcome}{notes}", body.name.strip(), conn)
        return {"ok": True}

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
    def ad_enable(conn_id: str, body: DnBody, request: Request):
        conn = get_conn(conn_id, "ad")
        with open_connector(conn) as c:
            c.set_enabled(body.dn, True)
        audit(request, "enabled AD account", body.dn, conn)
        return {"ok": True}

    @app.post("/api/ad/{conn_id}/users/disable", dependencies=api)
    def ad_disable(conn_id: str, body: DnBody, request: Request):
        conn = get_conn(conn_id, "ad")
        with open_connector(conn) as c:
            c.set_enabled(body.dn, False)
        audit(request, "disabled AD account", body.dn, conn)
        return {"ok": True}

    @app.post("/api/ad/{conn_id}/users/unlock", dependencies=api)
    def ad_unlock(conn_id: str, body: DnBody, request: Request):
        conn = get_conn(conn_id, "ad")
        with open_connector(conn) as c:
            c.unlock(body.dn)
        audit(request, "unlocked AD account", body.dn, conn)
        return {"ok": True}

    @app.post("/api/ad/{conn_id}/users/reset-password", dependencies=api)
    def ad_reset_password(conn_id: str, body: PasswordBody, request: Request):
        conn = get_conn(conn_id, "ad")
        with open_connector(conn) as c:
            c.reset_password(body.dn, body.new_password, unlock=body.unlock, must_change=body.must_change)
        extras = [x for x, on in (("unlocked", body.unlock), ("must change at next sign-in", body.must_change)) if on]
        audit(request, "reset AD password" + (f" ({', '.join(extras)})" if extras else ""), body.dn, conn)
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
    def vs_power(conn_id: str, vm_id: str, body: PowerBody, request: Request):
        conn = get_conn(conn_id, "vsphere")
        with open_connector(conn) as c:
            c.power(vm_id, body.action)
        audit(request, f"VM power: {body.action}", vm_id, conn)
        return {"ok": True}

    @app.get("/api/vsphere/{conn_id}/topology", dependencies=api)
    def vs_topology(conn_id: str):
        with open_connector(get_conn(conn_id, "vsphere")) as c:
            return c.topology()

    @app.get("/api/vsphere/{conn_id}/network", dependencies=api)
    def vsphere_network(conn_id: str, refresh: bool = False):
        """How hosts, physical and virtual switches, port groups and VMs connect (read with PowerCLI)."""
        conn = get_conn(conn_id, "vsphere")
        try:
            if conn.demo:
                with open_connector(conn) as c:
                    return netmap.build(c.network_raw())
            return netmaps.topology(conn, force=refresh)
        except netmap.NetMapError as e:
            raise HTTPException(502, str(e)) from e

    @app.get("/api/vsphere/{conn_id}/network/traffic", dependencies=api)
    def vsphere_network_traffic(conn_id: str):
        """Received/transmitted bits per second on each physical NIC and powered-on VM, from vCenter's real-time stats."""
        conn = get_conn(conn_id, "vsphere")
        try:
            if conn.demo:
                with open_connector(conn) as c:
                    return c.network_traffic()
            return netmaps.traffic(conn)
        except netmap.NetMapError as e:
            raise HTTPException(502, str(e)) from e

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

    # --- SolarWinds --------------------------------------------------------

    @app.get("/api/solarwinds/status", dependencies=api)
    def solarwinds_status():
        """Every monitored node's status from all SolarWinds connections, keyed by short host name,
        so other pages (e.g. the vSphere topology) can show it next to the same machine."""
        nodes, errors = {}, []
        for conn, found, error in gather(store.list("solarwinds"), lambda c: c.nodes()):
            if error:
                errors.append({"connection": conn.name, "error": error})
                continue
            for n in found:
                nodes.setdefault(_short_name(n["name"]), {"status": n["status"], "group": n["group"],
                                                         "name": n["name"], "connection": conn.name})
        return {"connections": len(store.list("solarwinds")), "nodes": nodes, "errors": errors}

    @app.get("/api/solarwinds/{conn_id}/overview", dependencies=api)
    def solarwinds_overview(conn_id: str):
        with open_connector(get_conn(conn_id, "solarwinds")) as c:
            return c.overview()

    # --- VNX storage -------------------------------------------------------

    @app.get("/api/vnx/{conn_id}/overview", dependencies=api)
    def vnx_overview(conn_id: str):
        """The array's health, pools, LUNs and storage groups, with each LUN matched to a vSphere
        datastore (and the VMs on it) where one has the same name."""
        conn = get_conn(conn_id, "vnx")
        with ThreadPoolExecutor(max_workers=2) as pool:
            stores_job = pool.submit(gather, store.list("vsphere"), lambda c: c.datastores_with_vms())
            with open_connector(conn) as c:
                data = c.overview()
                remember(conn, c)
            stores, errors = [], []
            for vc, found, error in stores_job.result():
                if error:
                    errors.append({"connection": vc.name, "error": error})
                else:
                    stores += [{**d, "connection": vc.name} for d in found]
        link_datastores(data["luns"], stores)
        data["vsphere"] = {"connections": len(store.list("vsphere")), "datastores": len(stores), "errors": errors}
        return data

    provision_lock = threading.Lock()

    def powercli_installed() -> bool:
        if not exe:
            return False
        ok, out = powershell.run_script(exe, provision.POWERCLI_CHECK, timeout=90)
        return ok and "POWERCLI|yes" in out

    def provision_state(conn: Connection, body: ProvisionBody) -> tuple[dict, dict | None, Connection | None]:
        """A fresh read of the array, and of the chosen vCenter when a datastore is wanted."""
        with open_connector(conn) as c:
            array = c.read()
            remember(conn, c)
        if not body.datastore:
            return array, None, None
        vconn = store.get(body.datastore.connection_id)
        if not vconn or vconn.kind != "vsphere":
            raise HTTPException(404, "vCenter connection not found")
        with open_connector(vconn) as v:
            vc = {"name": vconn.name, "clusters": v.clusters_with_hosts(), "datastores": [d["name"] for d in v.datastores()],
                  "powercli": True if vconn.demo else powercli_installed()}
        return array, vc, vconn

    @app.get("/api/vnx/{conn_id}/provision/options", dependencies=admin_only)
    def vnx_provision_options(conn_id: str):
        """What the Create LUN form can offer: pools, storage groups and each vCenter's clusters."""
        conn = get_conn(conn_id, "vnx")
        with open_connector(conn) as c:
            array = c.read()
            remember(conn, c)
        vcenters, errors = [], []
        for vc, clusters, error in gather(store.list("vsphere"), lambda v: v.clusters_with_hosts()):
            if error:
                errors.append({"connection": vc.name, "error": error})
            else:
                vcenters.append({"id": vc.id, "name": vc.name, "demo": vc.demo, "clusters": clusters})
        return {
            "array": {"model": array["agent"]["model"], "serial": array["agent"]["serial"]},
            "pools": [{k: p[k] for k in ("name", "free_gb", "user_gb", "percent_full", "percent_subscribed", "status")} for p in array["pools"]],
            "storage_groups": [{"name": g["name"], "hosts": g["hosts"], "luns": len(g["luns"])} for g in array["groups"] if not g["special"]],
            "lun_names": [lun["name"] for lun in array["luns"]],
            "vcenters": vcenters, "errors": errors, "powershell": bool(exe),
        }

    @app.post("/api/vnx/{conn_id}/provision/plan", dependencies=admin_only)
    def vnx_provision_plan(conn_id: str, body: ProvisionBody):
        array, vc, _ = provision_state(get_conn(conn_id, "vnx"), body)
        return provision.plan(body.model_dump(), array, vc)

    @app.post("/api/vnx/{conn_id}/provision", dependencies=admin_only)
    def vnx_provision(conn_id: str, body: ProvisionBody, request: Request):
        """Create the LUN, add it to the storage groups, then create the datastore: each step only if the one
        before worked, each one recorded in the activity log."""
        conn = get_conn(conn_id, "vnx")
        if body.confirm.strip() != body.name.strip():
            raise HTTPException(422, "Type the LUN name to confirm.")
        if not provision_lock.acquire(blocking=False):
            raise HTTPException(409, "KasTec is already creating a LUN. Wait for it to finish.")
        try:
            array, vc, vconn = provision_state(conn, body)
            p = provision.plan(body.model_dump(), array, vc)
            if not p["ok"]:
                raise HTTPException(422, " ".join(p["errors"]))
            steps = []

            def step(title, ok, detail):
                steps.append({"title": title, "ok": ok, "detail": detail})
                audit(request, f"VNX: {title}" + ("" if ok else " (failed)"), detail[:500], conn)
                return ok

            lun_cfg = p["lun"]
            try:
                with open_connector(conn) as c:
                    lun = c.create_lun(lun_cfg["name"], lun_cfg["pool"], lun_cfg["size_gb"], lun_cfg["thin"], lun_cfg["sp"])
                    step(f"Created LUN {lun['name']}", True,
                         f"LUN {lun['number']}, {lun_cfg['size_gb']} GB {'thin' if lun_cfg['thin'] else 'thick'} in {lun_cfg['pool']}, "
                         f"owned by SP {lun_cfg['sp'].upper()}, {lun['naa']}")
                    for g in p["storage_groups"]:
                        c.add_to_group(g, p["hlu"], lun["number"])
                        step(f"Presented it to {g}", True, f"HLU {p['hlu']}")
            except ConnectorError as e:
                step("Presenting the LUN to its storage groups" if steps else f"Creating LUN {lun_cfg['name']}", False, str(e))
                return {"ok": False, "steps": steps,
                        "note": (f"LUN {lun_cfg['name']} exists on the array; KasTec didn't remove it. Fix the problem above and "
                                 "finish in Unisphere or with the Control Station commands box.") if len(steps) > 1 else ""}

            ds = p["datastore"]
            if ds:
                title = f"Created datastore {ds['name']}"
                if vconn.demo:
                    try:
                        with open_connector(vconn) as v:
                            step(title, True, v.create_datastore(ds["cluster"], ds["name"], lun["naa"], lun_cfg["size_gb"], ds["vmfs"]))
                    except ConnectorError as e:
                        step(f"Creating datastore {ds['name']}", False, str(e))
                else:
                    ok, out = powershell.run_script(exe, provision.datastore_script(vconn, ds["cluster"], lun["naa"], ds["name"], ds["vmfs"]),
                                                    timeout=900)
                    done = provision.datastore_result(out) if ok else None
                    step(title if done else f"Creating datastore {ds['name']}", bool(done), done or out[-1500:])
            ok = all(s["ok"] for s in steps)
            return {"ok": ok, "steps": steps, "lun": lun,
                    "note": "" if ok else (f"LUN {lun['name']} ({lun['naa']}) was created and presented; only the datastore "
                                          "is missing. Fix the problem above, then create the datastore in vSphere on that naa ID.")}
        finally:
            provision_lock.release()

    @app.post("/api/vnx/{conn_id}/run", dependencies=admin_only)
    def vnx_run(conn_id: str, body: VNXCommandBody, request: Request):
        """An administrator's own command on the Control Station, like typing it in PuTTY."""
        conn = get_conn(conn_id, "vnx")
        audit(request, "VNX: ran a Control Station command", body.command[:500], conn)
        with open_connector(conn) as c:
            result = c.run_command(body.command)
            remember(conn, c)
        return result

    # --- employee activity -------------------------------------------------

    membership = {"at": 0.0, "key": None, "users": set(), "names": {}, "error": None}
    membership_lock = threading.Lock()

    def monitored(force: bool = False) -> tuple[set[str], dict, str | None]:
        """Members of the monitored AD group (user names, lower case), cached for 10 minutes."""
        s = activity_store.settings()
        key = (s["ad_connection_id"], s["group"])
        with membership_lock:
            if not force and membership["key"] == key and time.time() - membership["at"] < 600:
                return membership["users"], membership["names"], membership["error"]
        users, names, error = set(), {}, None
        conn = store.get(s["ad_connection_id"]) if s["ad_connection_id"] else None
        if not conn or conn.kind != "ad":
            error = "Choose the Active Directory connection that holds the monitored group, then click Save."
        elif not s["group"]:
            error = "Enter the AD group whose members are monitored."
        else:
            try:
                with open_connector(conn) as c:
                    users = c.group_member_usernames(s["group"])
                    names = dict(getattr(c, "member_names", {}))
            except ConnectorError as e:
                error = str(e)
        with membership_lock:
            membership.update(at=time.time(), key=key, users=users, names=names, error=error)
        if names:
            activity_store.set_names(names)
        return users, names, error

    def agent_auth(request: Request) -> dict:
        s = activity_store.settings()
        given = request.headers.get("x-kastec-agent-key", "")
        if not s["agent_key"] or not secrets.compare_digest(given.encode(), s["agent_key"].encode()):
            raise HTTPException(401, "Wrong agent key")
        return s

    def agent_user(raw: str) -> str:
        user = raw.strip().split("\\")[-1].split("@")[0].lower()
        if not activity.USER.match(user):
            raise HTTPException(422, "Invalid user name")
        return user

    def should_monitor(s: dict, user: str) -> bool:
        if not s["enabled"]:
            return False
        users, _, error = monitored()
        return not error and user in users

    @app.get("/api/activity/agent/config")
    def activity_agent_config(user: str, request: Request):
        """The agent asks at start (and every hour) whether to record this user, and what to tell them."""
        s = agent_auth(request)
        return {"monitor": should_monitor(s, agent_user(user)), "notice": s["notice"], "sample_seconds": 15,
                "upload_seconds": 300}

    @app.post("/api/activity/agent/report")
    def activity_agent_report(body: AgentReportBody, request: Request):
        s = agent_auth(request)
        user = agent_user(body.user)
        computer = body.computer.strip().lower()
        if not activity.COMPUTER.match(computer):
            raise HTTPException(422, "Invalid computer name")
        if not should_monitor(s, user):
            return {"accepted": 0, "monitor": False}
        names = monitored()[1]
        return {"accepted": activity_store.ingest(user, computer, body.samples, name=names.get(user, "")), "monitor": True}

    def activity_day(value: str | None):
        try:
            return datetime.strptime(value, "%Y-%m-%d").date() if value else datetime.now().date()
        except ValueError as e:
            raise HTTPException(422, "Dates look like 2026-11-17") from e

    def activity_call(fn, *args):
        try:
            return fn(*args)
        except activity.ActivityError as e:
            raise HTTPException(422, str(e)) from e

    @app.get("/api/activity/summary", dependencies=admin_only)
    def activity_summary(period: str = "month", date: str | None = None, user: str | None = None, scheduled: bool = True):
        return activity_call(activity.summary, activity_store, period, activity_day(date), user or None, scheduled)

    @app.get("/api/activity/live", dependencies=admin_only)
    def activity_live():
        return activity.live(activity_store)

    @app.get("/api/activity/attendance", dependencies=admin_only)
    def activity_attendance(period: str = "week", date: str | None = None, user: str | None = None):
        s = activity_store.settings()
        start, end, days = activity_call(activity.period_bounds, period, activity_day(date))
        now = datetime.now()
        rows = activity.attendance(activity_store._rows(start, end, user or None), s["schedule"], days, now.date(),
                                   now.hour * 60 + now.minute)
        people = activity_store.people()
        return [{**r, "name": people.get(r["user"], {}).get("name") or r["user"]} for r in rows]

    @app.get("/api/activity/apps", dependencies=admin_only)
    def activity_apps(period: str = "month", date: str | None = None):
        return activity_call(activity.apps, activity_store, period, activity_day(date))

    @app.get("/api/activity/people", dependencies=admin_only)
    def activity_people():
        return sorted(activity_store.people().values(), key=lambda p: p["name"].lower())

    @app.get("/api/activity/settings", dependencies=admin_only)
    def activity_settings():
        s = activity_store.settings()
        users, _, error = monitored()
        return {**s, "members": len(users), "membership_error": error, "demo_data": activity_store.has_demo(),
                "ad_connections": [{"id": c.id, "name": c.name} for c in store.list("ad")]}

    @app.put("/api/activity/settings", dependencies=admin_only)
    def activity_save_settings(body: dict, request: Request):
        old = activity_store.settings()
        new = activity_call(activity.clean_settings, body, old)
        activity_store.save_settings(new)
        changed = sorted(k for k in new if new[k] != old.get(k))
        audit(request, "Employee activity: changed settings", ", ".join(changed))
        monitored(force=True)
        return activity_settings()

    @app.post("/api/activity/settings/new-key", dependencies=admin_only)
    def activity_new_key(request: Request):
        activity_store.new_agent_key()
        audit(request, "Employee activity: made a new agent key", "agents using the old key stop reporting")
        return activity_settings()

    @app.post("/api/activity/demo", dependencies=admin_only)
    def activity_demo(request: Request):
        names = {}
        demo_ad = next((c for c in store.list("ad") if c.demo), None)
        if demo_ad:
            with open_connector(demo_ad) as c:
                c.group_member_usernames("demo")
                names = dict(c.member_names)
        if not names:
            names = {f"user{i:02d}": f"Demo User {i}" for i in range(1, 21)}
        n = activity.load_demo(activity_store, names)
        audit(request, "Employee activity: loaded demo data", f"{len(names)} people")
        return {"people": len(names), "minutes": n}

    @app.delete("/api/activity/demo", dependencies=admin_only)
    def activity_clear_demo(request: Request):
        activity_store.clear_demo()
        audit(request, "Employee activity: removed demo data")
        return {"ok": True}

    @app.get("/api/activity/agent.ps1", dependencies=admin_only)
    def activity_agent_script(request: Request, server: str | None = None):
        """The agent with this server's address and the agent key filled in, ready for Group Policy."""
        base = (server or str(request.base_url)).rstrip("/")
        if not re.match(r"^https?://[A-Za-z0-9.-]+(:\d+)?$", base):
            raise HTTPException(422, "The server address looks like https://kastec.btcbahamas.com:8443")
        text = (Path(__file__).parent.parent / "agent" / "KasTecAgent.ps1").read_text(encoding="utf-8")
        text = text.replace("__KASTEC_SERVER__", base).replace("__KASTEC_AGENT_KEY__", activity_store.settings()["agent_key"])
        audit(request, "Employee activity: downloaded the agent", base)
        return Response(text, media_type="text/plain; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="KasTecAgent.ps1"'})

    # --- getting into servers: RDP files, SSH terminals, guest commands ---------

    @app.get("/api/connect/rdp", dependencies=api)
    def connect_rdp(host: str, request: Request, name: str = "", user: str = ""):
        """A Remote Desktop file for mstsc on the viewer's PC; the connection runs from there."""
        try:
            text = remote.rdp_file(host.strip(), user.strip())
        except remote.RemoteError as e:
            raise HTTPException(422, str(e)) from e
        filename = re.sub(r"[^A-Za-z0-9._-]", "_", name or host)[:60] or "server"
        audit(request, "RDP: downloaded a connection file", host)
        return Response(text, media_type="application/x-rdp",
                        headers={"Content-Disposition": f'attachment; filename="{filename}.rdp"'})

    def who(request: Request) -> str:
        return (getattr(request.state, "user", None) or {}).get("username") or "?"

    @app.post("/api/ssh/open", dependencies=admin_only)
    def ssh_open(body: SSHOpenBody, request: Request):
        host = body.host.strip()
        try:
            if body.trust_fingerprint:
                ssh.trust(host, body.trust_fingerprint.strip())
                audit(request, "SSH: trusted a server key", f"{host} {body.trust_fingerprint.strip()}")
            s = ssh.open(host, body.port, body.username.strip(), body.password, who(request))
        except remote.HostKeyNeeded as e:
            return JSONResponse(status_code=409, content={"detail": str(e), "fingerprint": e.fingerprint, "changed": e.changed})
        except remote.RemoteError as e:
            raise HTTPException(502, str(e)) from e
        audit(request, "SSH: opened a session", f"{s.user}@{s.host}")
        return {"sid": s.sid, "host": s.host, "user": s.user}

    @app.get("/api/ssh/{sid}/read", dependencies=admin_only)
    def ssh_read(sid: str, request: Request, since: int = 0):
        try:
            return ssh.read(sid, who(request), since)
        except remote.RemoteError as e:
            raise HTTPException(410, str(e)) from e

    @app.post("/api/ssh/{sid}/write", dependencies=admin_only)
    def ssh_write(sid: str, body: SSHWriteBody, request: Request):
        try:
            ssh.write(sid, who(request), body.data)
        except remote.RemoteError as e:
            raise HTTPException(410, str(e)) from e
        return {"ok": True}

    @app.delete("/api/ssh/{sid}", dependencies=admin_only)
    def ssh_close(sid: str, request: Request):
        s = ssh.close(sid, who(request))
        if s:
            audit(request, "SSH: closed a session", f"{s.user}@{s.host}")
        return {"ok": True}

    @app.post("/api/vsphere/{conn_id}/vms/{vm_id}/guest/run", dependencies=admin_only)
    def vsphere_guest_run(conn_id: str, vm_id: str, body: GuestRunBody, request: Request):
        """Run a command inside a VM through VMware Tools (PowerCLI Invoke-VMScript)."""
        conn = get_conn(conn_id, "vsphere")
        audit(request, f"Ran a command inside VM {vm_id} ({body.script_type}) as {body.username}", body.command[:300], conn)
        try:
            if conn.demo:
                with open_connector(conn) as c:
                    return c.guest_run(vm_id, body.command, body.username, body.password, body.script_type)
            return netmaps.guest_run(conn, vm_id, body.command, body.username, body.password, body.script_type)
        except netmap.NetMapError as e:
            raise HTTPException(502, str(e)) from e

    # --- iDRAC -------------------------------------------------------------

    @app.get("/api/idrac/{conn_id}/servers", dependencies=api)
    def idrac_servers(conn_id: str):
        with open_connector(get_conn(conn_id, "idrac")) as c:
            return c.servers()

    @app.get("/api/idrac/{conn_id}/servers/{host}/inventory", dependencies=api)
    def idrac_inventory(conn_id: str, host: str):
        with open_connector(get_conn(conn_id, "idrac")) as c:
            return c.inventory(host)

    # --- UI ----------------------------------------------------------------

    @app.middleware("http")
    async def revalidate_ui(request: Request, call_next):
        # Make browsers check for a newer page and scripts every time, so an update shows up at once.
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html")

    return app
