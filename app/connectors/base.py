class ConnectorError(Exception):
    """Raised when a backend (AD or vCenter) rejects or fails a request.

    The message is shown to the operator, so it should say what went wrong
    without echoing credentials.
    """
