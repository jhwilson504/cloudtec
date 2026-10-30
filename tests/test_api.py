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
    assert "KasTec Center" in client.get("/").text
    assert client.get("/static/app.js").status_code == 200


def file_settings(tmp_path):
    return Settings(data_dir=tmp_path, secret_key=None, admin_password=None,
                    secure_cookies=False, session_hours=1)


def test_change_password(tmp_path):
    settings = file_settings(tmp_path)
    app = create_app(settings)
    old = (tmp_path / "admin_password").read_text().strip()
    with TestClient(app) as me, TestClient(app) as other:
        assert me.post("/api/login", json={"password": old}).status_code == 200
        assert other.post("/api/login", json={"password": old}).status_code == 200
        assert me.get("/api/settings").json()["password_from_env"] is False

        r = me.post("/api/settings/password", json={"current_password": "wrong", "new_password": "new-password-1"})
        assert r.status_code == 400
        r = me.post("/api/settings/password", json={"current_password": old, "new_password": "short"})
        assert r.status_code == 422
        r = me.post("/api/settings/password", json={"current_password": old, "new_password": "new-password-1"})
        assert r.status_code == 200

        assert me.get("/api/connections").status_code == 200      # this session survives
        assert other.get("/api/connections").status_code == 401   # others are signed out

    # stored hashed, plain-text file gone, and it survives a restart
    assert not (tmp_path / "admin_password").exists()
    assert "new-password-1" not in (tmp_path / "admin_password.hash").read_text()
    with TestClient(create_app(settings)) as c:
        assert c.post("/api/login", json={"password": old}).status_code == 401
        assert c.post("/api/login", json={"password": "new-password-1"}).status_code == 200


def test_hand_edited_password_file(tmp_path):
    (tmp_path / "admin_password").write_text("MyNewPassword123\r\n")
    with TestClient(create_app(file_settings(tmp_path))) as c:
        assert c.post("/api/login", json={"password": "MyNewPassword123"}).status_code == 200


def test_env_password_cannot_be_changed_in_ui(client):
    assert client.get("/api/settings").json()["password_from_env"] is True
    r = client.post("/api/settings/password", json={"current_password": PASSWORD, "new_password": "new-password-1"})
    assert r.status_code == 409 and "KASTEC_ADMIN_PASSWORD" in r.json()["detail"]


def test_settings_requires_login(tmp_path):
    with TestClient(create_app(file_settings(tmp_path))) as c:
        r = c.post("/api/settings/password", json={"current_password": "x", "new_password": "new-password-1"})
        assert r.status_code == 401


def test_blank_password_file_is_regenerated(tmp_path):
    (tmp_path / "admin_password").write_text("\r\n")
    with TestClient(create_app(file_settings(tmp_path))) as c:
        password = (tmp_path / "admin_password").read_text().strip()
        assert len(password) >= 12
        assert c.post("/api/login", json={"password": ""}).status_code == 401
        assert c.post("/api/login", json={"password": password}).status_code == 200


def test_password_file_blanked_while_running_never_allows_empty_login(tmp_path):
    with TestClient(create_app(file_settings(tmp_path))) as c:
        (tmp_path / "admin_password").write_text("")
        assert c.post("/api/login", json={"password": ""}).status_code == 401


def test_ui_is_revalidated_so_updates_show(client):
    assert client.get("/").headers["cache-control"] == "no-cache"
    assert client.get("/static/app.js").headers["cache-control"] == "no-cache"
    assert "cache-control" not in client.get("/api/connections").headers


def test_reset_password_can_unlock(client):
    ad = add_demo(client, "ad", "Corp")
    locked = next(u for u in client.get(f"/api/ad/{ad}/users").json() if u["locked"])
    r = client.post(f"/api/ad/{ad}/users/reset-password",
                    json={"dn": locked["dn"], "new_password": "N3w-Passw0rd!", "unlock": True, "must_change": True})
    assert r.status_code == 200
    after = {u["dn"]: u for u in client.get(f"/api/ad/{ad}/users").json()}[locked["dn"]]
    assert after["locked"] is False


def test_port_open():
    import socket
    from app.main import port_open
    with socket.socket() as srv:
        srv.bind(("127.0.0.1", 0)); srv.listen()
        port = srv.getsockname()[1]
        assert port_open("127.0.0.1", port) is True
    assert port_open("127.0.0.1", port, timeout=0.5) is False


def test_domain_controllers_endpoint(client):
    ad = add_demo(client, "ad", "Corp")
    r = client.get(f"/api/ad/{ad}/domain-controllers").json()
    assert r["ports"]["9389"] == "AD Web Services"
    assert r["dcs"] and all(set(d["ports"]) == {"389", "636", "3268", "9389"} for d in r["dcs"])
    assert any("PDC" in d["roles"] for d in r["dcs"])


def test_idrac_servers(client):
    srv = add_demo(client, "idrac", "Rack")
    servers = client.get(f"/api/idrac/{srv}/servers").json()
    assert len(servers) == 6 and all(s["model"] == "PowerEdge R750" for s in servers)
    assert any(p["no_input"] for s in servers for p in s["psus"])
    dash = {d["name"]: d for d in client.get("/api/dashboard").json()}
    assert dash["Rack"]["summary"]["psu_problems"] == 2
    assert client.get(f"/api/vsphere/{srv}/vms").status_code == 404


def test_idrac_hosts_are_cleaned(client):
    r = client.post("/api/connections", json={"kind": "idrac", "name": "r", "password": "pw",
                                              "config": {"hosts": ["10.0.0.1", " 10.0.0.1 ", "10.0.0.2"], "username": "root"}})
    assert r.status_code == 201 and r.json()["config"]["hosts"] == ["10.0.0.1", "10.0.0.2"]
    r = client.post("/api/connections", json={"kind": "idrac", "name": "r", "password": "pw",
                                              "config": {"hosts": ["bad host/x"], "username": "root"}})
    assert r.status_code == 422
