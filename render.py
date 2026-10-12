"""Форматирование расписания группы в HTML-текст для Telegram."""
from __future__ import annotations

import datetime as dt
import html

WEEKDAY_NAMES = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
WEEKDAY_SHORT = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def fmt_date(d: dt.date) -> str:
    return f"{WEEKDAY_NAMES[d.weekday()]}, {d:%d.%m.%Y}"


def short_date(d: dt.date) -> str:
    return f"{WEEKDAY_SHORT[d.weekday()]} {d:%d.%m}"


def render_day(day: dt.date, group: str, lessons: list[dict] | None) -> str:
    """lessons=None — группы нет в файле на этот день; [] — занятий нет."""
    e = html.escape
    head = f"📅 <b>{e(fmt_date(day))}</b>\n👥 Группа <b>{e(group)}</b>\n"
    if lessons is None:
        return head + "\nВ файле расписания на этот день такой группы нет."

    notes = []
    for ls in lessons:
        if ls.get("start_note") and ls["start_note"] not in notes:
            notes.append(ls["start_note"])

    body = []
    for ls in lessons:
        subject = ls.get("subject")
        room = ls.get("room")
        teachers = ls.get("teachers") or []
        if not (subject or room or teachers):
            continue  # пустая пара, была только пометка о времени начала
        title = f"<b>{ls['pair']} пара</b> — {e(subject) if subject else '—'}"
        lines = [title]
        if room:
            lines.append(f"🚪 {e(room)}")
        if teachers:
            sep = " / " if subject and "/" in subject else ", "
            lines.append(f"👨‍🏫 {e(sep.join(teachers))}")
        body.append("\n".join(lines))

    out = head
    if notes:
        out += "\n⏰ Занятия начинаются с " + ", ".join(notes)
        out += "\n"
    if not body:
        return out + "\n🎉 Занятий нет."
    return out + "\n" + "\n\n".join(body)
