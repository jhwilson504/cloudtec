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
    monkeypatch.setattr(ad, "discover_dcs", lambda domain: [])
    ad._recent_failures.clear()
    ad._discovered.clear()


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


NOT_LEARNED = {k: v for k, v in CONFIG.items() if k != "known_dcs"}


def test_without_learned_dcs_the_error_explains_why():
    FakeConnection.down = {"dc1.corp.example.com"}
    with pytest.raises(ConnectorError, match="couldn't find corp.example.com's other DCs in DNS"):
        connect(NOT_LEARNED)


def test_finds_other_dcs_in_dns_when_none_are_known(monkeypatch):
    asked = []
    monkeypatch.setattr(ad, "discover_dcs",
                        lambda domain: asked.append(domain) or ["dc1.corp.example.com", "dc2.corp.example.com"])
    FakeConnection.down = {"dc1.corp.example.com"}
    assert connect(NOT_LEARNED) == "dc2.corp.example.com"
    assert asked == ["corp.example.com"]                                    # domain from the base DN
    assert FakeConnection.attempts == ["dc1.corp.example.com", "dc2.corp.example.com"]   # dc1 not tried twice


def test_dns_is_only_asked_when_the_known_dcs_fail(monkeypatch):
    monkeypatch.setattr(ad, "discover_dcs", lambda domain: pytest.fail("shouldn't look up DNS"))
    assert connect() == "dc1.corp.example.com"


def test_discover_dcs_from_srv_records(monkeypatch):
    monkeypatch.undo()                     # the real discover_dcs, with Windows and PowerShell faked
    ad._discovered.clear()
    seen = []

    class R:
        stdout = "BTCPRODDC2.btcbahamas.com\nbtcproddc1.btcbahamas.com.\n\n"

    monkeypatch.setattr(ad.os, "name", "nt")
    monkeypatch.setattr(ad.shutil, "which", lambda name: "powershell.exe")
    monkeypatch.setattr(ad.subprocess, "run", lambda args, **kw: seen.append(args[-1]) or R())
    assert ad.discover_dcs("btcbahamas.com") == ["btcproddc1.btcbahamas.com", "btcproddc2.btcbahamas.com"]
    assert "_ldap._tcp.dc._msdcs.btcbahamas.com" in seen[0]
    ad.discover_dcs("btcbahamas.com")
    assert len(seen) == 1                                                   # cached
    assert ad.discover_dcs("bad name; Remove-Item C:\\") == []             # never reaches PowerShell


def test_discover_dcs_falls_back_to_the_domains_addresses(monkeypatch):
    monkeypatch.undo()
    ad._discovered.clear()
    monkeypatch.setattr(ad.os, "name", "posix")
    monkeypatch.setattr(ad.socket, "getaddrinfo", lambda host, *a, **k: [(0, 0, 0, "", ("10.0.0.1", 0)), (0, 0, 0, "", ("10.0.0.2", 0))])
    monkeypatch.setattr(ad.socket, "gethostbyaddr",
                        lambda ip: {"10.0.0.1": ("DC1.corp.example.com", [], [ip])}.get(ip) or (_ for _ in ()).throw(OSError()))
    assert ad.discover_dcs("corp.example.com") == ["dc1.corp.example.com"]
