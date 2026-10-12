"""Telegram-бот расписания Политеха. Запуск: python bot.py"""
from __future__ import annotations

import asyncio
import datetime as dt
import html
import logging
import os
import random

from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.types import BotCommand, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import config
from render import fmt_date, render_day, short_date
from storage import Storage, course_of
from watcher import Watcher

log = logging.getLogger("bot")
router = Router()

cfg: config.Config
storage: Storage
watcher: Watcher

ROMAN = {1: "I", 2: "II", 3: "III", 4: "IV", 5: "V", 6: "VI"}


# ---------- вспомогательное -------------------------------------------------

def today() -> dt.date:
    return dt.datetime.now(cfg.tz).date()


def btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def chunk(items: list, n: int) -> list[list]:
    return [items[i : i + n] for i in range(0, len(items), n)]


def visible_group(group: str) -> str:
    return group.split("::", 1)[-1]


def kb(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def msg_of(target: Message | CallbackQuery) -> Message:
    return target.message if isinstance(target, CallbackQuery) else target


async def show(target: Message | CallbackQuery, text: str, markup: InlineKeyboardMarkup) -> None:
    """Для кнопок редактируем текущее сообщение, для команд — отправляем новое."""
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e):
                raise
        await target.answer()
    else:
        await target.answer(text, reply_markup=markup)


async def allowed(target: Message | CallbackQuery) -> bool:
    """В личке можно всем; в группе менять настройки могут только админы."""
    msg = msg_of(target)
    chat = msg.chat
    if chat.type == ChatType.PRIVATE:
        return True
    if isinstance(target, Message) and target.sender_chat and target.sender_chat.id == chat.id:
        return True  # анонимный админ
    try:
        member = await msg.bot.get_chat_member(chat.id, target.from_user.id)
    except Exception:
        return False
    return member.status in (ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR)


async def deny(target: Message | CallbackQuery) -> None:
    text = "Менять настройки в групповом чате могут только администраторы."
    if isinstance(target, CallbackQuery):
        await target.answer(text, show_alert=True)
    else:
        await target.answer(text)


# ---------- экраны ------------------------------------------------------------

async def menu_screen(target: Message | CallbackQuery, note: str = "") -> None:
    chat_id = msg_of(target).chat.id
    storage.ensure_user(chat_id)
    user = storage.get_user(chat_id)
    if not user["group_name"]:
        return await location_screen(target)
    notify = bool(user["notify"])
    text = (
        (note + "\n\n" if note else "")
        + f"👥 Группа: <b>{html.escape(visible_group(user['group_name']))}</b>\n"
        + f"🔔 Уведомления о новом расписании: <b>{'включены' if notify else 'выключены'}</b>"
    )
    markup = kb(
        [
            [btn("📅 Сегодня", "today"), btn("📅 Завтра", "tomorrow")],
            [btn("🗓 Выбрать дату", "dates")],
            [btn("👥 Сменить группу", "chg")],
            [btn(f"🔔 Уведомления: {'выкл' if notify else 'вкл'}", "notify")],
            [btn("🎮 Игровая зона", "game")],
        ]
    )
    await show(target, text, markup)


async def location_screen(target: Message | CallbackQuery) -> None:
    user = storage.get_user(msg_of(target).chat.id)
    rows = [[btn("🏫 ул. Юбилейная", "loc:Юбилейная")], [btn("🏫 ул. Луначарского", "loc:Луначарского")]]
    rows.append([btn("🎮 Игровая зона", "game")])
    if user and user["group_name"]:
        rows.append([btn("🏠 Меню", "menu")])
    await show(target, "Сначала выберите корпус колледжа:", kb(rows))


async def course_screen(target: Message | CallbackQuery, location: str | None = None) -> None:
    user = storage.get_user(msg_of(target).chat.id)
    location = location or (user["location"] if user else None)
    if not location:
        return await location_screen(target)
    t = today()
    groups = storage.groups(t - dt.timedelta(days=30), location)
    if not groups:
        return await show(
            target,
            "Расписание пока не загружено. Попробуйте через пару минут.",
            kb([[btn("🔄 Повторить", "chg")]]),
        )
    courses = sorted({course_of(g, t) or 0 for g in groups})
    buttons = [btn(f"{ROMAN[c]} курс" if c else "Другие", f"c:{c}") for c in courses]
    rows = chunk(buttons, 2)
    rows.append([btn("◀️ Выбрать улицу", "chg")])
    await show(target, f"Корпус: {location}. Выберите курс:", kb(rows))


async def groups_screen(target: CallbackQuery, course: int) -> None:
    t = today()
    user = storage.get_user(msg_of(target).chat.id)
    location = user["location"] if user else None
    groups = [g for g in storage.groups(t - dt.timedelta(days=30), location) if (course_of(g, t) or 0) == course]
    rows = chunk([btn(visible_group(g), f"g:{g}") for g in groups], 3)
    rows.append([btn("◀️ Назад", "chg")])
    title = f"{ROMAN[course]} курс" if course else "Другие группы"
    await show(target, f"{location} — {title}. Выберите группу:", kb(rows))


async def dates_screen(target: Message | CallbackQuery) -> None:
    ds = storage.dates(today() - dt.timedelta(days=7))[:12]
    if not ds:
        return await show(target, "Расписание пока не загружено.", kb([[btn("🏠 Меню", "menu")]]))
    rows = chunk([btn(short_date(d), f"d:{d.isoformat()}") for d in ds], 3)
    rows.append([btn("🏠 Меню", "menu")])
    await show(target, "На какую дату показать расписание?", kb(rows))


async def day_screen(target: Message | CallbackQuery, d: dt.date) -> None:
    chat_id = msg_of(target).chat.id
    storage.ensure_user(chat_id)
    user = storage.get_user(chat_id)
    if not user["group_name"]:
        return await course_screen(target)

    known = storage.dates(today() - dt.timedelta(days=7))
    if d not in known:
        text = f"Расписания на <b>{html.escape(fmt_date(d))}</b> пока нет — оно ещё не опубликовано."
        return await show(target, text, kb([[btn("🗓 Другие даты", "dates")], [btn("🏠 Меню", "menu")]]))

    text = render_day(d, visible_group(user["group_name"]), storage.get_group_day(d, user["group_name"]))
    prev = max((x for x in known if x < d), default=None)
    nxt = min((x for x in known if x > d), default=None)
    nav = []
    if prev:
        nav.append(btn(f"◀️ {short_date(prev)}", f"d:{prev.isoformat()}"))
    if nxt:
        nav.append(btn(f"{short_date(nxt)} ▶️", f"d:{nxt.isoformat()}"))
    rows = ([nav] if nav else []) + [[btn("🗓 Даты", "dates"), btn("🏠 Меню", "menu")]]
    await show(target, text, kb(rows))


# ---------- команды -------------------------------------------------------------

@router.message(CommandStart())
@router.message(Command("menu"))
async def cmd_start(m: Message) -> None:
    await menu_screen(m)


@router.message(Command("group"))
async def cmd_group(m: Message) -> None:
    if not await allowed(m):
        return await deny(m)
    storage.ensure_user(m.chat.id)
    await location_screen(m)


@router.message(Command("today"))
async def cmd_today(m: Message) -> None:
    await day_screen(m, today())


@router.message(Command("tomorrow"))
async def cmd_tomorrow(m: Message) -> None:
    await day_screen(m, today() + dt.timedelta(days=1))


@router.message(Command("dates"))
async def cmd_dates(m: Message) -> None:
    await dates_screen(m)


@router.message(Command("status"))
async def cmd_status(m: Message) -> None:
    if m.from_user.id not in cfg.admin_ids:
        return
    total, subs = storage.stats()
    ok = (
        dt.datetime.fromtimestamp(watcher.last_ok, cfg.tz).strftime("%d.%m %H:%M")
        if watcher.last_ok
        else "ещё не было"
    )
    text = (
        f"Чатов: {total}, с уведомлениями: {subs}\n"
        f"Последняя успешная проверка: {ok}\n"
        f"Ошибка: {html.escape(watcher.last_error or 'нет')}"
    )
    await m.answer(text)


@router.message(Command("refresh"))
async def cmd_refresh(m: Message) -> None:
    if m.from_user.id not in cfg.admin_ids:
        return
    await m.answer("Проверяю сайт...")
    result = await watcher.check_once(force=True)
    await m.answer(html.escape(result))


# ---------- кнопки --------------------------------------------------------------

@router.callback_query(F.data == "menu")
async def cb_menu(cb: CallbackQuery) -> None:
    await menu_screen(cb)


@router.callback_query(F.data == "today")
async def cb_today(cb: CallbackQuery) -> None:
    await day_screen(cb, today())


@router.callback_query(F.data == "tomorrow")
async def cb_tomorrow(cb: CallbackQuery) -> None:
    await day_screen(cb, today() + dt.timedelta(days=1))


@router.callback_query(F.data == "dates")
async def cb_dates(cb: CallbackQuery) -> None:
    await dates_screen(cb)


@router.callback_query(F.data.startswith("d:"))
async def cb_date(cb: CallbackQuery) -> None:
    try:
        d = dt.date.fromisoformat(cb.data[2:])
    except ValueError:
        return await cb.answer()
    await day_screen(cb, d)


@router.callback_query(F.data == "chg")
async def cb_change_group(cb: CallbackQuery) -> None:
    if not await allowed(cb):
        return await deny(cb)
    await location_screen(cb)


@router.callback_query(F.data.startswith("loc:"))
async def cb_location(cb: CallbackQuery) -> None:
    if not await allowed(cb):
        return await deny(cb)
    location = cb.data[4:]
    if location not in cfg.pages:
        return await cb.answer()
    storage.set_location(cb.message.chat.id, location)
    await course_screen(cb, location)


@router.callback_query(F.data.startswith("c:"))
async def cb_course(cb: CallbackQuery) -> None:
    if not await allowed(cb):
        return await deny(cb)
    try:
        course = int(cb.data[2:])
    except ValueError:
        return await cb.answer()
    await groups_screen(cb, course)


@router.callback_query(F.data.startswith("g:"))
async def cb_group(cb: CallbackQuery) -> None:
    if not await allowed(cb):
        return await deny(cb)
    group = cb.data[2:]
    user = storage.get_user(cb.message.chat.id)
    if group not in storage.groups(today() - dt.timedelta(days=30), user["location"] if user else None):
        return await cb.answer("Такой группы нет в расписании", show_alert=True)
    storage.set_group(cb.message.chat.id, group)
    await menu_screen(cb, note="✅ Группа сохранена.")


@router.callback_query(F.data == "notify")
async def cb_notify(cb: CallbackQuery) -> None:
    if not await allowed(cb):
        return await deny(cb)
    storage.ensure_user(cb.message.chat.id)
    storage.toggle_notify(cb.message.chat.id)
    await menu_screen(cb)



# ---------- игровая зона (виртуальные монеты, не реальные деньги) ---------------

BET_OPTIONS = [10, 50, 100, 250, 500]


def player_name(user) -> str:
    return (user.full_name or user.username or f"Игрок {user.id}")[:80]


async def game_screen(target: Message | CallbackQuery, note: str = "") -> None:
    actor = target.from_user
    storage.game_player(actor.id, player_name(actor), actor.username)
    markup = kb([
        [btn("💰 Баланс", "game:balance")],
        [btn("🎡 Рулетка", "game:roulette"), btn("🎁 Ежедневный бонус", "game:bonus")],
        [btn("🏆 Топ-20 игроков", "game:top")],
        [btn("❌⭕ Крестики-нолики", "game:ttt")],
        [btn("🏠 В главное меню", "menu")],
    ])
    text = "🎮 <b>Игровая зона</b>\n\nИграем на виртуальные монеты — это не реальные деньги и их нельзя вывести."
    if note:
        text = note + "\n\n" + text
    await show(target, text, markup)


async def roulette_screen(target: Message | CallbackQuery, bet: int | None = None) -> None:
    if bet is None:
        rows = [[btn(f"Ставка {n} ₽", f"game:rbet:{n}") for n in BET_OPTIONS[i:i+2]]
                for i in range(0, len(BET_OPTIONS), 2)]
        rows.append([btn("◀️ В игровую зону", "game")])
        return await show(target, "🎡 <b>Рулетка</b>\nСначала выбери ставку, затем цвет.\n🔴 Красный / ⚫ Чёрный — 2×; 🟢 Зелёный — 14×.", kb(rows))
    await show(target, f"🎡 Ставка: <b>{bet} ₽</b>. На какой цвет ставишь?", kb([
        [btn("🔴 Красный ×2", f"game:roulette:{bet}:red"), btn("⚫ Чёрный ×2", f"game:roulette:{bet}:black")],
        [btn("🟢 Зелёный ×14", f"game:roulette:{bet}:green")],
        [btn("◀️ Изменить ставку", "game:roulette")],
    ]))


@router.callback_query(F.data == "game")
async def cb_game(cb: CallbackQuery) -> None:
    await game_screen(cb)


@router.callback_query(F.data == "game:balance")
async def cb_game_balance(cb: CallbackQuery) -> None:
    bal = storage.game_balance(cb.from_user.id, player_name(cb.from_user), cb.from_user.username)
    await show(cb, f"💰 <b>Твой баланс:</b> {bal:,} ₽\n\nМонеты виртуальные.".replace(",", " "), kb([
        [btn("🎡 Рулетка", "game:roulette")],
        [btn("◀️ Игровая зона", "game")],
    ]))


@router.callback_query(F.data == "game:roulette")
async def cb_game_roulette(cb: CallbackQuery) -> None:
    await roulette_screen(cb)


@router.callback_query(F.data.startswith("game:rbet:"))
async def cb_game_rbet(cb: CallbackQuery) -> None:
    try:
        bet = int(cb.data.rsplit(":", 1)[1])
    except ValueError:
        return await cb.answer()
    if bet not in BET_OPTIONS:
        return await cb.answer("Неверная ставка", show_alert=True)
    await roulette_screen(cb, bet)


@router.callback_query(F.data == "game:bonus")
async def cb_game_bonus(cb: CallbackQuery) -> None:
    amount = random.randint(10, 1000)
    ok, balance, awarded = storage.game_bonus(cb.from_user.id, player_name(cb.from_user), today().isoformat(), amount, cb.from_user.username)
    note = (f"🎁 Ежедневный бонус: <b>+{awarded} ₽</b>!\n💰 Баланс: <b>{balance} ₽</b>"
            if ok else f"⏳ Ты уже получил бонус сегодня!\n💰 Баланс: <b>{balance} ₽</b>")
    await game_screen(cb, note)


@router.callback_query(F.data == "game:top")
async def cb_game_top(cb: CallbackQuery) -> None:
    players = storage.game_top(20)
    lines = ["🏆 <b>Топ-20 игроков по балансу</b>", ""]
    medals = ["🥇", "🥈", "🥉"]
    for i, row in enumerate(players, 1):
        prefix = medals[i-1] if i <= 3 else f"{i}."
        shown_name = ("@" + row["username"]) if row["username"] else row["display_name"]
        escaped_name = html.escape(shown_name)
        # Username-ссылка открывает профиль по @username; если его нет — Telegram user mention по ID.
        profile_url = f"https://t.me/{row['username']}" if row["username"] else f"tg://user?id={row['user_id']}"
        clickable = f'<a href="{html.escape(profile_url, quote=True)}">{escaped_name}</a>'
        lines.append(f"{prefix} {clickable} — <b>{row['balance']:,} ₽</b>".replace(",", " "))
    if not players:
        lines.append("Пока игроков нет. Заходи в игровую зону!")
    await show(cb, "\n".join(lines), kb([[btn("◀️ Игровая зона", "game")]]))


@router.callback_query(F.data.startswith("game:roulette:"))
async def cb_game_roulette_play(cb: CallbackQuery) -> None:
    try:
        _, _, bet_s, color = cb.data.split(":")
        bet = int(bet_s)
    except ValueError:
        return await cb.answer()
    if bet not in BET_OPTIONS or color not in {"red", "black", "green"}:
        return await cb.answer("Неверная ставка", show_alert=True)
    landed = random.choices(["red", "black", "green"], weights=[18, 18, 2], k=1)[0]
    payout = bet * (14 if landed == "green" else 2) if landed == color else 0
    ok, balance = storage.game_play(cb.from_user.id, player_name(cb.from_user), bet, payout, cb.from_user.username)
    if not ok:
        return await cb.answer(f"Не хватает монет. Баланс: {balance} ₽", show_alert=True)
    emoji = {"red": "🔴 Красный", "black": "⚫ Чёрный", "green": "🟢 Зелёный"}
    result = f"🎉 Выигрыш: +{payout-bet} ₽" if payout else f"😢 Ставка {bet} ₽ проиграла."
    await show(cb, f"🎡 <b>Рулетка</b>\n\nВыпало: <b>{emoji[landed]}</b>\nТвоя ставка: {emoji[color]} — {bet} ₽\n\n{result}\n💰 Баланс: <b>{balance} ₽</b>", kb([
        [btn("🔄 Ещё ставка", f"game:rbet:{bet}")],
        [btn("🎡 Другая ставка", "game:roulette"), btn("◀️ Игровая зона", "game")],
    ]))



# ---------- крестики-нолики: матчмейкинг в личных сообщениях -------------------

TTT_QUEUE: list[tuple[int, str]] = []
TTT_MATCHES: dict[str, dict] = {}
TTT_BY_PLAYER: dict[int, str] = {}
TTT_LOCK = asyncio.Lock()
TTT_REWARD_WIN = 300
TTT_REWARD_LOSE = 50


def ttt_keyboard(match_id: str, board: list[str], turn: int, player_id: int, finished: bool = False):
    rows = []
    if not finished:
        for r in range(3):
            row = []
            for c in range(3):
                i = r * 3 + c
                label = board[i] if board[i] != " " else "▫️"
                row.append(btn(label, f"ttt:{match_id}:{i}"))
            rows.append(row)
    rows.append([btn("🎮 Игровая зона", "game")])
    return kb(rows)


def ttt_status(match: dict, player_id: int) -> str:
    mark = "❌" if player_id == match["players"][0] else "⭕"
    opponent = match["names"][1] if player_id == match["players"][0] else match["names"][0]
    board_text = "\n".join(
        "".join(match["board"][r * 3:r * 3 + 3][c] if match["board"][r * 3 + c] != " " else "▫️"
                for c in range(3))
        for r in range(3)
    )
    if match["finished"]:
        state = match["result_text"]
    elif match["turn"] == player_id:
        state = "👉 Твой ход!"
    else:
        state = "⏳ Ход соперника."
    return (f"❌⭕ <b>Крестики-нолики</b>\n"
            f"Ты играешь за {mark}. Соперник: <b>{html.escape(opponent)}</b>\n\n"
            f"{board_text}\n\n{state}")


def ttt_winner(board: list[str]) -> str | None:
    lines = [(0,1,2),(3,4,5),(6,7,8),(0,3,6),(1,4,7),(2,5,8),(0,4,8),(2,4,6)]
    for a,b,c in lines:
        if board[a] != " " and board[a] == board[b] == board[c]:
            return board[a]
    return None


async def ttt_render_match(bot: Bot, match_id: str) -> None:
    match = TTT_MATCHES.get(match_id)
    if not match:
        return
    for uid in match["players"]:
        text = ttt_status(match, uid)
        markup = ttt_keyboard(match_id, match["board"], match["turn"], uid, match["finished"])
        mid = match["message_ids"].get(uid)
        if mid:
            try:
                await bot.edit_message_text(text, chat_id=uid, message_id=mid, reply_markup=markup)
                continue
            except TelegramBadRequest as e:
                if "message is not modified" in str(e).lower():
                    continue
                log.warning("Не удалось обновить поле крестиков-ноликов для %s: %s", uid, e)
        try:
            msg = await bot.send_message(uid, text, reply_markup=markup)
            match["message_ids"][uid] = msg.message_id
        except Exception:
            log.exception("Не удалось отправить поле крестиков-ноликов игроку %s", uid)


@router.callback_query(F.data == "game:ttt")
async def cb_ttt_join(cb: CallbackQuery) -> None:
    if cb.message.chat.type != ChatType.PRIVATE:
        return await cb.answer("Крестики-нолики доступны в личном чате с ботом.", show_alert=True)
    uid = cb.from_user.id
    name = player_name(cb.from_user)
    storage.game_player(uid, name, cb.from_user.username)
    async with TTT_LOCK:
        if uid in TTT_BY_PLAYER:
            mid = TTT_BY_PLAYER[uid]
            await cb.answer("Ты уже участвуешь в матче.", show_alert=False)
            await ttt_render_match(cb.bot, mid)
            return
        TTT_QUEUE[:] = [(qid, qname) for qid, qname in TTT_QUEUE if qid != uid]
        if TTT_QUEUE:
            opponent_id, opponent_name = TTT_QUEUE.pop(0)
            match_id = f"{min(uid, opponent_id)}_{max(uid, opponent_id)}"
            match = {
                "players": [opponent_id, uid],
                "names": [opponent_name, name],
                "board": [" "] * 9,
                "turn": opponent_id,
                "message_ids": {},
                "finished": False,
                "result_text": "",
            }
            TTT_MATCHES[match_id] = match
            TTT_BY_PLAYER[opponent_id] = match_id
            TTT_BY_PLAYER[uid] = match_id
            await cb.answer("Соперник найден! Первым ходит ❌.")
            try:
                await cb.message.edit_text("🎮 Соперник найден! Открываю игровое поле…")
            except TelegramBadRequest:
                pass
            # Send each player their own board. If opponent never opened private chat,
            # the requester is safely returned to the game menu.
            try:
                for player_id in match["players"]:
                    msg = await cb.bot.send_message(
                        player_id, ttt_status(match, player_id),
                        reply_markup=ttt_keyboard(match_id, match["board"], match["turn"], player_id)
                    )
                    match["message_ids"][player_id] = msg.message_id
            except Exception:
                TTT_MATCHES.pop(match_id, None)
                TTT_BY_PLAYER.pop(opponent_id, None)
                TTT_BY_PLAYER.pop(uid, None)
                await cb.message.answer("Не удалось написать сопернику. Оба игрока должны сначала открыть личный чат с ботом и нажать /start.")
            return
        TTT_QUEUE.append((uid, name))
        await cb.answer()
    await show(cb, "🔎 <b>Поиск соперника…</b>\n\nКак только другой игрок нажмёт «Найти соперника», начнётся матч. Не закрывай чат.", kb([
        [btn("❌ Отменить поиск", "game:ttt_cancel")],
        [btn("◀️ Игровая зона", "game")],
    ]))


@router.callback_query(F.data == "game:ttt_cancel")
async def cb_ttt_cancel(cb: CallbackQuery) -> None:
    uid = cb.from_user.id
    async with TTT_LOCK:
        TTT_QUEUE[:] = [(qid, name) for qid, name in TTT_QUEUE if qid != uid]
    await game_screen(cb, "Поиск соперника отменён.")


@router.callback_query(F.data.startswith("ttt:"))
async def cb_ttt_move(cb: CallbackQuery) -> None:
    parts = cb.data.split(":")
    if len(parts) != 3:
        return await cb.answer()
    match_id = parts[1]
    try:
        pos = int(parts[2])
    except ValueError:
        return await cb.answer()
    if not 0 <= pos < 9:
        return await cb.answer()
    async with TTT_LOCK:
        match = TTT_MATCHES.get(match_id)
        if not match or cb.from_user.id not in match["players"]:
            return await cb.answer("Матч не найден.", show_alert=True)
        if match["finished"]:
            return await cb.answer("Матч уже завершён.", show_alert=True)
        if match["turn"] != cb.from_user.id:
            return await cb.answer("Сейчас ход соперника.", show_alert=True)
        if match["board"][pos] != " ":
            return await cb.answer("Эта клетка уже занята.", show_alert=True)
        mark = "❌" if cb.from_user.id == match["players"][0] else "⭕"
        match["board"][pos] = mark
        winner = ttt_winner(match["board"])
        if winner:
            match["finished"] = True
            winner_id = cb.from_user.id
            loser_id = next(p for p in match["players"] if p != winner_id)
            win_name = match["names"][match["players"].index(winner_id)]
            lose_name = match["names"][match["players"].index(loser_id)]
            _, win_balance = storage.game_change_balance(winner_id, win_name, TTT_REWARD_WIN, cb.from_user.username if winner_id == cb.from_user.id else None)
            _, lose_balance = storage.game_change_balance(loser_id, lose_name, TTT_REWARD_LOSE, cb.from_user.username if loser_id == cb.from_user.id else None)
            match["result_text"] = (
                f"🏆 Победил <b>{html.escape(win_name)}</b>! +{TTT_REWARD_WIN} ₽\n"
                f"🤝 Сопернику <b>{html.escape(lose_name)}</b>: утешительные +{TTT_REWARD_LOSE} ₽\n"
                f"💰 Твой баланс: {win_balance if cb.from_user.id == winner_id else lose_balance} ₽"
            )
            TTT_BY_PLAYER.pop(winner_id, None)
            TTT_BY_PLAYER.pop(loser_id, None)
        elif all(cell != " " for cell in match["board"]):
            match["finished"] = True
            for pid, pname in zip(match["players"], match["names"]):
                _, bal = storage.game_change_balance(pid, pname, TTT_REWARD_LOSE)
            match["result_text"] = f"🤝 Ничья! Оба игрока получают +{TTT_REWARD_LOSE} ₽."
            TTT_BY_PLAYER.pop(match["players"][0], None)
            TTT_BY_PLAYER.pop(match["players"][1], None)
        else:
            match["turn"] = next(p for p in match["players"] if p != cb.from_user.id)
        await cb.answer()
        await ttt_render_match(cb.bot, match_id)


async def start_health_server() -> web.AppRunner | None:
    """Мини-сервер для хостингов вроде Render: отвечает «ok» на GET/HEAD /.
    Его пингует UptimeRobot / cron-job.org, чтобы сервис не засыпал. Включается, если задана PORT."""
    port = int(os.environ.get("PORT", "0") or 0)
    if not port:
        return None
    app = web.Application()
    app.router.add_get("/", lambda request: web.Response(text="ok"))
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    log.info("health-сервер запущен на порту %s", port)
    return runner


# ---------- запуск --------------------------------------------------------------

async def main() -> None:
    global cfg, storage, watcher
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = config.load()
    storage = Storage(cfg.db_path)

    bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)

    async def send(chat_id: int, text: str) -> None:
        for attempt in range(2):
            try:
                await bot.send_message(chat_id, text)
                return
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
            except TelegramForbiddenError:
                storage.deactivate(chat_id)  # бот заблокирован / удалён из чата
                return
            except TelegramBadRequest as e:
                if "chat not found" in str(e).lower():
                    storage.deactivate(chat_id)
                    return
                raise

    async def notify_admin(text: str) -> None:
        for admin_id in cfg.admin_ids:
            try:
                await bot.send_message(admin_id, text, parse_mode=None)
            except Exception:
                log.exception("не удалось написать админу %s", admin_id)

    watcher = Watcher(cfg, storage, send, notify_admin)
    health = await start_health_server()
    task = asyncio.create_task(watcher.run_forever())

    await bot.set_my_commands(
        [
            BotCommand(command="menu", description="Главное меню"),
            BotCommand(command="today", description="Расписание на сегодня"),
            BotCommand(command="tomorrow", description="Расписание на завтра"),
            BotCommand(command="dates", description="Выбрать дату"),
            BotCommand(command="group", description="Сменить группу"),
        ]
    )
    try:
        await dp.start_polling(bot)
    finally:
        task.cancel()
        if health:
            await health.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
