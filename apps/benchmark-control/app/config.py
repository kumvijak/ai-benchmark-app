from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    app_secret: str
    db_path: Path
    artifact_dir: Path
    local_user_email: str


def get_settings() -> Settings:
    secret = os.getenv("APP_SECRET")
    if not secret or len(secret) < 24:
        raise RuntimeError("APP_SECRET must be set to at least 24 characters")
    local_user_email = os.getenv("APP_LOCAL_USER_EMAIL")
    if not local_user_email:
        raise RuntimeError("APP_LOCAL_USER_EMAIL must be set")
    db_path = Path(os.getenv("APP_DB_PATH", "/data/app.db"))
    artifact_dir = Path(os.getenv("APP_ARTIFACT_DIR", "/data/artifacts"))
    artifact_dir.mkdir(parents=True, exist_ok=True)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return Settings(app_secret=secret, db_path=db_path, artifact_dir=artifact_dir, local_user_email=local_user_email)
