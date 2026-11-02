"""Switching to another domain controller when the usual one doesn't answer."""

import logging

import pytest
from ldap3.core.exceptions import LDAPInvalidCredentialsResult, LDAPSocketOpenError

import app.connectors.ad as ad
from app.connectors import ConnectorError

CONFIG = {"host": "dc1.corp.example.com", "base_dn": "DC=corp,DC=example,DC=com", "bind_user": "svc@corp.example.com",
          "known_dcs": ["dc1.corp.example.com", "dc2.corp.example.com", "dc3.corp.example.com"]}


class FakeConnection:
    down: set = set()
    refused: set = set()
    attempts: list = []

    def __init__(self, server, **kw):
        FakeConnection.attempts.append(server.host)
        if server.host in FakeConnection.down:
            raise LDAPSocketOpenError("socket connection error while opening: timed out")
        if server.host in FakeConnection.refused:
            raise LDAPInvalidCredentialsResult(result=49, description="invalidCredentials")

    def unbind(self):
        pass


@pytest.fixture(autouse=True)
def fake_ldap(monkeypatch):
    FakeConnection.down, FakeConnection.refused, FakeConnection.attempts = set(), set(), []
    monkeypatch.setattr(ad, "Connection", FakeConnection)
    ad._recent_failures.clear()


def connect(config=CONFIG):
    with ad.ADConnector(config, "pw") as c:
        return c.server_host


def test_uses_the_configured_dc_when_it_answers():
    assert connect() == "dc1.corp.example.com"
    assert FakeConnection.attempts == ["dc1.corp.example.com"]


def test_switches_to_another_dc_and_says_so(caplog):
    FakeConnection.down = {"dc1.corp.example.com"}
    with caplog.at_level(logging.WARNING, logger="cloudtec"):
        assert connect() == "dc2.corp.example.com"
    assert FakeConnection.attempts == ["dc1.corp.example.com", "dc2.corp.example.com"]
    assert "dc1.corp.example.com didn't answer, so KasTec is using dc2.corp.example.com" in caplog.text


def test_a_dc_that_just_failed_is_tried_last():
    FakeConnection.down = {"dc1.corp.example.com"}
    connect()
    FakeConnection.attempts = []
    assert connect() == "dc2.corp.example.com"
    assert FakeConnection.attempts == ["dc2.corp.example.com"]      # no wait on dc1 again
    FakeConnection.down = set()                                      # dc1 is back
    ad._recent_failures.clear()                                      # (after the minute is up)
    assert connect() == "dc1.corp.example.com"


def test_wrong_password_does_not_try_every_dc():
    FakeConnection.refused = {"dc1.corp.example.com", "dc2.corp.example.com", "dc3.corp.example.com"}
    with pytest.raises(ConnectorError, match="invalidCredentials"):
        connect()
    assert FakeConnection.attempts == ["dc1.corp.example.com"]      # would risk locking the account out


def test_all_dcs_down():
    FakeConnection.down = set(CONFIG["known_dcs"])
    with pytest.raises(ConnectorError) as e:
        connect()
    assert "dc1.corp.example.com" in str(e.value) and "dc3.corp.example.com" in str(e.value)
    assert "doesn't know" not in str(e.value)


def test_without_learned_dcs_the_error_explains_why():
    FakeConnection.down = {"dc1.corp.example.com"}
    with pytest.raises(ConnectorError, match="doesn't know the domain's other DCs yet"):
        connect({k: v for k, v in CONFIG.items() if k != "known_dcs"})
