"""KasTec Center: one console for Active Directory, vSphere and iDRAC."""

import json
import logging
import re
import smtplib
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import certgen, certwatch
from . import shell as powershell
from .auth import COOKIE, AdminPassword, LoginThrottle, PasswordError, Sessions, hash_password, verify_hash
from .config import Settings
from .connectors import ConnectorError, open_connector
from .connectors.idrac import parse_hosts
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


class ShellConnectBody(BaseModel):
    connection_id: str


class ConnectionBody(BaseModel):
    kind: Literal["ad", "vsphere", "idrac"]
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
    audit_lock = threading.Lock()
    local_addresses = this_computer_addresses()
    exe = find_powershell(settings.powershell)
    shell = PowerShellSession(exe, Path.home(), settings.data_dir / "shell_history.log") if exe else None
    watcher = certwatch.Watcher(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        watcher.start()
        yield
        watcher.stop()
        if shell:
            shell.close()

    app = FastAPI(title="KasTec Center", docs_url="/api/docs", openapi_url="/api/openapi.json",
                  lifespan=lifespan)

    @app.exception_handler(ConnectorError)
    def connector_error(_: Request, exc: ConnectorError):
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
            return c.test()

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
        audit(request, "PowerShell: signed session in", conn.name, conn)
        return session.run(connect_script(conn), timeout=120, log_as=f"[connect] {conn.name} ({conn.kind})")

    @app.post("/api/shell/reset", dependencies=admin_only)
    def shell_reset(request: Request):
        session = require_shell(request)
        session.reset()
        return {"ok": True, "cwd": session.cwd}

    # --- Active Directory --------------------------------------------------

    @app.get("/api/ad/{conn_id}/domain-controllers", dependencies=api)
    def ad_domain_controllers(conn_id: str):
        with open_connector(get_conn(conn_id, "ad")) as c:
            dcs = c.domain_controllers()
        checks = [(dc, port) for dc in dcs if "ports" not in dc for port in DC_PORTS]
        if checks:
            with ThreadPoolExecutor(max_workers=min(32, len(checks))) as pool:
                results = list(pool.map(lambda x: port_open(x[0]["dns_name"] or x[0]["name"], x[1]), checks))
            for (dc, port), ok in zip(checks, results):
                dc.setdefault("ports", {})[str(port)] = ok
        return {"dcs": dcs, "ports": {str(p): label for p, label in DC_PORTS.items()}}

    @app.post("/api/ad/{conn_id}/domain-controllers/restart", dependencies=admin_only)
    def ad_restart_domain_controller(conn_id: str, body: RestartDCBody, request: Request):
        conn = get_conn(conn_id, "ad")
        name = body.name.strip()
        if body.confirm.strip().lower() != name.lower():
            raise HTTPException(422, "Type the domain controller's name exactly to confirm the restart.")
        with open_connector(conn) as c:
            dc = next((d for d in c.domain_controllers() if d["name"].lower() == name.lower()), None)
        if not dc:
            raise HTTPException(404, f"{name} isn't a domain controller in {conn.name}.")
        if conn.demo:
            audit(request, "restarted domain controller (demo)", dc["name"], conn)
            return {"ok": True, "message": f"{dc['name']} is a demo DC, so nothing was restarted."}
        if not exe:
            raise HTTPException(409, "PowerShell wasn't found on the computer KasTec runs on, so it can't restart DCs.")
        ok, output = powershell.restart_domain_controller(exe, conn, dc["dns_name"] or dc["name"])
        audit(request, "restarted domain controller" if ok else "domain controller restart failed", dc["name"], conn)
        if not ok:
            raise HTTPException(502, f"{dc['name']} wasn't restarted: {output}")
        return {"ok": True, "message": f"Restart sent to {dc['name']}. A domain controller is usually back within 5-10 minutes."}

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

    # --- iDRAC -------------------------------------------------------------

    @app.get("/api/idrac/{conn_id}/servers", dependencies=api)
    def idrac_servers(conn_id: str):
        with open_connector(get_conn(conn_id, "idrac")) as c:
            return c.servers()

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
