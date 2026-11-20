from ..store import Connection
from .ad import ADConnector
from .base import ConnectorError
from .demo import DemoAD, DemoFortiGate, DemoIDRAC, DemoSolarWinds, DemoVNX, DemoVSphere
from .fortigate import FortiGateConnector
from .idrac import IDRACConnector
from .solarwinds import SolarWindsConnector
from .vnx import VNXConnector
from .vsphere import VSphereConnector

__all__ = ["ConnectorError", "open_connector"]


def open_connector(conn: Connection):
    """Return a context manager that yields a live connector for a saved connection."""
    if conn.kind == "ad":
        return DemoAD(conn.id, conn.config) if conn.demo else ADConnector(conn.config, conn.secret)
    if conn.kind == "vsphere":
        return DemoVSphere(conn.id, conn.config) if conn.demo else VSphereConnector(conn.config, conn.secret)
    if conn.kind == "idrac":
        return DemoIDRAC(conn.id, conn.config) if conn.demo else IDRACConnector(conn.config, conn.secret)
    if conn.kind == "solarwinds":
        return DemoSolarWinds(conn.id, conn.config) if conn.demo else SolarWindsConnector(conn.config, conn.secret)
    if conn.kind == "vnx":
        return DemoVNX(conn.id, conn.config) if conn.demo else VNXConnector(conn.config, conn.secret)
    if conn.kind == "fortigate":
        return DemoFortiGate(conn.id, conn.config) if conn.demo else FortiGateConnector(conn.config, conn.secret)
    raise ConnectorError(f"Unknown connection kind {conn.kind!r}")
