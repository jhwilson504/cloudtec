"""Error watch: noting KasTec's own errors, keeping them anonymous, and reporting them only when an admin says so."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.errorwatch import ErrorWatch, fingerprint, js_frames, redact
from app.main import create_app

PASSWORD = "correct horse battery"


@pytest.fixture
def setup(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, secret_key=None, admin_password=PASSWORD,
                              secure_cookies=False, session_hours=1, shell="off"))

    @app.get("/api/test-boom")
    def boom():
        return {}["missing-key-on-BTCPRODDC1.btcbahamas.com"]

    c = TestClient(app, raise_server_exceptions=False)
    c.post("/api/login", json={"password": PASSWORD})
    return app, c, tmp_path


def test_redact_keeps_the_bug_and_drops_the_network():
    assert redact("Cannot set properties of null (setting 'innerHTML')") == "Cannot set properties of null (setting 'innerHTML')"
    out = redact("bind to BTCPRODDC1.btcbahamas.com:636 as btcbahamas\\a-jovan.wilson failed; 10.50.241.81; "
                 "CN=Jovan Wilson,OU=IT,DC=btcbahamas,DC=com; jfkvh10; https://jfkvcs/sdk; me@corp.com; \\\\srv\\share",
                 extra=["Corp Main"])
    for leaked in ("BTCPRODDC1", "btcbahamas", "jovan", "Wilson", "10.50", "jfkvh10", "jfkvcs", "corp.com", "srv"):
        assert leaked.lower() not in out.lower(), (leaked, out)
    assert redact("KeyError: 'secret-host'") == "KeyError: '…'"
    assert "Corp Main" not in redact("connection Corp Main failed", extra=["Corp Main"])


def test_browser_frames_and_stable_fingerprints():
    stack = "TypeError: x\n    at renderEcosystem (https://10.0.0.5:8000/static/app.js:1234:9)\n    at async route (https://h/static/app.js:99:5)"
    assert js_frames(stack) == ["static/app.js:1234 in renderEcosystem", "static/app.js:99 in route"]
    # Same bug after an edit moved the lines: same fingerprint.
    a = fingerprint("browser", "TypeError", ["static/app.js:1234 in renderEcosystem"], "x of null")
    assert a == fingerprint("browser", "TypeError", ["static/app.js:1301 in renderEcosystem"], "x of null")


def test_server_bug_is_noted_and_explained(setup):
    app, c, _ = setup
    r = c.get("/api/test-boom")
    assert r.status_code == 500 and "Error reports" in r.json()["detail"]
    c.get("/api/test-boom")
    errs = c.get("/api/errors").json()["errors"]
    assert len(errs) == 1 and errs[0]["type"] == "KeyError" and errs[0]["count"] == 2 and errs[0]["kind"] == "server"
    assert "btcbahamas" not in json.dumps(errs).lower() and "BTCPRODDC1" not in json.dumps(errs)
    assert any("tests/test_errorwatch.py" in f for f in errs[0]["frames"])
    assert errs[0]["where"] == "GET /api/test-boom"


def test_browser_crash_is_noted(setup):
    _, c, _ = setup
    c.post("/api/errors/client", json={"message": "Cannot set properties of null (setting 'innerHTML')",
                                        "stack": "TypeError\n at renderX (http://h/static/app.js:10:1)", "page": "#/ecosystem", "kind": "TypeError"})
    e = c.get("/api/errors").json()["errors"][0]
    assert e["kind"] == "browser" and e["frames"] == ["static/app.js:10 in renderX"] and e["where"] == "#/ecosystem"


def test_nothing_is_sent_without_a_token_and_reports_go_out_redacted(tmp_path):
    seen = []

    def gh(request: httpx.Request):
        seen.append((request.method, request.url.path, json.loads(request.content or b"{}"), request.headers["authorization"]))
        return httpx.Response(201, json={"number": 42})

    w = ErrorWatch(tmp_path, b"k" * 43 + b"=", "test", transport=httpx.MockTransport(gh))
    w.extra_terms = ["BTC Domain"]
    fp = w.record("server", "ValueError", "BTC Domain at 10.1.2.3 broke", ["app/main.py:1 in f"], "GET /api/x")
    with pytest.raises(ValueError, match="token"):
        w.send(fp)
    with pytest.raises(ValueError, match="token"):
        w.save_settings(auto_send=True)
    w.save_settings(token="github_pat_secret", repo="owner/repo")
    assert "github_pat_secret" not in (tmp_path / "errorwatch.json").read_text()      # stored encrypted
    assert w.send(fp)["issue"] == 42
    method, path, body, auth = seen[0]
    assert (method, path, auth) == ("POST", "/repos/owner/repo/issues", "Bearer github_pat_secret")
    assert "BTC Domain" not in body["body"] and "10.1.2.3" not in body["body"] and body["labels"] == ["kastec-auto"]
    w.record("server", "ValueError", "BTC Domain at 10.1.2.3 broke", ["app/main.py:1 in f"], "GET /api/x")
    w.send(fp)
    assert seen[1][1] == "/repos/owner/repo/issues/42/comments"                     # seen again: a comment, not a new issue


def test_error_reports_are_admin_only(setup):
    app, c, _ = setup
    c.post("/api/users", json={"username": "op", "display_name": "Op", "role": "operator", "password": "operator-pass-1"})
    op = TestClient(app, raise_server_exceptions=False)
    op.post("/api/login", json={"username": "op", "password": "operator-pass-1"})
    assert op.get("/api/errors").status_code == 403
    assert op.put("/api/errors/settings", json={"auto_send": True}).status_code == 403
