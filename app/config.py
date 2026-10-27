"""Runtime settings, read from environment variables."""

import os
from dataclasses import dataclass
from pathlib import Path


def _flag(name: str, default: bool) -> bool:
    value = os.environ.get(name)
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
            data_dir=Path(os.environ.get("CLOUDTEC_DATA_DIR", "data")),
            secret_key=os.environ.get("CLOUDTEC_SECRET_KEY") or None,
            admin_password=os.environ.get("CLOUDTEC_ADMIN_PASSWORD") or None,
            secure_cookies=_flag("CLOUDTEC_SECURE_COOKIES", False),
            session_hours=int(os.environ.get("CLOUDTEC_SESSION_HOURS", "8")),
            shell=os.environ.get("CLOUDTEC_SHELL", "local").strip().lower(),
            powershell=os.environ.get("CLOUDTEC_POWERSHELL") or None,
            smtp_host=os.environ.get("CLOUDTEC_SMTP_HOST") or None,
            smtp_port=int(os.environ.get("CLOUDTEC_SMTP_PORT", "25")),
            smtp_user=os.environ.get("CLOUDTEC_SMTP_USER") or None,
            smtp_password=os.environ.get("CLOUDTEC_SMTP_PASSWORD") or None,
            smtp_starttls=_flag("CLOUDTEC_SMTP_STARTTLS", False),
            alert_from=os.environ.get("CLOUDTEC_ALERT_FROM") or None,
            cert_check_hours=float(os.environ.get("CLOUDTEC_CERT_CHECK_HOURS", "12")),
        )
