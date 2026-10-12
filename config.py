from __future__ import annotations

import os
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

DEFAULT_PAGE = (
    "https://politex59.ru/%d1%80%d0%b0%d1%81%d0%bf%d0%b8%d1%81%d0%b0%d0%bd%d0%b8%d0%b5-"
    "%d0%b7%d0%b0%d0%bd%d1%8f%d1%82%d0%b8%d0%b9-%d1%83%d0%bb-%d1%8e%d0%b1%d0%b8%d0%bb%d0%b5%d0%b9%d0%bd%d0%b0%d1%8f/"
)


@dataclass(frozen=True)
class Config:
    bot_token: str
    page_url: str
    db_path: str
    check_interval: int  # секунд между проверками сайта
    recheck_seconds: int  # как часто перекачивать файл, если ссылка не менялась
    tz: ZoneInfo
    admin_ids: tuple[int, ...]
    pages: dict[str, str] = field(default_factory=dict)


def load() -> Config:
    token = os.environ.get("BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("Не задан BOT_TOKEN (см. .env.example)")
    admins = tuple(int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if x)
    return Config(
        bot_token=token,
        page_url=os.environ.get("SCHEDULE_PAGE_URL", DEFAULT_PAGE),
        db_path=os.environ.get("DB_PATH", "bot.db"),
        check_interval=int(os.environ.get("CHECK_INTERVAL", "300")),
        recheck_seconds=int(os.environ.get("RECHECK_SECONDS", "3600")),
        tz=ZoneInfo(os.environ.get("TZ_NAME", "Asia/Yekaterinburg")),
        admin_ids=admins,
        pages={
            "Юбилейная": os.environ.get("SCHEDULE_PAGE_URL", DEFAULT_PAGE),
            "Луначарского": os.environ.get("SCHEDULE_PAGE_URL_LUNACHARSKOGO", "https://politex59.ru/studentam/raspisanie-zanyatiy/raspisanie-zanyatiy-ul-lunacharskogo/"),
        },
    )
