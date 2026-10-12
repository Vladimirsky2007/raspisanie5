"""Получение ссылок с сайта колледжа и скачивание xlsx (в т.ч. с Облака Mail.ru).

Весь код, зависящий от внешних сайтов, собран здесь. Если Mail.ru поменяет схему
скачивания — править нужно только `download_mailru`.
"""
from __future__ import annotations

import logging
import re
from urllib.parse import urljoin, urlparse

import aiohttp
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9",
}
TIMEOUT = aiohttp.ClientTimeout(total=45)

# основа слова -> индекс дня (0 = понедельник)
DAY_STEMS = {"понедельник": 0, "вторник": 1, "сред": 2, "четверг": 3, "пятниц": 4, "суббот": 5}


class FetchError(Exception):
    pass


# У сайта колледжа бывает просроченный SSL-сертификат. Для него (и только для него)
# при ошибке сертификата повторяем запрос без проверки. Остальные сайты — всегда с проверкой.
LENIENT_SSL_HOSTS = ("politex59.ru",)
ALLOWED_LINK_HOSTS = ("cloud.mail.ru", "politex59.ru")


def _host_matches(url: str, hosts: tuple[str, ...]) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in hosts)


async def _read(session: aiohttp.ClientSession, url: str, as_text: bool):
    """GET с запасным вариантом для сайта колледжа с просроченным сертификатом."""
    async def once(ssl_param):
        kw = {} if ssl_param is None else {"ssl": ssl_param}
        async with session.get(url, allow_redirects=True, **kw) as r:
            body = await r.text() if as_text else await r.read()
            return r.status, body

    try:
        return await once(None)
    except aiohttp.ClientSSLError:
        if not _host_matches(url, LENIENT_SSL_HOSTS):
            raise
        log.warning("Сертификат %s недействителен — запрашиваем без проверки (только эту страницу)", url)
        return await once(False)


def new_session() -> aiohttp.ClientSession:
    return aiohttp.ClientSession(headers=HEADERS, timeout=TIMEOUT)


async def fetch_day_links(session: aiohttp.ClientSession, page_url: str) -> dict[int, str]:
    """Возвращает {0: ссылка на понедельник, ..., 5: ссылка на субботу}."""
    status, html = await _read(session, page_url, as_text=True)
    if status != 200:
        raise FetchError(f"страница расписания вернула HTTP {status}")
    soup = BeautifulSoup(html, "html.parser")

    links: dict[int, str] = {}
    cloud_links: list[str] = []
    direct_files: list[str] = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        full = urljoin(page_url, href)
        if not _host_matches(full, ALLOWED_LINK_HOSTS):
            continue  # ссылки на сторонние сайты не принимаем
        if "cloud.mail.ru/public/" in full and full not in cloud_links:
            cloud_links.append(full)
        if re.search(r"\.(?:xls|xlsx)(?:[?#]|$)", full, re.I) and full not in direct_files:
            direct_files.append(full)
        text = a.get_text(" ", strip=True).lower()
        if "распис" not in text and not re.search(r"\.(?:xls|xlsx)(?:[?#]|$)", full, re.I):
            continue
        for stem, idx in DAY_STEMS.items():
            if stem in text and idx not in links:
                links[idx] = full
        # На странице Луначарского кнопки могут вести сразу на файл с датой в имени.
        if re.search(r"\.(?:xls|xlsx)(?:[?#]|$)", full, re.I):
            m = re.search(r"(\d{1,2})[-_. ]+(?:январ|феврал|март|апрел|мая|июн|июл|август|сентябр|октябр|ноябр|декабр)", full.lower())
            if m:
                # День недели будет определён из самого файла; при отсутствии подписи используем порядок кнопки ниже.
                pass

    if not links and len(cloud_links) == 6:
        # кнопки без читаемых подписей: берём 6 ссылок на Облако по порядку на странице
        links = dict(enumerate(cloud_links))
    if direct_files:
        # На странице Луначарского кнопки названы датами («09 октября», «Скачать»),
        # поэтому учитываем прямые Excel-ссылки даже при частично подписанных кнопках.
        if not links:
            links = dict(enumerate(direct_files))
        else:
            used = set(links.values())
            missing = [u for u in direct_files if u not in used]
            for idx in range(6):
                if idx not in links and missing:
                    links[idx] = missing.pop(0)
    if not links:
        raise FetchError("на странице не найдены кнопки «Расписание на ...»")
    return links


def _mailru_weblink(url: str) -> str:
    path = urlparse(url).path
    if "/public/" not in path:
        raise FetchError(f"не похоже на публичную ссылку Облака: {url}")
    return path.split("/public/", 1)[1].strip("/")


def _looks_like_xlsx(data: bytes) -> bool:
    return data[:2] == b"PK"


async def _get_bytes(session: aiohttp.ClientSession, url: str) -> tuple[int, bytes]:
    return await _read(session, url, as_text=False)


async def download_mailru(session: aiohttp.ClientSession, url: str) -> bytes:
    """Скачивает файл по публичной ссылке cloud.mail.ru/public/XXXX/YYYY."""
    weblink = _mailru_weblink(url)
    bases: list[str] = []

    # Способ 1: страница шары содержит window.cloudSettings с адресом сервера выдачи.
    try:
        async with session.get(url) as r:
            page = await r.text()
        for block in re.findall(r'"weblink_get"\s*:\s*\[(.*?)\]', page, re.S):
            for u in re.findall(r'"url"\s*:\s*"([^"]+)"', block):
                bases.append(u.replace("\\u002F", "/").replace("\\/", "/"))
    except Exception as e:
        log.warning("mail.ru: не удалось прочитать страницу шары: %s", e)

    # Способ 2: dispatcher API.
    if not bases:
        try:
            async with session.get(
                "https://cloud.mail.ru/api/v2/dispatcher", params={"email": "anonym", "x-email": "anonym"}
            ) as r:
                j = await r.json(content_type=None)
            for item in (j.get("body") or {}).get("weblink_get") or []:
                if item.get("url"):
                    bases.append(item["url"])
        except Exception as e:
            log.warning("mail.ru: dispatcher недоступен: %s", e)

    if not bases:
        raise FetchError("mail.ru: не удалось получить адрес сервера для скачивания (схема могла измениться)")

    last = ""
    for base in dict.fromkeys(bases):
        direct = base.rstrip("/") + "/" + weblink
        try:
            status, data = await _get_bytes(session, direct)
        except Exception as e:
            last = f"{direct}: {e}"
            continue
        if status == 200 and _looks_like_xlsx(data):
            return data
        last = f"{direct}: HTTP {status}, {len(data)} байт"
    raise FetchError(f"mail.ru: скачивание не удалось ({last})")


async def download_xlsx(session: aiohttp.ClientSession, url: str) -> bytes:
    if "cloud.mail.ru/public/" in url:
        return await download_mailru(session, url)
    status, data = await _get_bytes(session, url)
    if status != 200 or not (_looks_like_xlsx(data) or data.startswith(bytes.fromhex("D0CF11E0A1B11AE1"))):
        raise FetchError(f"{url}: HTTP {status}, не Excel")
    return data
