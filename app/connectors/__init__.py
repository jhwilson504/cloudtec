from ..store import Connection
from .ad import ADConnector
from .base import ConnectorError
from .demo import DemoAD, DemoVSphere
from .vsphere import VSphereConnector

__all__ = ["ConnectorError", "open_connector"]


def open_connector(conn: Connection):
    """Return a context manager that yields a live connector for a saved connection."""
    if conn.kind == "ad":
        return DemoAD(conn.id, conn.config) if conn.demo else ADConnector(conn.config, conn.secret)
    if conn.kind == "vsphere":
        return DemoVSphere(conn.id, conn.config) if conn.demo else VSphereConnector(conn.config, conn.secret)
    raise ConnectorError(f"Unknown connection kind {conn.kind!r}")
