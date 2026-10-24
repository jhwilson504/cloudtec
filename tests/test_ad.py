"""The real AD connector, against ldap3's in-memory mock server."""

import pytest
from ldap3 import MOCK_SYNC, OFFLINE_AD_2012_R2, Connection, Server

from app.connectors import ConnectorError
from datetime import datetime, timedelta, timezone

from app.connectors.ad import FOREVER, ADConnector, _contains, _lockout_duration

BASE = "DC=corp,DC=example,DC=com"
ALICE = f"CN=Alice Admin,OU=Staff,{BASE}"


@pytest.fixture
def ad():
    connector = ADConnector({"host": "dc01", "base_dn": BASE, "bind_user": "svc@corp.example.com"}, "pw")
    conn = Connection(Server("dc01", get_info=OFFLINE_AD_2012_R2), user=f"CN=svc,{BASE}",
                      password="pw", client_strategy=MOCK_SYNC, raise_exceptions=True)
    conn.strategy.add_entry(ALICE, {
        "objectClass": ["top", "person", "user"], "objectCategory": "person",
        "sAMAccountName": "alice", "displayName": "Alice Admin", "mail": "alice@corp.example.com",
        "userAccountControl": 512, "lockoutTime": 133500000000000000,
        "msDS-User-Account-Control-Computed": 16, "lastLogonTimestamp": 133500000000000000,
    })
    conn.strategy.add_entry(f"CN=Bob,OU=Staff,{BASE}", {
        "objectClass": ["top", "person", "user"], "objectCategory": "person",
        "sAMAccountName": "bob", "displayName": "Bob", "userAccountControl": 514,
        "lockoutTime": 133500000000000000, "msDS-User-Account-Control-Computed": 0,
    })
    conn.strategy.add_entry(f"CN=SRV-WEB01,OU=Servers,{BASE}", {
        "objectClass": ["top", "computer"], "cn": "SRV-WEB01", "dNSHostName": "srv-web01.corp.example.com",
        "operatingSystem": "Windows Server 2022 Standard", "userAccountControl": 4096,
    })
    conn.strategy.add_entry(f"CN=Helpdesk,OU=Groups,{BASE}", {
        "objectClass": ["top", "group"], "cn": "Helpdesk", "groupType": -2147483646,
        "member": [ALICE],
    })
    conn.strategy.add_entry(f"CN=svc,{BASE}", {"userPassword": "pw", "sAMAccountName": "svc"})
    conn.bind()
    connector._conn = conn
    return connector


def test_users_and_search(ad):
    users = {u["username"]: u for u in ad.users()}
    assert users["alice"]["enabled"] and users["alice"]["locked"]
    assert users["alice"]["last_logon"].startswith("2024")
    assert users["bob"]["enabled"] is False
    assert users["bob"]["locked"] is False   # old lockout that has expired by itself
    assert [u["username"] for u in ad.users("ali")] == ["alice"]


def test_search_input_is_escaped():
    assert _contains(["cn"], "*)(objectClass=*") == r"(|(cn=*\2a\29\28objectClass=\2a*))"


def test_computers_groups_summary(ad):
    [comp] = ad.computers()
    assert comp["name"] == "SRV-WEB01" and comp["enabled"]
    [group] = ad.groups()
    assert group["type"] == "Security" and group["members"] == 1
    s = ad.summary()
    assert (s["users"], s["disabled"], s["locked"], s["computers"], s["groups"]) == (2, 1, 1, 1, 1)


def test_disable_and_enable(ad):
    ad.set_enabled(ALICE, False)
    assert {u["username"]: u for u in ad.users()}["alice"]["enabled"] is False
    ad.set_enabled(ALICE, True)
    assert {u["username"]: u for u in ad.users()}["alice"]["enabled"] is True


def test_actions_stay_inside_base_dn(ad):
    with pytest.raises(ConnectorError, match="outside"):
        ad.set_enabled("CN=Admin,DC=other,DC=com", False)


def test_password_reset_needs_ldaps():
    plain = ADConnector({"host": "dc", "base_dn": BASE, "bind_user": "u", "use_ssl": False}, "pw")
    with pytest.raises(ConnectorError, match="LDAPS"):
        plain.reset_password(ALICE, "NewPassw0rd!")


def _ticks(dt):
    return int((dt - datetime(1601, 1, 1, tzinfo=timezone.utc)).total_seconds() * 10_000_000)


@pytest.mark.parametrize("duration, locked_minutes_ago, expected", [
    (-18_000_000_000, 5, True),             # 30-minute policy, locked 5 minutes ago
    (-18_000_000_000, 120, False),          # 30-minute policy, locked 2 hours ago: expired
    (0, 60 * 24 * 400, True),                # "until an admin unlocks": still locked
    (None, 5, True),                        # policy unreadable: assume AD's 10-minute default
    (None, 60, False),
])
def test_lockout_fallback_without_computed_attribute(ad, duration, locked_minutes_ago, expected):
    """Older or restricted DCs may not return msDS-User-Account-Control-Computed."""
    if duration is not None:
        ad._conn.strategy.add_entry(BASE, {"objectClass": ["top", "domain"], "lockoutDuration": duration})
    locked_at = datetime.now(timezone.utc) - timedelta(minutes=locked_minutes_ago)
    assert ad._is_locked({"lockoutTime": str(_ticks(locked_at))}) is expected
    assert ad._is_locked({"lockoutTime": "0"}) is False


def test_lockout_duration_parsing():
    assert _lockout_duration("-18000000000") == timedelta(minutes=30)
    assert _lockout_duration("0") is FOREVER
    assert _lockout_duration("-9223372036854775808") is FOREVER
    assert _lockout_duration(timedelta(minutes=-30)) == timedelta(minutes=30)
