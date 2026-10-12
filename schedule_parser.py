"""Парсер Excel-файла расписания Политеха (один файл = один день).

Структура файла (по присланному образцу):
  B3  — дата (datetime), B4 — день недели
  дальше блоки по курсам: строка с названиями групп (ГЭС-26-1, РМ-25-2 ...),
  под ней нумерованные строки 1..10 (ячейка в колонке A объединена на 2 строки):
      нечётный номер: строка 1 — предмет, строка 2 — аудитория (или «с 12-40»)
      чётный номер:   строка 1 — преподаватель, строка 2 — второй преподаватель
  Пара N = номера (2N-1, 2N).
"""
from __future__ import annotations

import datetime as dt
import io
import re
from dataclasses import dataclass, field

import openpyxl

GROUP_RE = re.compile(r"^[А-ЯЁ]{1,6}-\d{2}(?:-\d{1,2})?$")
TIME_NOTE_RE = re.compile(r"^с\s*(\d{1,2})\s*[-:.]\s*(\d{2})$", re.I)
NUM_RE = re.compile(r"^\d{1,2}$")

WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
WEEKDAY_STEMS = ["понедельник", "вторник", "сред", "четверг", "пятниц", "суббот", "воскресенье"]

# латинские буквы, похожие на кириллические (в названиях групп встречаются опечатки)
_HOMOGLYPHS = str.maketrans("ABCEHKMOPTXY", "АВСЕНКМОРТХУ")


class ParseError(Exception):
    pass


@dataclass
class Lesson:
    pair: int
    subject: str | None = None
    room: str | None = None
    teachers: list[str] = field(default_factory=list)
    start_note: str | None = None  # "12:40" — занятия у группы начинаются с этого времени

    def to_dict(self) -> dict:
        return {
            "pair": self.pair,
            "subject": self.subject,
            "room": self.room,
            "teachers": self.teachers,
            "start_note": self.start_note,
        }


@dataclass
class ParsedDay:
    date: dt.date
    weekday: int  # 0 = понедельник
    groups: dict[str, list[Lesson]]


def clean(v) -> str | None:
    if v is None:
        return None
    s = str(v).replace("\xa0", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s or None


def norm_group(v) -> str | None:
    s = clean(v)
    if not s:
        return None
    s = s.upper().replace(" ", "").replace("–", "-").replace("—", "-").translate(_HOMOGLYPHS)
    return s if GROUP_RE.match(s) else None


def _detect_date(grid: list[list[str | None]], raw: list[tuple], today: dt.date | None) -> dt.date | None:
    for row in raw[:10]:
        for v in row:
            if isinstance(v, dt.datetime):
                return v.date()
            if isinstance(v, dt.date):
                return v
    month_names = {
        "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "мая": 5,
        "май": 5, "июн": 6, "июл": 7, "август": 8, "сентябр": 9,
        "октябр": 10, "ноябр": 11, "декабр": 12,
    }
    for row in grid[:12]:
        for s in row:
            if not s:
                continue
            m = re.search(r"(\\d{1,2})[.]([0-1]?\\d)[.](\\d{4})", s)
            if m:
                d, mo, y = map(int, m.groups())
                try:
                    return dt.date(y, mo, d)
                except ValueError:
                    pass
            # Файлы Луначарского содержат дату текстом: «12 октября 2026».
            m = re.search(r"(?i)(\\d{1,2})\\s+([а-яё]+)\\s+(\\d{4})", s)
            if m:
                d, month_word, y = m.groups()
                mo = next((num for stem, num in month_names.items() if month_word.lower().startswith(stem)), None)
                if mo:
                    try:
                        return dt.date(int(y), mo, int(d))
                    except ValueError:
                        pass
    return None


def _detect_weekday(grid: list[list[str | None]]) -> int | None:
    for row in grid[:10]:
        for s in row:
            if not s:
                continue
            low = s.lower()
            for i, stem in enumerate(WEEKDAY_STEMS):
                if low.startswith(stem):
                    return i
    return None


def parse_xlsx(data: bytes, today: dt.date | None = None) -> ParsedDay:
    try:
        if data.startswith(b"PK"):
            wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
            sheets = [list(ws.iter_rows(values_only=True)) for ws in wb.worksheets]
        else:
            import xlrd
            book = xlrd.open_workbook(file_contents=data)
            sheets = []
            for ws in book.sheets():
                rows = []
                for ri in range(ws.nrows):
                    vals = []
                    for ci in range(ws.ncols):
                        cell = ws.cell(ri, ci)
                        value = cell.value
                        if cell.ctype == xlrd.XL_CELL_DATE:
                            value = xlrd.xldate_as_datetime(value, book.datemode)
                        elif cell.ctype in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
                            value = None
                        vals.append(value)
                    rows.append(tuple(vals))
                sheets.append(rows)
    except Exception as e:
        raise ParseError(f"не удалось открыть Excel: {e}") from e

    best = None
    for raw in sheets:
        grid = [[clean(v) for v in row] for row in raw]
        headers = []
        for r, row in enumerate(grid):
            cols = {c: g for c, v in enumerate(row) if (g := norm_group(v))}
            if cols:
                headers.append((r, cols))
        score = sum(len(c) for _, c in headers)
        if best is None or score > best[0]:
            best = (score, raw, grid, headers)

    if not best or best[0] == 0:
        raise ParseError("в файле не найдены названия групп")
    _, raw, grid, headers = best

    groups: dict[str, dict[int, Lesson]] = {}

    for idx, (hr, cols) in enumerate(headers):
        end = headers[idx + 1][0] if idx + 1 < len(headers) else len(grid)
        for g in cols.values():
            groups.setdefault(g, {})

        # Два формата расписания:
        # Юбилейная: нумеруются строки 1,3,5... (предмет), затем аудитория;
        # строки 2,4,6... содержат преподавателей.
        # Луначарского: нумеруется каждая строка; нечётная строка содержит
        # аудиторию, следующая строка — предмет, чётная — преподавателя.
        numbered_rows = []
        for rr in range(hr + 1, end):
            a = grid[rr][0] if grid[rr] else None
            if a and NUM_RE.match(a):
                numbered_rows.append((rr, int(a)))
        # Определяем раскладку по содержимому, а не по названиям групп:
        # у Луначарского в строке с номером пары обычно аудитория (103/3, 28/1),
        # а предмет находится в следующей строке; у Юбилейной наоборот.
        room_pattern = re.compile(r"^(?:ауд\.?\s*\d|\d{1,3}(?:/\d{1,3})?|с\s*\d{1,2}[-:.]\d{2})", re.I)
        room_score = subject_score = 0
        for rr, nn in numbered_rows:
            if nn % 2 != 1:
                continue
            next_row = grid[rr + 1] if rr + 1 < end else []
            for c in cols:
                cur = grid[rr][c] if c < len(grid[rr]) else None
                nxt = next_row[c] if c < len(next_row) else None
                if cur and room_pattern.match(cur):
                    room_score += 1
                if nxt and room_pattern.match(nxt):
                    subject_score += 1
        lunacharskogo_layout = room_score > subject_score

        if lunacharskogo_layout:
            for r, n in numbered_rows:
                if n % 2 == 0:
                    continue
                pair = (n + 1) // 2
                room_row = grid[r]
                subject_row = grid[r + 1] if r + 1 < end else []
                teacher_row = grid[r + 3] if r + 3 < end else []
                for c, g in cols.items():
                    room = room_row[c] if c < len(room_row) else None
                    subject = subject_row[c] if c < len(subject_row) else None
                    teacher = teacher_row[c] if c < len(teacher_row) else None
                    lesson = groups[g].setdefault(pair, Lesson(pair=pair))
                    if subject:
                        lesson.subject = subject
                    if room and (m := TIME_NOTE_RE.match(room)):
                        lesson.start_note = f"{int(m.group(1))}:{m.group(2)}"
                    elif room:
                        lesson.room = room
                    if teacher:
                        lesson.teachers = [teacher]
        else:
            for r, n in numbered_rows:
                pair = (n + 1) // 2
                row1 = grid[r]
                row2 = grid[r + 1] if r + 1 < end else []
                row2_is_numbered = bool(row2 and row2[0] and NUM_RE.match(row2[0] or ""))
                if row2_is_numbered:
                    row2 = []
                for c, g in cols.items():
                    v1 = row1[c] if c < len(row1) else None
                    v2 = row2[c] if c < len(row2) else None
                    lesson = groups[g].setdefault(pair, Lesson(pair=pair))
                    if n % 2 == 1:
                        lesson.subject = v1
                        if v2 and (m := TIME_NOTE_RE.match(v2)):
                            lesson.start_note = f"{int(m.group(1))}:{m.group(2)}"
                        else:
                            lesson.room = v2
                    else:
                        lesson.teachers = [t for t in (v1, v2) if t]

    result: dict[str, list[Lesson]] = {}
    for g, pairs in groups.items():
        lessons = []
        for p in sorted(pairs):
            ls = pairs[p]
            if ls.subject or ls.room or ls.teachers or ls.start_note:
                lessons.append(ls)
        result[g] = lessons

    date = _detect_date(grid, raw, today)
    weekday = _detect_weekday(grid)
    if date is None:
        if weekday is None:
            raise ParseError("не найдена дата и день недели")
        base = today or dt.date.today()
        # ближайшая дата с таким днём недели в пределах ±3 дней
        date = min(
            (base + dt.timedelta(days=d) for d in range(-3, 4) if (base + dt.timedelta(days=d)).weekday() == weekday),
            key=lambda x: abs((x - base).days),
        )
    return ParsedDay(date=date, weekday=date.weekday(), groups=result)
