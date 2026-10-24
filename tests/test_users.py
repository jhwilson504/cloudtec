"""Console user accounts, roles, sign-in throttling and the activity log."""

import json

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

ADMIN_PW = "correct-horse"


@pytest.fixture
def app(tmp_path):
    return create_app(Settings(data_dir=tmp_path, secret_key=None, admin_password=ADMIN_PW,
                               secure_cookies=False, session_hours=1))


@pytest.fixture
def admin(app):
    with TestClient(app) as c:
        assert c.post("/api/login", json={"username": "admin", "password": ADMIN_PW}).status_code == 200
        yield c


def sign_in(app, username, password):
    c = TestClient(app)
    r = c.post("/api/login", json={"username": username, "password": password})
    return c, r


def add(admin, username="jsmith", role="operator", password="Op3rator-pw"):
    r = admin.post("/api/users", json={"username": username, "display_name": "Jane Smith",
                                       "role": role, "password": password})
    assert r.status_code == 201, r.text
    return r.json()


def test_admin_login_defaults_to_admin_username(app):
    _, r = sign_in(app, "", ADMIN_PW)
    assert r.status_code == 200 and r.json()["role"] == "admin"


def test_add_user_and_sign_in(app, admin, tmp_path):
    add(admin)
    users = {u["username"]: u for u in admin.get("/api/users").json()}
    assert users["admin"]["builtin"] and users["jsmith"]["role"] == "operator"
    assert "password_hash" not in users["jsmith"]
    assert b"Op3rator-pw" not in (tmp_path / "cloudtec.db").read_bytes()

    c, r = sign_in(app, "JSMITH", "Op3rator-pw")    # usernames are case-insensitive
    assert r.status_code == 200
    me = c.get("/api/me").json()
    assert me["username"] == "jsmith" and me["role"] == "operator" and me["display_name"] == "Jane Smith"

    _, r = sign_in(app, "jsmith", "wrong-password")
    assert r.status_code == 401 and r.json()["detail"] == "Wrong username or password"


def test_operator_permissions(app, admin):
    add(admin)
    op, _ = sign_in(app, "jsmith", "Op3rator-pw")
    ad = admin.post("/api/connections", json={"kind": "ad", "name": "Corp", "demo": True}).json()["id"]

    # Day-to-day work is allowed
    assert op.get("/api/dashboard").status_code == 200
    assert op.get("/api/connections").status_code == 200
    user = next(u for u in op.get(f"/api/ad/{ad}/users").json() if u["enabled"])
    assert op.post(f"/api/ad/{ad}/users/unlock", json={"dn": user["dn"]}).status_code == 200
    assert op.post(f"/api/ad/{ad}/users/reset-password",
                   json={"dn": user["dn"], "new_password": "N3w-Passw0rd!"}).status_code == 200

    # Administration is not
    assert op.post("/api/connections", json={"kind": "ad", "name": "x", "demo": True}).status_code == 403
    assert op.delete(f"/api/connections/{ad}").status_code == 403
    assert op.get("/api/users").status_code == 403
    assert op.post("/api/users", json={"username": "evil", "role": "admin", "password": "12345678"}).status_code == 403
    assert op.get("/api/audit").status_code == 403
    assert op.post("/api/shell/run", json={"command": "whoami"}).status_code == 403


def test_operator_changes_own_password(app, admin):
    add(admin)
    op, _ = sign_in(app, "jsmith", "Op3rator-pw")
    other, _ = sign_in(app, "jsmith", "Op3rator-pw")
    assert op.get("/api/settings").json() == {"password_from_env": False}
    r = op.post("/api/settings/password", json={"current_password": "nope", "new_password": "Brand-new-pw1"})
    assert r.status_code == 400
    r = op.post("/api/settings/password", json={"current_password": "Op3rator-pw", "new_password": "Brand-new-pw1"})
    assert r.status_code == 200
    assert op.get("/api/me").json()["signed_in"] is True        # this browser stays signed in
    assert other.get("/api/dashboard").status_code == 401        # their other sessions don't
    assert admin.get("/api/dashboard").status_code == 200        # other users are unaffected
    assert sign_in(app, "jsmith", "Brand-new-pw1")[1].status_code == 200


def test_disable_reset_and_delete_sign_user_out(app, admin):
    add(admin)
    op, _ = sign_in(app, "jsmith", "Op3rator-pw")

    assert admin.patch("/api/users/jsmith", json={"disabled": True}).json()["disabled"] is True
    assert op.get("/api/dashboard").status_code == 401
    assert sign_in(app, "jsmith", "Op3rator-pw")[1].status_code == 401

    admin.patch("/api/users/jsmith", json={"disabled": False, "password": "Reset-by-admin1"})
    assert sign_in(app, "jsmith", "Op3rator-pw")[1].status_code == 401
    op, r = sign_in(app, "jsmith", "Reset-by-admin1")
    assert r.status_code == 200

    admin.patch("/api/users/jsmith", json={"role": "admin"})
    assert op.get("/api/users").status_code == 200               # role changes apply at once

    assert admin.delete("/api/users/jsmith").status_code == 200
    assert op.get("/api/dashboard").status_code == 401
    assert admin.delete("/api/users/jsmith").status_code == 404


def test_username_rules(admin):
    for bad in ("admin", "Admin", "a", "has space", "x" * 41, "semi;colon"):
        assert admin.post("/api/users", json={"username": bad, "password": "12345678"}).status_code == 422, bad
    add(admin)
    assert admin.post("/api/users", json={"username": "JSmith", "password": "12345678"}).status_code == 409
    assert admin.post("/api/users", json={"username": "short", "password": "1234567"}).status_code == 422
    assert admin.patch("/api/users/admin", json={"disabled": True}).status_code == 422
    assert admin.delete("/api/users/admin").status_code == 422


def test_too_many_wrong_passwords_pauses_sign_in(app, admin):
    add(admin)
    for _ in range(5):
        assert sign_in(app, "jsmith", "wrong")[1].status_code == 401
    _, r = sign_in(app, "jsmith", "Op3rator-pw")        # even the right password, for now
    assert r.status_code == 429 and "Try again" in r.json()["detail"]
    assert sign_in(app, "admin", ADMIN_PW)[1].status_code == 200   # other accounts aren't affected


def test_activity_log_records_who_did_what(app, admin, tmp_path):
    add(admin)
    op, _ = sign_in(app, "jsmith", "Op3rator-pw")
    ad = admin.post("/api/connections", json={"kind": "ad", "name": "Corp", "demo": True}).json()["id"]
    user = next(u for u in op.get(f"/api/ad/{ad}/users").json() if u["enabled"])
    op.post(f"/api/ad/{ad}/users/reset-password",
            json={"dn": user["dn"], "new_password": "N3w-Passw0rd!", "unlock": True})
    sign_in(app, "jsmith", "wrong")

    log = admin.get("/api/audit").json()          # newest first
    assert log[0]["action"] == "sign-in failed" and log[0]["user"] == "jsmith"
    reset = next(e for e in log if e["action"].startswith("reset AD password"))
    assert reset["user"] == "jsmith" and reset["target"] == user["dn"] and reset["connection"] == "Corp"
    assert "unlocked" in reset["action"]
    assert any(e["action"] == "added user (operator)" and e["user"] == "admin" for e in log)
    raw = (tmp_path / "audit.log").read_text()
    assert "N3w-Passw0rd!" not in raw and "Op3rator-pw" not in raw
    assert all(json.loads(line) for line in raw.splitlines())
