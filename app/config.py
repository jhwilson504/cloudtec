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
        )
