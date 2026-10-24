"""Active Directory over LDAP(S), using ldap3.

Config keys:
    host        domain controller hostname or IP
    port        defaults to 636 with SSL, 389 without
    use_ssl     LDAPS (required for password resets); default true
    verify_ssl  verify the DC certificate; default true
    base_dn     e.g. DC=corp,DC=example,DC=com
    bind_user   UPN (admin@corp.example.com) or full DN
The connection secret is the bind password.
"""

import ssl
from datetime import datetime, timedelta, timezone

from ldap3 import BASE, MODIFY_REPLACE, NONE, SUBTREE, Connection, Server, Tls
from ldap3.core.exceptions import LDAPException
from ldap3.utils.conv import escape_filter_chars

from .base import ConnectorError

UAC_DISABLED = 0x2
UF_LOCKOUT = 0x10  # in msDS-User-Account-Control-Computed: locked right now
COMPUTED_UAC = "msDS-User-Account-Control-Computed"
FOREVER = timedelta.max
FILETIME_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)
SUMMARY_CAP = 5000

USER_FILTER = "(&(objectCategory=person)(objectClass=user))"
GROUP_FILTER = "(objectClass=group)"
COMPUTER_FILTER = "(objectClass=computer)"

USER_ATTRS = [
    "sAMAccountName", "displayName", "mail", "userAccountControl", "lockoutTime", COMPUTED_UAC,
    "lastLogonTimestamp", "whenCreated", "title", "department",
]
GROUP_ATTRS = ["cn", "description", "groupType", "member"]
COMPUTER_ATTRS = [
    "cn", "dNSHostName", "operatingSystem", "operatingSystemVersion",
    "userAccountControl", "lastLogonTimestamp",
]


def _first(attrs: dict, key: str):
    value = attrs.get(key)
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _filetime_dt(value) -> datetime | None:
    """AD stores timestamps as 100ns ticks since 1601 (or a datetime once decoded)."""
    if isinstance(value, datetime):
        return None if value.year <= 1601 else value
    ticks = _int(value)
    if ticks <= 0 or ticks >= 0x7FFFFFFFFFFFFFFF:
        return None
    return FILETIME_EPOCH + timedelta(microseconds=ticks // 10)


def _filetime(value) -> str | None:
    dt = _filetime_dt(value)
    return dt.isoformat() if dt else None


def _lockout_duration(value) -> timedelta:
    """The domain's lockoutDuration: negative 100ns ticks; 0 or the minimum value means
    accounts stay locked until an admin unlocks them."""
    if isinstance(value, timedelta):
        td = abs(value)
        return FOREVER if td == timedelta(0) or td > timedelta(days=36500) else td
    ticks = abs(_int(value))
    if ticks == 0 or ticks >= 0x7FFFFFFFFFFFFFFF:
        return FOREVER
    return timedelta(microseconds=ticks // 10)


def _generalized_time(value) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    if not value:
        return None
    try:
        return datetime.strptime(str(value)[:14], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc).isoformat()
    except ValueError:
        return str(value)


def _contains(attrs: list[str], q: str) -> str:
    if not q:
        return ""
    term = escape_filter_chars(q)
    return "(|" + "".join(f"({a}=*{term}*)" for a in attrs) + ")"


class ADConnector:
    def __init__(self, config: dict, secret: str):
        self.host = config.get("host", "").strip()
        self.use_ssl = config.get("use_ssl", True)
        self.port = int(config.get("port") or (636 if self.use_ssl else 389))
        self.verify_ssl = config.get("verify_ssl", True)
        self.base_dn = config.get("base_dn", "").strip()
        self.bind_user = config.get("bind_user", "").strip()
        self.password = secret
        self._conn: Connection | None = None
        self._lockout_window: timedelta | None = None
        if not (self.host and self.base_dn and self.bind_user):
            raise ConnectorError("host, base_dn and bind_user are required")

    def __enter__(self) -> "ADConnector":
        tls = Tls(validate=ssl.CERT_REQUIRED if self.verify_ssl else ssl.CERT_NONE)
        server = Server(self.host, port=self.port, use_ssl=self.use_ssl, tls=tls,
                        get_info=NONE, connect_timeout=10)
        try:
            self._conn = Connection(server, user=self.bind_user, password=self.password,
                                    auto_bind=True, receive_timeout=30, raise_exceptions=True)
        except LDAPException as e:
            raise ConnectorError(f"Could not bind to {self.host}:{self.port}: {e}") from e
        return self

    def __exit__(self, *exc) -> None:
        if self._conn:
            try:
                self._conn.unbind()
            except LDAPException:
                pass

    # --- helpers -----------------------------------------------------------

    def _search(self, filt: str, attrs: list[str], limit: int) -> list[dict]:
        try:
            results = self._conn.extend.standard.paged_search(
                self.base_dn, filt, SUBTREE, attributes=attrs, paged_size=500, generator=True
            )
            out = []
            for item in results:
                if item.get("type") != "searchResEntry":
                    continue
                out.append({"dn": item["dn"], **item["attributes"]})
                if len(out) >= limit:
                    break
            return out
        except LDAPException as e:
            raise ConnectorError(f"LDAP search failed: {e}") from e

    def _domain_lockout_window(self) -> timedelta:
        """How long a lockout lasts, from the domain's password policy (read once per connection)."""
        if self._lockout_window is None:
            domain_dn = ",".join(p for p in self.base_dn.split(",") if p.strip().lower().startswith("dc="))
            try:
                self._conn.search(domain_dn, "(objectClass=*)", search_scope=BASE, attributes=["lockoutDuration"])
                entries = [r for r in self._conn.response if r.get("type") == "searchResEntry"]
                value = _first(entries[0]["attributes"], "lockoutDuration") if entries else None
            except LDAPException:
                value = None
            # Unknown policy: AD's default is 10 minutes (Windows Server 2016 and later).
            self._lockout_window = _lockout_duration(value) if value is not None else timedelta(minutes=10)
        return self._lockout_window

    def _is_locked(self, attrs: dict) -> bool:
        computed = _first(attrs, COMPUTED_UAC)
        if computed not in (None, ""):
            return bool(_int(computed) & UF_LOCKOUT)
        # Fallback: lockoutTime stays set after a lockout expires on its own, so check its age.
        locked_at = _filetime_dt(_first(attrs, "lockoutTime"))
        if locked_at is None:
            return False
        window = self._domain_lockout_window()
        return window is FOREVER or datetime.now(timezone.utc) - locked_at < window

    def _require_dn(self, dn: str) -> None:
        if not dn.lower().endswith(self.base_dn.lower()):
            raise ConnectorError("Object is outside the configured base DN")

    # --- reads -------------------------------------------------------------

    def test(self) -> dict:
        who = self._conn.extend.standard.who_am_i()
        return {"ok": True, "message": f"Bound as {who or self.bind_user}"}

    def summary(self) -> dict:
        users = self._search(USER_FILTER, ["userAccountControl", "lockoutTime", COMPUTED_UAC], SUMMARY_CAP)
        computers = self._search(COMPUTER_FILTER, ["cn"], SUMMARY_CAP)
        groups = self._search(GROUP_FILTER, ["cn"], SUMMARY_CAP)
        return {
            "users": len(users),
            "disabled": sum(1 for u in users if _int(_first(u, "userAccountControl")) & UAC_DISABLED),
            "locked": sum(1 for u in users if self._is_locked(u)),
            "computers": len(computers),
            "groups": len(groups),
            "capped": any(len(x) >= SUMMARY_CAP for x in (users, computers, groups)),
        }

    def users(self, q: str = "", limit: int = 500) -> list[dict]:
        filt = f"(&{USER_FILTER}{_contains(['sAMAccountName', 'displayName', 'mail'], q)})"
        out = []
        for u in self._search(filt, USER_ATTRS, limit):
            uac = _int(_first(u, "userAccountControl"))
            out.append({
                "dn": u["dn"],
                "username": _first(u, "sAMAccountName"),
                "display_name": _first(u, "displayName"),
                "email": _first(u, "mail"),
                "title": _first(u, "title"),
                "department": _first(u, "department"),
                "enabled": not (uac & UAC_DISABLED),
                "locked": self._is_locked(u),
                "last_logon": _filetime(_first(u, "lastLogonTimestamp")),
                "created": _generalized_time(_first(u, "whenCreated")),
            })
        return out

    def groups(self, q: str = "", limit: int = 500) -> list[dict]:
        filt = f"(&{GROUP_FILTER}{_contains(['cn', 'description'], q)})"
        out = []
        for g in self._search(filt, GROUP_ATTRS, limit):
            members = g.get("member") or []
            gtype = _int(_first(g, "groupType"))
            out.append({
                "dn": g["dn"],
                "name": _first(g, "cn"),
                "description": _first(g, "description"),
                "members": len(members) if isinstance(members, list) else 1,
                "type": "Security" if gtype & 0x80000000 else "Distribution",
            })
        return out

    def computers(self, q: str = "", limit: int = 2000) -> list[dict]:
        filt = f"(&{COMPUTER_FILTER}{_contains(['cn', 'dNSHostName', 'operatingSystem'], q)})"
        out = []
        for c in self._search(filt, COMPUTER_ATTRS, limit):
            out.append({
                "dn": c["dn"],
                "name": _first(c, "cn"),
                "dns_name": _first(c, "dNSHostName"),
                "os": _first(c, "operatingSystem"),
                "os_version": _first(c, "operatingSystemVersion"),
                "enabled": not (_int(_first(c, "userAccountControl")) & UAC_DISABLED),
                "last_logon": _filetime(_first(c, "lastLogonTimestamp")),
            })
        return out

    # --- actions -----------------------------------------------------------

    def set_enabled(self, dn: str, enabled: bool) -> None:
        self._require_dn(dn)
        try:
            self._conn.search(dn, "(objectClass=*)", search_scope=BASE, attributes=["userAccountControl"])
            entries = [r for r in self._conn.response if r.get("type") == "searchResEntry"]
            if not entries:
                raise ConnectorError("Object not found")
            uac = _int(_first(entries[0]["attributes"], "userAccountControl"))
            uac = (uac & ~UAC_DISABLED) if enabled else (uac | UAC_DISABLED)
            self._conn.modify(dn, {"userAccountControl": [(MODIFY_REPLACE, [str(uac)])]})
        except LDAPException as e:
            raise ConnectorError(f"Could not update account: {e}") from e

    def unlock(self, dn: str) -> None:
        self._require_dn(dn)
        try:
            self._conn.extend.microsoft.unlock_account(dn)
        except LDAPException as e:
            raise ConnectorError(f"Could not unlock account: {e}") from e

    def reset_password(self, dn: str, new_password: str, unlock: bool = False, must_change: bool = False) -> None:
        self._require_dn(dn)
        if not self.use_ssl:
            raise ConnectorError("Active Directory only accepts password resets over LDAPS (use_ssl)")
        try:
            self._conn.extend.microsoft.modify_password(dn, new_password)
        except LDAPException as e:
            raise ConnectorError(f"Password reset rejected: {e}") from e
        try:
            if must_change:
                # pwdLastSet = 0 makes Windows ask for a new password at the next sign-in.
                self._conn.modify(dn, {"pwdLastSet": [(MODIFY_REPLACE, ["0"])]})
            if unlock:
                self._conn.extend.microsoft.unlock_account(dn)
        except LDAPException as e:
            raise ConnectorError(f"The password was reset, but a follow-up step failed: {e}") from e
