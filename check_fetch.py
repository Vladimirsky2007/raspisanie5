"""Диагностика сетевой части (запускать у себя / на сервере, где есть доступ в интернет):

    python check_fetch.py

Показывает, какие ссылки бот находит на странице, удаётся ли скачать каждый файл
с Облака Mail.ru и что из него парсится.
"""
import asyncio
import datetime as dt
import os

import config
from fetcher import download_xlsx, fetch_day_links, new_session
from render import WEEKDAY_NAMES
from schedule_parser import parse_xlsx


async def main() -> None:
    page = os.environ.get("SCHEDULE_PAGE_URL", config.DEFAULT_PAGE)
    async with new_session() as s:
        links = await fetch_day_links(s, page)
        print(f"Найдено ссылок: {len(links)}")
        for idx in sorted(links):
            print(f"\n{WEEKDAY_NAMES[idx]}: {links[idx]}")
            try:
                data = await download_xlsx(s, links[idx])
                day = parse_xlsx(data, dt.date.today())
                print(f"  ✅ скачано {len(data)} байт, дата {day.date:%d.%m.%Y}, групп: {len(day.groups)}")
            except Exception as e:
                print(f"  ❌ {type(e).__name__}: {e}")
            await asyncio.sleep(1)


if __name__ == "__main__":
    asyncio.run(main())
