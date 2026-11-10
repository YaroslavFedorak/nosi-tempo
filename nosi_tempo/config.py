from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    bot_token: str
    tz: ZoneInfo
    db_path: Path
    secret_key: bytes

    anthropic_model: str
    openai_model: str
    openai_base_url: str
    gemini_model: str


def _secret_key(db_path: Path) -> bytes:
    # ключем шифруються API-ключі користувачів; якщо його не задано, створюємо один раз поруч із базою
    raw = os.environ.get("SECRET_KEY", "").strip()
    if raw:
        return raw.encode()
    path = db_path.parent / "secret.key"
    if path.exists():
        return path.read_bytes().strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    path.write_bytes(key)
    return key


def load_settings() -> Settings:
    load_dotenv(ROOT / ".env", override=True)
    env = os.environ.get

    token = env("BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("BOT_TOKEN порожній. Скопіюй .env.example у .env і заповни його.")

    db_path = Path(env("DB_PATH", "") or ROOT / "data" / "tempo.db")

    return Settings(
        bot_token=token,
        tz=ZoneInfo(env("TZ_NAME", "Europe/Kyiv")),
        db_path=db_path,
        secret_key=_secret_key(db_path),
        anthropic_model=env("ANTHROPIC_MODEL", "claude-haiku-5-5"),
        openai_model=env("OPENAI_MODEL", "gpt-5-mini"),
        openai_base_url=env("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
        gemini_model=env("GEMINI_MODEL", "gemini-flash-latest,gemini-3-flash-preview,gemini-flash-lite-latest"),
    )
