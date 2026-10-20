import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

PASSWORD = "correct-horse"


@pytest.fixture
def client(tmp_path):
    settings = Settings(data_dir=tmp_path, secret_key=None, admin_password=PASSWORD,
                        secure_cookies=False, session_hours=1)
    with TestClient(create_app(settings)) as c:
        assert c.post("/api/login", json={"password": PASSWORD}).status_code == 200
        yield c


def add_demo(client, kind, name):
    r = client.post("/api/connections", json={"kind": kind, "name": name, "demo": True,
                                              "config": {"base_dn": "DC=corp,DC=example,DC=com"}})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_requires_login(tmp_path):
    settings = Settings(data_dir=tmp_path, secret_key=None, admin_password=PASSWORD,
                        secure_cookies=False, session_hours=1)
    with TestClient(create_app(settings)) as c:
        assert c.get("/api/connections").status_code == 401
        assert c.post("/api/login", json={"password": "nope"}).status_code == 401
        assert c.get("/api/me").json() == {"signed_in": False}


def test_generated_admin_password_is_persisted(tmp_path):
    settings = Settings(data_dir=tmp_path, secret_key=None, admin_password=None,
                        secure_cookies=False, session_hours=1)
    create_app(settings)
    password = (tmp_path / "admin_password").read_text().strip()
    assert len(password) >= 12
    with TestClient(create_app(settings)) as c:
        assert c.post("/api/login", json={"password": password}).status_code == 200


def test_passwords_never_returned_and_encrypted_at_rest(client, tmp_path):
    r = client.post("/api/connections", json={
        "kind": "vsphere", "name": "vc", "password": "s3cret-value",
        "config": {"host": "vc.example.com", "username": "admin@vsphere.local", "bogus": "x"},
    })
    assert r.status_code == 201
    body = r.json()
    assert "password" not in body and "secret" not in body
    assert "bogus" not in body["config"]
    assert b"s3cret-value" not in (tmp_path / "cloudtec.db").read_bytes()
    assert "s3cret-value" not in client.get("/api/connections").text


def test_real_connection_requires_password(client):
    r = client.post("/api/connections", json={"kind": "ad", "name": "x", "config": {"host": "dc"}})
    assert r.status_code == 422


def test_dashboard_and_ad_actions(client):
    ad = add_demo(client, "ad", "Corp")
    add_demo(client, "vsphere", "Lab")

    dash = client.get("/api/dashboard").json()
    assert {d["kind"] for d in dash} == {"ad", "vsphere"}
    assert all(d["error"] is None for d in dash)

    users = client.get(f"/api/ad/{ad}/users").json()
    user = next(u for u in users if u["enabled"])
    assert client.post(f"/api/ad/{ad}/users/disable", json={"dn": user["dn"]}).status_code == 200
    again = {u["dn"]: u for u in client.get(f"/api/ad/{ad}/users").json()}
    assert again[user["dn"]]["enabled"] is False

    r = client.post(f"/api/ad/{ad}/users/reset-password", json={"dn": user["dn"], "new_password": "short"})
    assert r.status_code == 422

    assert client.get(f"/api/ad/{ad}/groups").json()
    assert client.get(f"/api/ad/{ad}/computers?q=SRV").json()


def test_vm_power_and_errors(client):
    vs = add_demo(client, "vsphere", "Lab")
    vms = client.get(f"/api/vsphere/{vs}/vms").json()
    on = next(v for v in vms if v["power_state"] == "POWERED_ON")

    assert client.post(f"/api/vsphere/{vs}/vms/{on['id']}/power", json={"action": "stop"}).status_code == 200
    r = client.post(f"/api/vsphere/{vs}/vms/{on['id']}/power", json={"action": "stop"})
    assert r.status_code == 502 and "not powered on" in r.json()["detail"]

    assert client.get(f"/api/vsphere/{vs}/vms/{on['id']}").json()["name"] == on["name"]
    assert client.get(f"/api/vsphere/{vs}/vms/../../etc").status_code == 404
    assert client.get(f"/api/vsphere/{vs}/vms/not-a-vm").status_code == 502
    for part in ("hosts", "datastores", "clusters"):
        assert client.get(f"/api/vsphere/{vs}/{part}").json()


def test_wrong_kind_is_404(client):
    ad = add_demo(client, "ad", "Corp")
    assert client.get(f"/api/vsphere/{ad}/vms").status_code == 404


def test_search_and_crosscheck(client):
    add_demo(client, "ad", "Corp")
    add_demo(client, "vsphere", "Lab")

    found = client.get("/api/search?q=srv-sql").json()
    assert found["computers"] and found["vms"]
    assert all("connection_id" in v for v in found["vms"])

    statuses = {m["status"] for m in client.get("/api/crosscheck").json()["machines"]}
    assert statuses == {"matched", "vm_only", "ad_only"}


def test_unreachable_connection_does_not_break_dashboard(client):
    add_demo(client, "ad", "Corp")
    client.post("/api/connections", json={
        "kind": "vsphere", "name": "dead", "password": "x",
        "config": {"host": "127.0.0.1", "port": 1, "username": "u"},
    })
    dash = {d["name"]: d for d in client.get("/api/dashboard").json()}
    assert dash["Corp"]["error"] is None
    assert dash["dead"]["error"]


def test_delete_connection(client):
    ad = add_demo(client, "ad", "Corp")
    assert client.delete(f"/api/connections/{ad}").status_code == 200
    assert client.get("/api/connections").json() == []
    assert client.delete(f"/api/connections/{ad}").status_code == 404


def test_ui_served(client):
    assert "CloudTec Center" in client.get("/").text
    assert client.get("/static/app.js").status_code == 200
