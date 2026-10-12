"""Фоновая проверка сайта: нашли новые файлы -> распарсили -> сохранили -> разослали."""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import logging
import time
from typing import Awaitable, Callable

from config import Config
from fetcher import FetchError, download_xlsx, fetch_day_links, new_session
from render import render_day
from schedule_parser import ParseError, parse_xlsx
from storage import Storage

log = logging.getLogger(__name__)

# send(chat_id, text) -> None; реализация живёт в bot.py (aiogram)
SendFn = Callable[[int, str], Awaitable[None]]
AdminFn = Callable[[str], Awaitable[None]]


class Watcher:
    def __init__(self, cfg: Config, storage: Storage, send: SendFn, notify_admin: AdminFn):
        self.cfg = cfg
        self.storage = storage
        self.send = send
        self.notify_admin = notify_admin
        self.lock = asyncio.Lock()
        self.last_ok: float | None = None
        self.last_error: str | None = None
        self._last_admin_alert = 0.0

    def today(self) -> dt.date:
        return dt.datetime.now(self.cfg.tz).date()

    async def _alert(self, text: str) -> None:
        # не чаще раза в 6 часов, чтобы не спамить админу
        if time.time() - self._last_admin_alert < 6 * 3600:
            return
        self._last_admin_alert = time.time()
        try:
            await self.notify_admin(text)
        except Exception:
            log.exception("не удалось отправить алерт админу")

    async def check_once(self, force: bool = False) -> str:
        async with self.lock:
            return await self._check(force)

    async def _check(self, force: bool) -> str:
        silent = not self.storage.has_any_schedule()  # самый первый запуск: ничего не рассылаем
        today = self.today()
        report: list[str] = []
        errors: list[str] = []

        async with new_session() as session:
            pages = self.cfg.pages or {"Юбилейная": self.cfg.page_url}
            for location_idx, (location, page_url) in enumerate(pages.items()):
                try:
                    links = await fetch_day_links(session, page_url)
                except Exception as e:
                    msg = f"Не удалось получить страницу расписания ({location}): {e}"
                    log.error(msg)
                    errors.append(msg)
                    continue

                for idx in sorted(links):
                    url = links[idx]
                    source_idx = location_idx * 1000 + idx
                    src = self.storage.get_source(source_idx)
                    due = (force or src is None or src.url != url or
                           time.time() - src.checked_at > self.cfg.recheck_seconds)
                    if not due:
                        continue
                    try:
                        data = await download_xlsx(session, url)
                        h = hashlib.sha256(data).hexdigest()
                        # При /refresh принудительно перепарсиваем даже неизменившийся файл:
                        # это нужно после обновления парсера, иначе в БД останутся старые пустые данные.
                        if src and src.hash == h and not force:
                            self.storage.set_source(source_idx, url, h, time.time())
                            continue
                        day = await asyncio.to_thread(parse_xlsx, data, today)
                    except (FetchError, ParseError) as e:
                        errors.append(f"{location}, день {idx + 1}: {e}")
                        log.error("%s день %s (%s): %s", location, idx, url, e)
                        continue
                    except Exception as e:
                        errors.append(f"{location}, день {idx + 1}: {e!r}")
                        log.exception("%s день %s: непредвиденная ошибка", location, idx)
                        continue

                    # Внутренний ключ включает адрес корпуса: одинаковые названия групп не смешиваются.
                    day.groups = {f"{location}::{g}": lessons for g, lessons in day.groups.items()}
                    n = await self._apply(day, silent)
                    self.storage.set_source(source_idx, url, h, time.time())
                    report.append(f"{location}, {day.date:%d.%m} обновлено, уведомлений: {n}")
                    await asyncio.sleep(1)

        self.storage.purge_before(today - dt.timedelta(days=30))

        if errors:
            self.last_error = "; ".join(errors)
            await self._alert("⚠️ Ошибки при обновлении расписания:\n" + "\n".join(errors))
        else:
            self.last_error = None
            self.last_ok = time.time()
        return "\n".join(report + errors) or "Изменений нет."

    async def _apply(self, day, silent: bool) -> int:
        """Сохраняет день и рассылает подписчикам только затронутых групп. Возвращает число отправок."""
        new = {g: [ls.to_dict() for ls in lessons] for g, lessons in day.groups.items()}
        old_raw = self.storage.get_day_raw(day.date)
        self.storage.save_day(day.date, new)

        if silent or day.date < self.today():
            return 0

        sent = 0
        for g, lessons in new.items():
            old_json = old_raw.get(g)
            new_json = json.dumps(lessons, ensure_ascii=False, sort_keys=True)
            if old_json == new_json:
                continue
            old_lessons = json.loads(old_json) if old_json else []
            if not lessons and not old_lessons:
                continue  # новый день, а у группы всё равно пусто — не беспокоим
            title = "🔔 <b>Новое расписание</b>" if old_json is None else "🔄 <b>Расписание изменено</b>"
            location, visible_group = g.split("::", 1) if "::" in g else ("", g)
            text = title + "\n\n" + (f"🏫 <b>{location}</b>\n" if location else "") + render_day(day.date, visible_group, lessons)
            for chat_id in self.storage.subscribers(g):
                try:
                    await self.send(chat_id, text)
                    sent += 1
                except Exception:
                    log.exception("не удалось отправить в %s", chat_id)
                await asyncio.sleep(0.05)  # лимит Telegram ~30 сообщений/сек
        return sent

    async def run_forever(self) -> None:
        while True:
            try:
                await self.check_once()
            except Exception:
                log.exception("сбой цикла проверки")
            await asyncio.sleep(self.cfg.check_interval)
