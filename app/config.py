"""Runtime settings, read from environment variables.

Each setting is KASTEC_<NAME>; the older CLOUDTEC_<NAME> spelling still works.
"""

import os
from dataclasses import dataclass
from pathlib import Path


def env(name: str, default: str | None = None) -> str | None:
    """KASTEC_<name>, or the older CLOUDTEC_<name>, or the default."""
    for prefix in ("KASTEC_", "CLOUDTEC_"):
        value = os.environ.get(prefix + name)
        if value is not None:
            return value
    return default


def _flag(name: str, default: bool) -> bool:
    value = env(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    secret_key: str | None
    admin_password: str | None
    secure_cookies: bool
    session_hours: int
    shell: str = "local"
    powershell: str | None = None
    smtp_host: str | None = None
    smtp_port: int = 25
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_starttls: bool = False
    alert_from: str | None = None
    cert_check_hours: float = 12

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            data_dir=Path(env("DATA_DIR", "data")),
            secret_key=env("SECRET_KEY") or None,
            admin_password=env("ADMIN_PASSWORD") or None,
            secure_cookies=_flag("SECURE_COOKIES", False),
            session_hours=int(env("SESSION_HOURS", "8")),
            shell=env("SHELL", "local").strip().lower(),
            powershell=env("POWERSHELL") or None,
            smtp_host=env("SMTP_HOST") or None,
            smtp_port=int(env("SMTP_PORT", "25")),
            smtp_user=env("SMTP_USER") or None,
            smtp_password=env("SMTP_PASSWORD") or None,
            smtp_starttls=_flag("SMTP_STARTTLS", False),
            alert_from=env("ALERT_FROM") or None,
            cert_check_hours=float(env("CERT_CHECK_HOURS", "12")),
        )
