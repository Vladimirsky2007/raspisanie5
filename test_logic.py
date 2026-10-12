"""Проверки без сети и без aiogram: python test_logic.py"""
import asyncio
import datetime as dt
import sys
import tempfile
import types
from pathlib import Path

# aiohttp нужен только fetcher'у; для проверки логики подставляем заглушку, если пакета нет
try:
    import aiohttp  # noqa: F401
except ImportError:
    stub = types.ModuleType("aiohttp")
    stub.ClientSession = object
    stub.ClientSSLError = type("ClientSSLError", (Exception,), {})
    stub.ClientTimeout = lambda **kw: None
    sys.modules["aiohttp"] = stub

from render import render_day
from schedule_parser import parse_xlsx
from storage import Storage, course_of

SAMPLE = Path(__file__).parent / "samples" / "monday.xlsx"


def test_parser():
    day = parse_xlsx(SAMPLE.read_bytes(), today=dt.date(2026, 10, 12))
    assert day.date == dt.date(2026, 10, 12) and day.weekday == 0
    names = list(day.groups)
    assert len(names) == 27, names
    for g in ["ГЭС-26-1", "РМ-26-2", "СГ-25", "СД-25", "СД-24", "АТ-23", "РМ-23-1"]:
        assert g in day.groups, g

    # РМ-26-1: пара 1 = «Химия/Ин.язык», две аудитории и два преподавателя (подгруппы)
    rm = {ls.pair: ls for ls in day.groups["РМ-26-1"]}
    assert rm[1].subject == "Химия/Ин.язык"
    assert rm[1].room == "Ауд.107 (I)/Ауд.403"
    assert rm[1].teachers == ["Султанова Н.В.", "Поплаухина Е.В."]
    assert rm[2].subject == "Литература" and rm[2].room == "Ауд.208"
    assert rm[3].subject == "История" and rm[3].teachers == ["Гайнутдинова Л.Ш."]
    assert 4 not in rm and 5 not in rm  # пустые пары не попадают в список

    # ГЭС-26-1: первой пары нет, занятия с 12:40
    g1 = {ls.pair: ls for ls in day.groups["ГЭС-26-1"]}
    assert g1[1].start_note == "12:40" and g1[1].subject is None
    assert g1[3].subject == "Материаловедение" and g1[3].room == "Ауд.309"
    assert g1[4].subject == "Основы инж.графики"

    # блок III курса (расположен справа от «II курс» в той же строке-заголовке)
    sd24 = {ls.pair: ls for ls in day.groups["СД-24"]}
    assert sd24[1].subject == "ИТ в прф.деят-ти" and sd24[1].room == "ДО (ПЗ)"
    # IV курс
    at23 = {ls.pair: ls for ls in day.groups["АТ-23"]}
    assert at23[1].subject == "Физическая культура" and at23[1].room == "Сп.зал"
    # группа без занятий вообще не должна пропасть
    assert "СД-25" in day.groups



def test_lunacharskogo_layout_sample():
    sample = Path(__file__).parent / "samples" / "lunacharskogo_12_october.xlsx"
    day = parse_xlsx(sample.read_bytes(), today=dt.date(2026, 10, 12))
    assert day.date == dt.date(2026, 10, 12)
    assert len(day.groups) >= 20
    sd = {ls.pair: ls for ls in day.groups["СД-26"]}
    assert sd[1].subject == "биология" and sd[1].room == "103/3"
    assert sd[1].teachers == ["Маланицева А.В."]


def test_both_uploaded_workbooks():
    luna = parse_xlsx((Path(__file__).parent / "samples" / "lunacharskogo_12_october.xlsx").read_bytes(),
                      today=dt.date(2026, 10, 12))
    jub = parse_xlsx((Path(__file__).parent / "samples" / "yubileynaya_12_october.xlsx").read_bytes(),
                     today=dt.date(2026, 10, 12))
    assert luna.date == jub.date == dt.date(2026, 10, 12)
    assert {ls.pair: ls for ls in luna.groups["СД-26"]}[1].subject == "биология"
    assert {ls.pair: ls for ls in jub.groups["РМ-26-1"]}[1].subject == "Химия/Ин.язык"

def test_render():
    day = parse_xlsx(SAMPLE.read_bytes(), today=dt.date(2026, 10, 12))
    text = render_day(day.date, "ГЭС-26-1", [ls.to_dict() for ls in day.groups["ГЭС-26-1"]])
    assert "Понедельник, 12.10.2026" in text and "с 12:40" in text and "3 пара" in text
    assert "РМ-26-2" not in text and "Литература" not in text  # только своя группа
    print(text, "\n---")
    rm = render_day(day.date, "РМ-26-1", [ls.to_dict() for ls in day.groups["РМ-26-1"]])
    assert "Султанова Н.В. / Поплаухина Е.В." in rm
    print(rm, "\n---")
    assert "нет" in render_day(day.date, "ХХ-26", None)
    assert "Занятий нет" in render_day(day.date, "ХХ-26", [])



def test_save_two_campuses_same_date():
    db = Storage(tempfile.mktemp(suffix=".db"))
    d = dt.date(2026, 10, 12)
    db.save_day(d, {"Юбилейная::ГЭС-26-1": [{"pair": 1, "subject": "Химия"}]})
    db.save_day(d, {"Луначарского::СД-26": [{"pair": 1, "subject": "биология"}]})
    assert db.get_group_day(d, "Юбилейная::ГЭС-26-1") == [{"pair": 1, "subject": "Химия"}]
    assert db.get_group_day(d, "Луначарского::СД-26") == [{"pair": 1, "subject": "биология"}]



def test_game_wallet():
    db = Storage(tempfile.mktemp(suffix=".db"))
    assert db.game_balance(101, "Тестовый игрок") == 1000
    ok, balance, awarded = db.game_bonus(101, "Тестовый игрок", "2026-10-12", 777)
    assert ok and balance == 1777 and awarded == 777
    ok, balance, awarded = db.game_bonus(101, "Тестовый игрок", "2026-10-12", 200)
    assert not ok and balance == 1777 and awarded == 0
    ok, balance = db.game_play(101, "Тестовый игрок", 50, 200)
    assert ok and balance == 1927
    ok, balance = db.game_play(101, "Тестовый игрок", 5000, 0)
    assert not ok and balance == 1927
    assert db.game_top(20)[0]["user_id"] == 101
    db.game_player(101, "Тестовый игрок", "test_user")
    assert db.game_top(20)[0]["username"] == "test_user"

def test_course():
    t = dt.date(2026, 10, 12)
    assert course_of("ГЭС-26-1", t) == 1 and course_of("СД-24", t) == 3 and course_of("АТ-23", t) == 4
    assert course_of("ГЭС-26-1", dt.date(2027, 3, 1)) == 1  # до сентября учебный год прежний
    assert course_of("ГЭС-26-1", dt.date(2027, 9, 2)) == 2


def test_watcher_notifications():
    from config import Config
    from watcher import Watcher
    from zoneinfo import ZoneInfo

    db = Storage(tempfile.mktemp(suffix=".db"))
    cfg = Config("x", "http://x", ":memory:", 300, 3600, ZoneInfo("Asia/Yekaterinburg"), ())
    sent: list[tuple[int, str]] = []

    async def send(chat_id, text):
        sent.append((chat_id, text))

    async def admin(text):
        pass

    w = Watcher(cfg, db, send, admin)
    w.today = lambda: dt.date(2026, 10, 12)

    db.set_group(1, "РМ-26-1")
    db.set_group(2, "РМ-26-2")
    db.set_group(3, "РМ-26-1")
    db.toggle_notify(3)  # у третьего уведомления выключены

    day = parse_xlsx(SAMPLE.read_bytes(), today=dt.date(2026, 10, 12))

    async def run():
        # первый запуск — тишина
        assert await w._apply(day, silent=True) == 0
        assert sent == []
        # тот же файл ещё раз — ничего не изменилось
        assert await w._apply(day, silent=False) == 0
        # у РМ-26-1 меняется кабинет второй пары
        for ls in day.groups["РМ-26-1"]:
            if ls.pair == 2:
                ls.room = "Ауд.999"
        n = await w._apply(day, silent=False)
        assert n == 1, n  # только чат 1: чат 2 — другая группа, чат 3 — выключены уведомления
        assert sent[0][0] == 1 and "Ауд.999" in sent[0][1] and "изменено" in sent[0][1]
        # прошедшая дата не рассылается
        w.today = lambda: dt.date(2026, 10, 20)
        day.groups["РМ-26-1"][0].room = "Ауд.1"
        assert await w._apply(day, silent=False) == 0

    asyncio.run(run())
    assert db.get_group_day(dt.date(2026, 10, 12), "РМ-26-1") is not None
    assert db.groups(dt.date(2026, 10, 1)) and db.dates(dt.date(2026, 10, 1)) == [dt.date(2026, 10, 12)]


class _Resp:
    status = 200

    def __init__(self, body):
        self.body = body

    async def text(self):
        return self.body

    async def read(self):
        return self.body.encode()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def test_fetch_links():
    import fetcher

    # просроченный сертификат: для politex59.ru повторяем без проверки, для чужих сайтов — нет
    class SSLSess:
        calls = []

        def get(self, url, **kw):
            SSLSess.calls.append(kw.get("ssl", "default"))
            if kw.get("ssl") is False:
                return _Resp("<a href='https://cloud.mail.ru/public/A/b'>Расписание на Понедельник</a>")
            raise fetcher.aiohttp.ClientSSLError("expired")

    links = asyncio.run(fetcher.fetch_day_links(SSLSess(), "https://politex59.ru/x/"))
    assert links == {0: "https://cloud.mail.ru/public/A/b"} and SSLSess.calls == ["default", False]
    SSLSess.calls.clear()
    try:
        asyncio.run(fetcher._read(SSLSess(), "https://evil.example/x", True))
        raise AssertionError("должно было упасть")
    except fetcher.aiohttp.ClientSSLError:
        pass
    assert SSLSess.calls == ["default"]

    html = """
    <a href="/menu">Расписание занятий</a>
    <a class="elementor-button" href="https://cloud.mail.ru/public/AAAA/mon"><span>Расписание на Понедельник</span></a>
    <a class="elementor-button" href="https://cloud.mail.ru/public/BBBB/tue"><span>Расписание на Вторник</span></a>
    <a class="elementor-button" href="https://cloud.mail.ru/public/CCCC/wed"><span>Расписание на Среду</span></a>
    <a class="elementor-button" href="https://cloud.mail.ru/public/DDDD/thu"><span>Расписание на Четверг</span></a>
    <a class="elementor-button" href="https://cloud.mail.ru/public/EEEE/fri"><span>Расписание на Пятницу</span></a>
    <a class="elementor-button" href="https://cloud.mail.ru/public/FFFF/sat"><span>Расписание на Субботу</span></a>
    """

    class Resp:
        status = 200

        def __init__(self, body):
            self.body = body

        async def text(self):
            return self.body

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class Sess:
        def __init__(self, body):
            self.body = body

        def get(self, url, **kw):
            return Resp(self.body)

    links = asyncio.run(fetcher.fetch_day_links(Sess(html), "https://politex59.ru/x/"))
    assert links == {i: f"https://cloud.mail.ru/public/{c*4}/{d}" for i, (c, d) in enumerate(
        zip("ABCDEF", ["mon", "tue", "wed", "thu", "fri", "sat"]))}, links
    # кнопки без подписей: берём 6 ссылок на Облако по порядку
    bare = "".join(f'<a href="https://cloud.mail.ru/public/X{i}/f">  </a>' for i in range(6))
    links = asyncio.run(fetcher.fetch_day_links(Sess(bare), "https://politex59.ru/x/"))
    assert list(links) == [0, 1, 2, 3, 4, 5]
    assert fetcher._mailru_weblink("https://cloud.mail.ru/public/A1m8/cKHSLi3TF") == "A1m8/cKHSLi3TF"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("OK", name)
