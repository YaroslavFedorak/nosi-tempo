"""Settings read from the environment (or a .env file next to the project)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


def _ids(raw: str) -> frozenset[int]:
    return frozenset(int(x) for x in raw.replace(";", ",").split(",") if x.strip())


@dataclass(frozen=True)
class Settings:
    bot_token: str
    owner_ids: frozenset[int]
    tz: ZoneInfo
    db_path: Path
    webapp_url: str
    web_host: str
    web_port: int
    dev_user_id: int | None

    anthropic_key: str
    anthropic_model: str
    openai_key: str
    openai_model: str
    openai_base_url: str
    gemini_key: str
    gemini_model: str
    ai_default: str

    @property
    def tz_label(self) -> str:
        return self.tz.key.split("/")[-1].replace("_", " ")

    def allowed(self, user_id: int) -> bool:
        return not self.owner_ids or user_id in self.owner_ids

    def ai_providers(self) -> list[str]:
        keys = {
            "gemini": self.gemini_key,
            "claude": self.anthropic_key,
            "openai": self.openai_key,
        }
        return [name for name, key in keys.items() if key]


def load_settings(require_token: bool = True) -> Settings:
    load_dotenv(ROOT / ".env")
    env = os.environ.get

    token = env("BOT_TOKEN", "").strip()
    if not token and require_token:
        raise SystemExit("BOT_TOKEN is empty. Copy .env.example to .env and fill it in.")

    db_path = Path(env("DB_PATH", "") or ROOT / "data" / "tempo.db")
    dev_user = env("DEV_USER_ID", "").strip()

    return Settings(
        bot_token=token,
        owner_ids=_ids(env("OWNER_IDS", "")),
        tz=ZoneInfo(env("TZ_NAME", "Europe/Kyiv")),
        db_path=db_path,
        webapp_url=env("WEBAPP_URL", "").strip().rstrip("/"),
        web_host=env("WEB_HOST", "0.0.0.0"),
        web_port=int(env("PORT", env("WEB_PORT", "8080"))),
        dev_user_id=int(dev_user) if dev_user else None,
        anthropic_key=env("ANTHROPIC_API_KEY", "").strip(),
        anthropic_model=env("ANTHROPIC_MODEL", "claude-haiku-5-5"),
        openai_key=env("OPENAI_API_KEY", "").strip(),
        openai_model=env("OPENAI_MODEL", "gpt-5-mini"),
        openai_base_url=env("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
        gemini_key=env("GEMINI_API_KEY", "").strip(),
        gemini_model=env("GEMINI_MODEL", "gemini-flash-latest"),
        ai_default=env("AI_DEFAULT", "gemini"),
    )
