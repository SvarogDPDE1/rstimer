
import asyncio
import html
import logging
import re
import sqlite3
from datetime import datetime, timedelta
from typing import Dict, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram.client.session.aiohttp import AiohttpSession
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message


BOT_TOKEN = "9238830575:AAGtlPcszMqfTcCNO1cMHsqafIV9zxntmnc"
DB_PATH = "timers.db"
DEFAULT_TZ = "Europe/Moscow"




QUICK_PRESETS: list[tuple[str, int]] = [
    ("+15 мин", 15 * 60),
    ("+1 час", 60 * 60),
    ("+3 часа", 3 * 3600),
    ("+24 часа", 24 * 3600),
]

POPULAR_TIMEZONES: list[tuple[str, str]] = [
    ("🇷🇺 Москва (UTC+3)", "Europe/Moscow"),
    ("🇷🇺 Калининград (UTC+2)", "Europe/Kaliningrad"),
    ("🇷🇺 Самара (UTC+4)", "Europe/Samara"),
    ("🇷🇺 Екатеринбург (UTC+5)", "Asia/Yekaterinburg"),
    ("🇷🇺 Новосибирск (UTC+7)", "Asia/Novosibirsk"),
    ("🇷🇺 Владивосток (UTC+10)", "Asia/Vladivostok"),
    ("🌐 UTC (UTC+0)", "UTC"),
]

REPEAT_LABELS: dict[str, str] = {
    "none": "одноразовый",
    "daily": "каждый день",
    "weekly": "каждую неделю",
}

WEEKDAY_ALIASES: dict[str, int] = {
    "пн": 0,
    "понедельник": 0,
    "mon": 0,
    "вт": 1,
    "вторник": 1,
    "tue": 1,
    "ср": 2,
    "среда": 2,
    "wed": 2,
    "чт": 3,
    "четверг": 3,
    "thu": 3,
    "пт": 4,
    "пятница": 4,
    "fri": 4,
    "сб": 5,
    "суббота": 5,
    "sat": 5,
    "вс": 6,
    "воскресенье": 6,
    "sun": 6,
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()

active_timers: Dict[tuple[int, int], asyncio.Task] = {}
bot_username_cache: Optional[str] = None

DATE_FORMATS = (
    "%d.%m.%Y %H:%M:%S",
    "%d.%m.%Y %H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
)


# --- Работа с SQLite ---


def init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_settings (
                chat_id INTEGER PRIMARY KEY,
                timezone TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS timers (
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                message_thread_id INTEGER,
                created_utc_ts REAL NOT NULL,
                target_utc_ts REAL NOT NULL,
                label TEXT NOT NULL,
                timezone TEXT NOT NULL,
                repeat_mode TEXT NOT NULL DEFAULT 'none',
                notify_user_id INTEGER,
                PRIMARY KEY (chat_id, message_id)
            )
            """
        )
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(timers)").fetchall()
        }
        migrations: dict[str, str] = {
            "message_thread_id": "ALTER TABLE timers ADD COLUMN message_thread_id INTEGER",
            "created_utc_ts": "ALTER TABLE timers ADD COLUMN created_utc_ts REAL NOT NULL DEFAULT 0",
            "repeat_mode": "ALTER TABLE timers ADD COLUMN repeat_mode TEXT NOT NULL DEFAULT 'none'",
            "notify_user_id": "ALTER TABLE timers ADD COLUMN notify_user_id INTEGER",
        }
        for col, sql in migrations.items():
            if col not in columns:
                conn.execute(sql)
        conn.commit()


def get_chat_timezone_name(chat_id: int) -> str:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT timezone FROM chat_settings WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()
    return row[0] if row else DEFAULT_TZ


def set_chat_timezone_name(chat_id: int, tz_name: str) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO chat_settings (chat_id, timezone)
            VALUES (?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET timezone = excluded.timezone
            """,
            (chat_id, tz_name),
        )
        conn.commit()


def save_timer_db(
    chat_id: int,
    message_id: int,
    message_thread_id: Optional[int],
    created_utc_ts: float,
    target_utc_ts: float,
    label: str,
    tz_name: str,
    repeat_mode: str = "none",
    notify_user_id: Optional[int] = None,
) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO timers
                (chat_id, message_id, message_thread_id, created_utc_ts, target_utc_ts, label, timezone, repeat_mode, notify_user_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                chat_id,
                message_id,
                message_thread_id,
                created_utc_ts,
                target_utc_ts,
                label,
                tz_name,
                repeat_mode,
                notify_user_id,
            ),
        )
        conn.commit()


def update_timer_notify_db(
    chat_id: int, message_id: int, notify_user_id: Optional[int]
) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE timers SET notify_user_id = ? WHERE chat_id = ? AND message_id = ?",
            (notify_user_id, chat_id, message_id),
        )
        conn.commit()


def get_timer_db(
    chat_id: int, message_id: int
) -> Optional[tuple[Optional[int], float, float, str, str, str, Optional[int]]]:
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute(
            """
            SELECT message_thread_id, created_utc_ts, target_utc_ts, label, timezone, repeat_mode, notify_user_id
            FROM timers
            WHERE chat_id = ? AND message_id = ?
            """,
            (chat_id, message_id),
        ).fetchone()


def delete_timer_db(chat_id: int, message_id: int) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "DELETE FROM timers WHERE chat_id = ? AND message_id = ?",
            (chat_id, message_id),
        )
        conn.commit()


def load_all_timers_db() -> list[
    tuple[int, int, Optional[int], float, float, str, str, str, Optional[int]]
]:
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute(
            """
            SELECT chat_id, message_id, message_thread_id, created_utc_ts, target_utc_ts, label, timezone, repeat_mode, notify_user_id
            FROM timers
            ORDER BY target_utc_ts ASC
            """
        ).fetchall()


def load_chat_timers_db(
    chat_id: int,
) -> list[tuple[int, Optional[int], float, float, str, str, str, Optional[int]]]:
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute(
            """
            SELECT message_id, message_thread_id, created_utc_ts, target_utc_ts, label, timezone, repeat_mode, notify_user_id
            FROM timers
            WHERE chat_id = ?
            ORDER BY target_utc_ts ASC
            """,
            (chat_id,),
        ).fetchall()


# --- Парсинг и расчёт дат ---


def is_group_chat(message: Message) -> bool:
    return message.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)


async def get_bot_username() -> str:
    global bot_username_cache
    if bot_username_cache is None:
        me = await bot.get_me()
        bot_username_cache = (me.username or "").lower()
    return bot_username_cache


def load_tz(tz_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        return ZoneInfo(DEFAULT_TZ)


def extract_repeat_mode(text: str) -> tuple[str, str]:
    patterns = [
 (r"\s+(каждый\s+день|ежедневно|daily)$", "daily"),
        (r"\s+(каждую\s+неделю|еженедельно|weekly)$", "weekly"),
    ]
    cleaned = text.strip()
    for pattern, mode in patterns:
        match = re.search(pattern, cleaned, flags=re.IGNORECASE)
        if match:
            return cleaned[: match.start()].strip(), mode
    return cleaned, "none"


def parse_target_datetime(
    text: str, tz: ZoneInfo
) -> tuple[Optional[datetime], str, str]:
    without_repeat, repeat_mode = extract_repeat_mode(text)
    match = re.match(
        r"^(\d{2}\.\d{2}\.\d{4}|\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}(?::\d{2})?)(?:\s+(.*))?$",
        without_repeat,
    )
    if not match:
        return None, "", "none"

    date_part, time_part, label = match.groups()
    dt_str = f"{date_part} {time_part}"
    label = (label or "Событие").strip()

    for fmt in DATE_FORMATS:
        try:
            naive_dt = datetime.strptime(dt_str, fmt)
            return naive_dt.replace(tzinfo=tz), label, repeat_mode
        except ValueError:
            continue

    return None, "", "none"


def parse_repeat_command(
    arg_text: str, tz: ZoneInfo
) -> tuple[Optional[datetime], str, str]:
    cleaned = arg_text.strip()
    if not cleaned:
        return None, "", "none"

    dt, label, mode = parse_target_datetime(cleaned, tz)
    if dt:
        return dt, label, mode if mode != "none" else "daily"

    now_local = datetime.now(tz=tz)
    match = re.match(
        r"^(?:([а-яёa-z]+)\s+)?(\d{2}:\d{2}(?::\d{2})?)(?:\s+(.*))?$",
        cleaned,
        flags=re.IGNORECASE,
    )
    if not match:
        return None, "", "none"

    weekday_str, time_str, label = match.groups()
    label = (label or "Повторяющийся таймер").strip()

    time_fmt = "%H:%M:%S" if time_str.count(":") == 2 else "%H:%M"
    try:
        parsed_time = datetime.strptime(time_str, time_fmt).time()
    except ValueError:
        return None, "", "none"

    if weekday_str:
        wd = WEEKDAY_ALIASES.get(weekday_str.lower())
        if wd is None:
            return None, "", "none"
        days_ahead = (wd - now_local.weekday()) % 7
        candidate = now_local.replace(
            hour=parsed_time.hour,
            minute=parsed_time.minute,
            second=parsed_time.second,
            microsecond=0,
        ) + timedelta(days=days_ahead)
        if candidate <= now_local:
            candidate += timedelta(days=7)
        return candidate, label, "weekly"

    candidate = now_local.replace(
        hour=parsed_time.hour,
        minute=parsed_time.minute,
        second=parsed_time.second,
        microsecond=0,
    )
    if candidate <= now_local:
        candidate += timedelta(days=1)
    return candidate, label, "daily"


def compute_next_occurrence(
    prev_created_utc_ts: float,
    prev_target_utc_ts: float,
    now_utc_ts: float,
    repeat_mode: str,
) -> tuple[float, float]:
    step = 86400.0 if repeat_mode == "daily" else 7 * 86400.0
    next_target = prev_target_utc_ts + step
    while next_target <= now_utc_ts:
        next_target += step
    next_created = min(prev_target_utc_ts, now_utc_ts)
    if prev_created_utc_ts > 0 and (next_target - next_created) <= 0:
        next_created = now_utc_ts
    return next_created, next_target


# --- Визуальные компоненты ---


def build_progress_bar(
    created_utc_ts: float,
    target_utc_ts: float,
    now_utc_ts: float,
    width: int = 12,
) -> str:
    total_span = target_utc_ts - created_utc_ts
    if total_span <= 0 or created_utc_ts <= 0:
        return f"<code>[{'▓' * width}]</code> <b>100%</b>"

    elapsed = max(0.0, min(total_span, now_utc_ts - created_utc_ts))
    ratio = elapsed / total_span
    filled = max(0, min(width, int(round(ratio * width))))
    bar = "▓" * filled + "░" * (width - filled)
    percent = int(round(ratio * 100))
    return f"<code>[{bar}]</code> <b>{percent}%</b>"


def format_remaining_blocks(delta: timedelta) -> str:
    total_seconds = max(0, int(delta.total_seconds()))
    days, rem = divmod(total_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)

    parts: list[str] = []
    if days > 0:
        parts.append(f"<b>{days}</b> дн.")
    parts.append(f"<b>{hours:02d}</b> ч")
    parts.append(f"<b>{minutes:02d}</b> мин")
    parts.append(f"<b>{seconds:02d}</b> сек")
    return " · ".join(parts)


def format_short_remaining(remaining_sec: float) -> str:
    total_seconds = max(0, int(remaining_sec))
    days, rem = divmod(total_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    if days > 0:
        return f"{days}д {hours:02d}ч {minutes:02d}м"
    if hours > 0:
        return f"{hours}ч {minutes:02d}м {seconds:02d}с"
    return f"{minutes:02d}м {seconds:02d}с"


def get_sleep_interval(remaining_seconds: float, chat_id: int) -> float:
    is_group = chat_id < 0
    if remaining_seconds > 3600:
        return 45.0 if is_group else 30.0
    if remaining_seconds > 60:
        return 10.0 if is_group else 5.0
    return 3.0 if is_group else 2.0


def render_timer_card(
    label: str,
    created_utc_ts: float,
    target_utc_ts: float,
    now_utc_ts: float,
    tz_name: str,
    repeat_mode: str = "none",
    notify_user_id: Optional[int] = None,
) -> str:
    tz = load_tz(tz_name)
    target_local = datetime.fromtimestamp(target_utc_ts, tz=ZoneInfo("UTC")).astimezone(tz)
    remaining_sec = max(0.0, target_utc_ts - now_utc_ts)
    safe_label = html.escape(label)
    safe_tz = html.escape(tz_name)

    meta_lines = [
        f"🗓 Цель: <code>{target_local.strftime('%d.%m.%Y %H:%M:%S')}</code>",
        f"🌍 Пояс: <code>{safe_tz}</code>",
    ]
    if repeat_mode != "none":
        meta_lines.append(f"🔁 Повтор: <b>{REPEAT_LABELS.get(repeat_mode, repeat_mode)}</b>")
    if notify_user_id:
        meta_lines.append("🔔 Уведомление в ЛС: <b>включено</b>")

    return (
        f"⏳ <b>{safe_label}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Осталось: {format_remaining_blocks(timedelta(seconds=remaining_sec))}\n"
        f"Прогресс: {build_progress_bar(created_utc_ts, target_utc_ts, now_utc_ts)}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        + "\n".join(meta_lines)
    )


def render_done_card(
    label: str,
    target_utc_ts: float,
    tz_name: str,
    repeat_mode: str = "none",
) -> str:
    tz = load_tz(tz_name)
    target_local = datetime.fromtimestamp(target_utc_ts, tz=ZoneInfo("UTC")).astimezone(tz)
    safe_label = html.escape(label)
    safe_tz = html.escape(tz_name)

    extra = (
        f"\n🔁 Режим: <b>{REPEAT_LABELS.get(repeat_mode, repeat_mode)}</b> (запускаю следующий цикл...)"
        if repeat_mode != "none"
        else ""
    )

    return (
        f"🎉 <b>Время вышло — {safe_label}!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<code>[{'▓' * 12}]</code> <b>100%</b>\n"
        f"🗓 Наступило: <code>{target_local.strftime('%d.%m.%Y %H:%M:%S')}</code>\n"
        f"🌍 Пояс: <code>{safe_tz}</code>{extra}"
    )


# --- Клавиатуры ---


def timer_controls_kb(
    chat_id: int,
    message_id: int,
    notify_enabled: bool,
) -> InlineKeyboardMarkup:
    notify_btn_text = "🔕 Не слать в ЛС" if notify_enabled else "🔔 В ЛС"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔄 Обновить",
                    callback_data=f"refresh_timer:{chat_id}:{message_id}",
                ),
                InlineKeyboardButton(
                    text=notify_btn_text,
                    callback_data=f"toggle_dm:{chat_id}:{message_id}",
                ),
                InlineKeyboardButton(
                    text="⏹ Стоп",
                    callback_data=f"stop_timer:{chat_id}:{message_id}",
                ),
            ]
        ]
    )


def quick_presets_kb() -> InlineKeyboardMarkup:
    row1 = [
        InlineKeyboardButton(text=title, callback_data=f"preset:{sec}")
 for title, sec in QUICK_PRESETS[:2]
    ]
    row2 = [
        InlineKeyboardButton(text=title, callback_data=f"preset:{sec}")
        for title, sec in QUICK_PRESETS[2:]
    ]
    return InlineKeyboardMarkup(
        inline_keyboard=[
            row1,
            row2,
            [
                InlineKeyboardButton(text="📋 Активные таймеры", callback_data="list_timers"),
                InlineKeyboardButton(text="🌍 Часовой пояс", callback_data="open_tz_menu"),
            ],
        ]
    )


def timezone_menu_kb(current_tz: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for title, tz_code in POPULAR_TIMEZONES:
        mark = " ✅" if tz_code == current_tz else ""
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{title}{mark}",
                    callback_data=f"set_tz:{tz_code}",
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def timers_list_kb(
    chat_id: int,
    rows: list[tuple[int, Optional[int], float, float, str, str, str, Optional[int]]],
    now_utc_ts: float,
) -> InlineKeyboardMarkup:
    keyboard: list[list[InlineKeyboardButton]] = []
    for msg_id, _, _, target_utc_ts, label, _, repeat_mode, _ in rows[:10]:
        rem_str = format_short_remaining(max(0.0, target_utc_ts - now_utc_ts))
        rep_icon = " 🔁" if repeat_mode != "none" else ""
        short_label = label[:20] + ("…" if len(label) > 20 else "")
        keyboard.append(
            [
                InlineKeyboardButton(
                    text=f"⏹ {short_label}{rep_icon} ({rem_str})",
                    callback_data=f"stop_timer:{chat_id}:{msg_id}",
                )
            ]
        )
    if len(rows) > 1:
        keyboard.append(
            [
                InlineKeyboardButton(
                    text="🗑 Остановить все таймеры",
                    callback_data=f"stop_all:{chat_id}",
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


# --- Уведомления и цикл таймера ---


async def send_dm_notification(
    notify_user_id: Optional[int],
    label: str,
    target_utc_ts: float,
    tz_name: str,
    repeat_mode: str,
) -> None:
    if not notify_user_id:
        return
    tz = load_tz(tz_name)
    target_local = datetime.fromtimestamp(target_utc_ts, tz=ZoneInfo("UTC")).astimezone(tz)
    repeat_note = (
        f"\n🔁 Повтор: <b>{REPEAT_LABELS.get(repeat_mode, repeat_mode)}</b>"
        if repeat_mode != "none"
        else ""
    )
    text = (
        f"🔔 <b>Напоминание: {html.escape(label)}</b>\n"
        f"🗓 Время: <code>{target_local.strftime('%d.%m.%Y %H:%M:%S')}</code> "
        f"(<code>{html.escape(tz_name)}</code>){repeat_note}"
    )
    try:
        await bot.send_message(chat_id=notify_user_id, text=text)
    except TelegramForbiddenError:
        logging.info(
            f"Не удалось отправить ЛС пользователю {notify_user_id}: бот заблокирован или чат не начат."
        )
    except TelegramBadRequest as e:
        logging.warning(f"Ошибка отправки ЛС пользователю {notify_user_id}: {e}")


async def run_dynamic_timer(
    chat_id: int,
    message_id: int,
    message_thread_id: Optional[int],
    created_utc_ts: float,
    target_utc_ts: float,
    label: str,
    tz_name: str,
    repeat_mode: str = "none",
    notify_user_id: Optional[int] = None,
) -> None:
    key = (chat_id, message_id)
    cur_created = created_utc_ts
    cur_target = target_utc_ts
    cur_notify_user = notify_user_id
    last_rendered_text = ""

    try:
        while True:
            db_row = get_timer_db(chat_id, message_id)
            if db_row:
                cur_notify_user = db_row[6]

            now_utc_ts = datetime.now(tz=ZoneInfo("UTC")).timestamp()
            remaining_sec = cur_target - now_utc_ts

            if remaining_sec <= 0:
                done_text = render_done_card(label, cur_target, tz_name, repeat_mode)
                try:
                    await bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=message_id,
                        text=done_text,
                        reply_markup=None,
                    )
                except TelegramBadRequest:
                    pass

                await send_dm_notification(
                    notify_user_id=cur_notify_user,
                    label=label,
                    target_utc_ts=cur_target,
                    tz_name=tz_name,
                    repeat_mode=repeat_mode,
                )

                if repeat_mode in ("daily", "weekly"):
                    cur_created, cur_target = compute_next_occurrence(
                        prev_created_utc_ts=cur_created,
                        prev_target_utc_ts=cur_target,
                        now_utc_ts=datetime.now(tz=ZoneInfo("UTC")).timestamp(),
                        repeat_mode=repeat_mode,
                    )
                    save_timer_db(
                        chat_id=chat_id,
                        message_id=message_id,
                        message_thread_id=message_thread_id,
                        created_utc_ts=cur_created,
                        target_utc_ts=cur_target,
                        label=label,
                        tz_name=tz_name,
                        repeat_mode=repeat_mode,
                        notify_user_id=cur_notify_user,
                    )
                    last_rendered_text = ""
                    await asyncio.sleep(1.5)
                    continue

                delete_timer_db(chat_id, message_id)
                break

            new_text = render_timer_card(
                label=label,
                created_utc_ts=cur_created,
                target_utc_ts=cur_target,
                now_utc_ts=now_utc_ts,
                tz_name=tz_name,
                repeat_mode=repeat_mode,
                notify_user_id=cur_notify_user,
            )

            if new_text != last_rendered_text:
                try:
                    await bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=message_id,
                        text=new_text,
                        reply_markup=timer_controls_kb(
                            chat_id, message_id, bool(cur_notify_user)
                        ),
                    )
                    last_rendered_text = new_text
                except TelegramRetryAfter as e:
                    await asyncio.sleep(e.retry_after + 1)
                    continue
                except TelegramBadRequest as e:
                    if "message is not modified" not in e.message.lower():
                        logging.warning(f"Таймер {key} остановлен из-за ошибки: {e}")
                        delete_timer_db(chat_id, message_id)
                        break

            now_utc_ts = datetime.now(tz=ZoneInfo("UTC")).timestamp()
            remaining_sec = max(0.0, cur_target - now_utc_ts)
            sleep_for = min(
                get_sleep_interval(remaining_sec, chat_id), max(0.5, remaining_sec)
            )
            await asyncio.sleep(sleep_for)

    except asyncio.CancelledError:
        delete_timer_db(chat_id, message_id)
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=(
                    f"⏹ <b>Таймер остановлен</b>\n"
                    f"Событие: <b>{html.escape(label)}</b>"
                ),
                reply_markup=None,
            )
        except TelegramBadRequest:
            pass
        raise
    finally:
        active_timers.pop(key, None)


def spawn_timer(
    chat_id: int,
    message_id: int,
    message_thread_id: Optional[int],
    created_utc_ts: float,
    target_utc_ts: float,
    label: str,
    tz_name: str,
    repeat_mode: str = "none",
    notify_user_id: Optional[int] = None,
    persist: bool = True,
) -> None:
    if persist:
        save_timer_db(
            chat_id=chat_id,
            message_id=message_id,
            message_thread_id=message_thread_id,
            created_utc_ts=created_utc_ts,
            target_utc_ts=target_utc_ts,
            label=label,
            tz_name=tz_name,
            repeat_mode=repeat_mode,
            notify_user_id=notify_user_id,
        )

    key = (chat_id, message_id)
    old_task = active_timers.get(key)
    if old_task and not old_task.done():
        old_task.cancel()

    active_timers[key] = asyncio.create_task(
        run_dynamic_timer(
            chat_id=chat_id,
            message_id=message_id,
            message_thread_id=message_thread_id,
            created_utc_ts=created_utc_ts,
            target_utc_ts=target_utc_ts,
            label=label,
            tz_name=tz_name,
            repeat_mode=repeat_mode,
            notify_user_id=notify_user_id,
        )
    )


async def restore_timers() -> None:
    rows = load_all_timers_db()
    for (
        chat_id,
        message_id,
        message_thread_id,
        created_utc_ts,
        target_utc_ts,
        label,
        tz_name,
        repeat_mode,
        notify_user_id,
    ) in rows:
        effective_created = created_utc_ts if created_utc_ts > 0 else target_utc_ts - 3600
        spawn_timer(
            chat_id=chat_id,
            message_id=message_id,
            message_thread_id=message_thread_id,
            created_utc_ts=effective_created,
            target_utc_ts=target_utc_ts,
            label=label,
            tz_name=tz_name,
            repeat_mode=repeat_mode or "none",
            notify_user_id=notify_user_id,
            persist=False,
        )
    if rows:
        logging.info(f"Восстановлено таймеров из БД: {len(rows)}")


async def create_and_start_timer(
    source_message: Message,
    created_utc_ts: float,
    target_utc_ts: float,
    label: str,
    tz_name: str,
    repeat_mode: str = "none",
    notify_user_id: Optional[int] = None,
) -> None:
    now_utc_ts = datetime.now(tz=ZoneInfo("UTC")).timestamp()
    initial_card = render_timer_card(
        label=label,
        created_utc_ts=created_utc_ts,
        target_utc_ts=target_utc_ts,
        now_utc_ts=now_utc_ts,
        tz_name=tz_name,
        repeat_mode=repeat_mode,
        notify_user_id=notify_user_id,
    )
    thread_id = source_message.message_thread_id
    sent = await source_message.answer(initial_card, message_thread_id=thread_id)
    await sent.edit_reply_markup(
        reply_markup=timer_controls_kb(
            sent.chat.id, sent.message_id, bool(notify_user_id)
        )
    )

    spawn_timer(
        chat_id=sent.chat.id,
        message_id=sent.message_id,
        message_thread_id=thread_id,
        created_utc_ts=created_utc_ts,
        target_utc_ts=target_utc_ts,
        label=label,
        tz_name=tz_name,
        repeat_mode=repeat_mode,
        notify_user_id=notify_user_id,
        persist=True,
    )


async def process_timer_text(
    message: Message,
    raw_text: str,
    strict_group_parse: bool = False,
) -> bool:
    tz_name = get_chat_timezone_name(message.chat.id)
    tz = load_tz(tz_name)

    target_local, label, repeat_mode = parse_target_datetime(raw_text, tz)
    if not target_local:
        if strict_group_parse:
            await message.answer(
                "🤔 Не удалось распознать дату и время.\n"
                "Формат: <code>ДД.ММ.ГГГГ ЧЧ:ММ [Название] [каждый день | каждую неделю]</code>\n"
                "Пример: <code>31.12.2026 23:59 Новый год</code>",
                message_thread_id=message.message_thread_id,
            )
            return True
        if not is_group_chat(message):
            await message.answer(
                "🤔 Не удалось распознать дату и время.\n\n"
                "Введите в формате:\n"
                "<code>ДД.ММ.ГГГГ ЧЧ:ММ [Название] [каждый день | каждую неделю]</code>\n"
                "Например: <code>31.12.2026 23:59 Новый год</code>\n\n"
                "Или воспользуйтесь быстрыми кнопками:",
                reply_markup=quick_presets_kb(),
            )
            return True
        return False

    now_local = datetime.now(tz=tz)
    if target_local <= now_local:
        await message.answer(
            f"⚠️ Время <code>{target_local.strftime('%d.%m.%Y %H:%M')}</code> "
            f"(<code>{html.escape(tz_name)}</code>) уже прошло. Укажите момент в будущем.",
            message_thread_id=message.message_thread_id,
        )
        return True

    notify_uid = message.from_user.id if message.from_user else None

    await create_and_start_timer(
        source_message=message,
        created_utc_ts=now_local.astimezone(ZoneInfo("UTC")).timestamp(),
        target_utc_ts=target_local.astimezone(ZoneInfo("UTC")).timestamp(),
        label=label,
        tz_name=tz_name,
        repeat_mode=repeat_mode,
        notify_user_id=notify_uid,
    )
    return True


# --- Хендлеры команд и кнопок ---


@dp.message(CommandStart())
async def cmd_start(message: Message) -> None:
    tz_name = get_chat_timezone_name(message.chat.id)
    await message.answer(
        "⏱ <b>Динамический таймер</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Работает в личных чатах, группах и топиках форумов.\n"
        "Поддерживает прогресс-бар, повторы, уведомления в ЛС, SQLite и часовые пояса.\n\n"
        f"🌍 Текущий пояс: <code>{html.escape(tz_name)}</code>\n\n"
        "<b>Примеры:</b>\n"
        "• <code>31.12.2026 23:59 Новый год</code>\n"
        "• <code>/timer 25.12.2026 18:00 Дедлайн</code>\n"
        "• <code>/repeat 09:00 Ежедневный стендап</code>\n"
        "• <code>/repeat пн 18:30 Планёрка</code>\n\n"
        "<b>Команды:</b>\n"
        "/timer — создать таймер (удобно в группах)\n"
        "/repeat — повторяющийся таймер\n"
        "/timers — активные таймеры\n"
        "/tz — выбор часового пояса",
        message_thread_id=message.message_thread_id,
        reply_markup=quick_presets_kb(),
    )


@dp.message(Command("timer"))
async def cmd_timer(message: Message) -> None:
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer(
            "⏱ <b>Создание таймера</b>\n"
            "Пример: <code>/timer 31.12.2026 23:59 Новый год</code>\n"
            "Или выберите быстрый пресет:",
            message_thread_id=message.message_thread_id,
            reply_markup=quick_presets_kb(),
        )
        return

    await process_timer_text(message, parts[1].strip(), strict_group_parse=True)


@dp.message(Command("repeat"))
async def cmd_repeat(message: Message) -> None:
    parts = (message.text or "").split(maxsplit=1)
    tz_name = get_chat_timezone_name(message.chat.id)
    tz = load_tz(tz_name)

    if len(parts) < 2:
        await message.answer(
            "🔁 <b>Повторяющийся таймер</b>\n"
            "Примеры:\n"
            "• <code>/repeat 09:00 Ежедневная задача</code>\n"
            "• <code>/repeat пн 18:00 Еженедельная встреча</code>\n"
            "• <code>/repeat 01.12.2026 12:00 Релиз каждую неделю</code>",
            message_thread_id=message.message_thread_id,
        )
        return

    target_local, label, repeat_mode = parse_repeat_command(parts[1], tz)
    if not target_local:
        await message.answer(
            "❌ Не удалось распознать расписание.\n"
            "Пример: <code>/repeat 09:30 Зарядка</code> или <code>/repeat пт 17:00 Итоги недели</code>",
            message_thread_id=message.message_thread_id,
        )
        return

    now_local = datetime.now(tz=tz)
    notify_uid = message.from_user.id if message.from_user else None

    await create_and_start_timer(
        source_message=message,
        created_utc_ts=now_local.astimezone(ZoneInfo("UTC")).timestamp(),
        target_utc_ts=target_local.astimezone(ZoneInfo("UTC")).timestamp(),
        label=label,
        tz_name=tz_name,
        repeat_mode=repeat_mode,
        notify_user_id=notify_uid,
    )
@dp.message(Command("tz"))
async def cmd_tz(message: Message) -> None:
    parts = (message.text or "").split(maxsplit=1)
    current_tz = get_chat_timezone_name(message.chat.id)

    if len(parts) < 2:
        await message.answer(
            f"🌍 <b>Часовой пояс чата</b>\n"
            f"Текущий: <code>{html.escape(current_tz)}</code>\n\n"
            "Выберите пояс кнопкой ниже или отправьте любое название IANA, "
            "например <code>/tz Europe/Berlin</code>:",
            message_thread_id=message.message_thread_id,
            reply_markup=timezone_menu_kb(current_tz),
        )
        return

    tz_candidate = parts[1].strip()
    try:
        ZoneInfo(tz_candidate)
    except ZoneInfoNotFoundError:
        await message.answer(
            f"❌ Неизвестный часовой пояс: <code>{html.escape(tz_candidate)}</code>.\n"
            "Пример корректного значения: <code>Europe/Moscow</code>",
            message_thread_id=message.message_thread_id,
            reply_markup=timezone_menu_kb(current_tz),
        )
        return

    set_chat_timezone_name(message.chat.id, tz_candidate)
    await message.answer(
        f"✅ Часовой пояс обновлён: <code>{html.escape(tz_candidate)}</code>",
        message_thread_id=message.message_thread_id,
        reply_markup=quick_presets_kb(),
    )


@dp.message(Command("timers"))
async def cmd_timers(message: Message) -> None:
    now_utc_ts = datetime.now(tz=ZoneInfo("UTC")).timestamp()
    rows = [
        r for r in load_chat_timers_db(message.chat.id) if r[3] > now_utc_ts
    ]

    if not rows:
        await message.answer(
            "📭 В этом чате пока нет активных таймеров.\n"
            "Запустите быстрый таймер или отправьте дату и время:",
            message_thread_id=message.message_thread_id,
            reply_markup=quick_presets_kb(),
        )
        return

    tz_default = get_chat_timezone_name(message.chat.id)
    lines: list[str] = ["📋 <b>Активные таймеры</b>", "━━━━━━━━━━━━━━━━━━━━"]
    for idx, (_, _, _, target_ts, label, tz_name, repeat_mode, notify_uid) in enumerate(
        rows, start=1
    ):
        tz = load_tz(tz_name or tz_default)
        target_local = datetime.fromtimestamp(target_ts, tz=ZoneInfo("UTC")).astimezone(tz)
        rem_str = format_short_remaining(target_ts - now_utc_ts)
        badges: list[str] = []
        if repeat_mode != "none":
            badges.append(f"🔁 {REPEAT_LABELS.get(repeat_mode, repeat_mode)}")
        if notify_uid:
            badges.append("🔔 ЛС")
        badge_str = f" · {' · '.join(badges)}" if badges else ""
        lines.append(
            f"{idx}. <b>{html.escape(label)}</b> — <code>{rem_str}</code>{badge_str}\n"
            f"    🗓 <code>{target_local.strftime('%d.%m.%Y %H:%M')}</code> ({html.escape(tz_name)})"
        )

    await message.answer(
        "\n".join(lines),
        message_thread_id=message.message_thread_id,
        reply_markup=timers_list_kb(message.chat.id, rows, now_utc_ts),
    )


@dp.callback_query(F.data == "open_tz_menu")
async def on_open_tz_menu(callback: CallbackQuery) -> None:
    if not callback.message or not isinstance(callback.message, Message):
        return
    current_tz = get_chat_timezone_name(callback.message.chat.id)
    await callback.message.answer(
        f"🌍 <b>Выберите часовой пояс</b>\nТекущий: <code>{html.escape(current_tz)}</code>",
        message_thread_id=callback.message.message_thread_id,
        reply_markup=timezone_menu_kb(current_tz),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("set_tz:"))
async def on_set_tz(callback: CallbackQuery) -> None:
    if not callback.message or not callback.data:
        return
    tz_code = callback.data.split(":", maxsplit=1)[1]
    try:
        ZoneInfo(tz_code)
    except ZoneInfoNotFoundError:
        await callback.answer("Некорректный пояс", show_alert=True)
        return

    chat_id = callback.message.chat.id
    set_chat_timezone_name(chat_id, tz_code)
    try:
        await callback.message.edit_text(
            f"✅ Часовой пояс установлен: <code>{html.escape(tz_code)}</code>",
            reply_markup=timezone_menu_kb(tz_code),
        )
    except TelegramBadRequest:
        pass
    await callback.answer(f"Пояс: {tz_code}")


@dp.callback_query(F.data.startswith("preset:"))
async def on_preset(callback: CallbackQuery) -> None:
    if not callback.message or not isinstance(callback.message, Message) or not callback.data:
        return
    seconds = int(callback.data.split(":", maxsplit=1)[1])
    now_utc_ts = datetime.now(tz=ZoneInfo("UTC")).timestamp()
    target_utc_ts = now_utc_ts + seconds
    tz_name = get_chat_timezone_name(callback.message.chat.id)
    notify_uid = callback.from_user.id if callback.from_user else None

    preset_titles = {sec: title for title, sec in QUICK_PRESETS}
    label = f"Таймер {preset_titles.get(seconds, '')}".strip()

    await create_and_start_timer(
        source_message=callback.message,
        created_utc_ts=now_utc_ts,
        target_utc_ts=target_utc_ts,
        label=label,
        tz_name=tz_name,
        repeat_mode="none",
        notify_user_id=notify_uid,
    )
    await callback.answer("Таймер запущен!")


@dp.callback_query(F.data == "list_timers")
async def on_list_timers(callback: CallbackQuery) -> None:
    if not callback.message or not isinstance(callback.message, Message):
        return
    await cmd_timers(callback.message)
    await callback.answer()


@dp.callback_query(F.data.startswith("toggle_dm:"))
async def on_toggle_dm(callback: CallbackQuery) -> None:
    if not callback.data or not callback.from_user:
        return
    _, chat_id_str, message_id_str = callback.data.split(":")
    chat_id, message_id = int(chat_id_str), int(message_id_str)

    row = get_timer_db(chat_id, message_id)
    if not row:
        await callback.answer("Таймер уже завершён", show_alert=False)
        return

    _, created_ts, target_ts, label, tz_name, repeat_mode, current_notify_uid = row
    new_notify_uid = None if current_notify_uid else callback.from_user.id
    update_timer_notify_db(chat_id, message_id, new_notify_uid)

    now_utc_ts = datetime.now(tz=ZoneInfo("UTC")).timestamp()
    card_text = render_timer_card(
        label=label,
        created_utc_ts=created_ts,
        target_utc_ts=target_ts,
        now_utc_ts=now_utc_ts,
        tz_name=tz_name,
        repeat_mode=repeat_mode,
        notify_user_id=new_notify_uid,
    )
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=card_text,
            reply_markup=timer_controls_kb(chat_id, message_id, bool(new_notify_uid)),
        )
    except TelegramBadRequest:
        pass

    await callback.answer(
        "🔔 Уведомления в ЛС включены" if new_notify_uid else "🔕 Уведомления в ЛС выключены"
    )


@dp.callback_query(F.data.startswith("refresh_timer:"))
async def on_refresh_timer(callback: CallbackQuery) -> None:
    if not callback.data:
        return
    _, chat_id_str, message_id_str = callback.data.split(":")
    chat_id, message_id = int(chat_id_str), int(message_id_str)

    row = get_timer_db(chat_id, message_id)
    if not row:
        await callback.answer("Таймер уже завершён или остановлен", show_alert=False)
        return

    _, created_ts, target_ts, label, tz_name, repeat_mode, notify_uid = row
    now_utc_ts = datetime.now(tz=ZoneInfo("UTC")).timestamp()
    card_text = (
        render_done_card(label, target_ts, tz_name, repeat_mode)
        if target_ts <= now_utc_ts
        else render_timer_card(
            label=label,
            created_utc_ts=created_ts,
            target_utc_ts=target_ts,
            now_utc_ts=now_utc_ts,
            tz_name=tz_name,
            repeat_mode=repeat_mode,
            notify_user_id=notify_uid,
        )
    )
    markup = (
        None
        if target_ts <= now_utc_ts
        else timer_controls_kb(chat_id, message_id, bool(notify_uid))
    )
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=card_text,
            reply_markup=markup,
        )
        await callback.answer("Обновлено")
    except TelegramBadRequest:
        await callback.answer("Актуальные данные уже на экране")


@dp.callback_query(F.data.startswith("stop_timer:"))
async def on_stop_timer(callback: CallbackQuery) -> None:
    if not callback.data:
        return
    _, chat_id_str, message_id_str = callback.data.split(":")
    chat_id, message_id = int(chat_id_str), int(message_id_str)
    key = (chat_id, message_id)

    delete_timer_db(chat_id, message_id)
    task = active_timers.get(key)
    if task and not task.done():
        task.cancel()
        await callback.answer("Таймер остановлен")
    else:
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text="⏹ <b>Таймер остановлен</b>",
                reply_markup=None,
            )
        except TelegramBadRequest:
            pass
        await callback.answer("Таймер остановлен")


@dp.callback_query(F.data.startswith("stop_all:"))
async def on_stop_all(callback: CallbackQuery) -> None:
    if not callback.data:
        return
    chat_id = int(callback.data.split(":")[1])
    rows = load_chat_timers_db(chat_id)
    stopped = 0

    for msg_id, *_ in rows:
        delete_timer_db(chat_id, msg_id)
        task = active_timers.get((chat_id, msg_id))
        if task and not task.done():
            task.cancel()
            stopped += 1

    if callback.message:
        try:
            await callback.message.edit_text(
                f"🗑 Остановлено таймеров: <b>{max(stopped, len(rows))}</b>",
                reply_markup=None,
            )
        except TelegramBadRequest:
            pass
    await callback.answer("Все таймеры остановлены")


@dp.message(F.text)
async def handle_text_message(message: Message) -> None:
    text = (message.text or "").strip()
    if not text:
        return

    if is_group_chat(message):
        bot_username = await get_bot_username()
        mention_prefix = f"@{bot_username}" if bot_username else ""
        replied_to_bot = bool(
            message.reply_to_message
            and message.reply_to_message.from_user
            and message.reply_to_message.from_user.id == bot.id
        )

        if mention_prefix and text.lower().startswith(mention_prefix):
            payload = text[len(mention_prefix) :].strip(" ,:\n\t")
            await process_timer_text(message, payload, strict_group_parse=True)
            return

        if replied_to_bot:
            await process_timer_text(message, text, strict_group_parse=True)
            return

        await process_timer_text(message, text, strict_group_parse=False)
        return

    await process_timer_text(message, text, strict_group_parse=False)


async def main() -> None:
    init_db()
    await restore_timers()
    while True:
        try:
            await dp.start_polling(bot)
            break
        except TelegramRetryAfter as e:
            logging.warning(f"Флуд-контроль при запуске. Ждём {e.retry_after} сек...")
            await asyncio.sleep(e.retry_after + 1)


if __name__ == "__main__":
    asyncio.run(main())
