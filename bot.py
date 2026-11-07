"""Vexonova: movie catalog bot, aiogram 3.x.

Required: TOKEN. Optional: GOOGLE_SHEET_ID, GOOGLE_CREDENTIALS_JSON or
GOOGLE_CREDENTIALS_FILE, GOOGLE_WORKSHEET, DATA_DIR, ADMIN_ID.

Sheet columns (first row, exact English headers):
code | title | category | year | rating | description

The bot stores all data locally in SQLite and imports old codes.json/stats.json
once, without deleting the originals. Sheet sync is optional.
"""
import asyncio
import hashlib
import json
import logging
import os
import random
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, Message, ReplyKeyboardMarkup, ReplyKeyboardRemove,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("vexonova")

TOKEN = os.getenv("TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "7288303373"))
DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "").strip()
WORKSHEET = os.getenv("GOOGLE_WORKSHEET", "Movies").strip() or "Movies"
SYNC_SECONDS = max(60, int(os.getenv("SYNC_SECONDS", "300")))
CHANNELS = [
    ("@vexonova", "https://t.me/vexonova"),
    ("@vexonova_channel", "https://t.me/vexonova_channel"),
]
HEADERS = ["code", "title", "category", "year", "rating", "description"]
CODE_RE = re.compile(r"^[A-Z0-9]{4}$")

if not TOKEN:
    raise RuntimeError("Укажи TOKEN в Render Environment Variables")

DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "vexonova.sqlite3"
DB = sqlite3.connect(DB_PATH, timeout=30)
DB.row_factory = sqlite3.Row
DB.execute("PRAGMA foreign_keys=ON")
DB.execute("PRAGMA journal_mode=WAL")

bot = Bot(token=TOKEN)
dp = Dispatcher()
sheet_lock = asyncio.Lock()
pending_broadcast = {}
broadcast_lock = asyncio.Lock()


class UserStates(StatesGroup):
    search = State()


class AdminStates(StatesGroup):
    add = State()
    edit = State()
    delete = State()
    broadcast = State()


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_code(value):
    return str(value).strip().upper().lstrip("#").strip()


def valid_code(value):
    return bool(CODE_RE.fullmatch(normalize_code(value)))


def clip(text, size=60):
    text = str(text)
    return text if len(text) <= size else text[:size-1] + "…"


def send_lines(lines, max_size=3500):
    """Split message on newline boundaries, without splitting a movie name."""
    chunks, part = [], ""
    for line in lines:
        if len(line) > max_size:
            line = line[:max_size]
        if len(part) + len(line) + 1 > max_size:
            if part:
                chunks.append(part)
            part = ""
        part += ("\n" if part else "") + line
    if part:
        chunks.append(part)
    return chunks


def meta_get(key, default="0"):
    row = DB.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def meta_set(key, value):
    DB.execute("INSERT INTO meta(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def init_db():
    with DB:
        DB.executescript("""
        CREATE TABLE IF NOT EXISTS movies (
            code TEXT PRIMARY KEY, title TEXT NOT NULL, category TEXT NOT NULL DEFAULT 'Без жанра',
            year TEXT NOT NULL DEFAULT '', rating REAL, description TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT 'local', updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, first_seen TEXT, last_seen TEXT, news_optin INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS favorites (
            user_id INTEGER NOT NULL, code TEXT NOT NULL,
            PRIMARY KEY(user_id, code), FOREIGN KEY(code) REFERENCES movies(code) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS movie_views (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            code TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_movie_views_time ON movie_views(created_at);
        CREATE TABLE IF NOT EXISTS code_counts (code TEXT PRIMARY KEY, views INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
    if meta_get("legacy_imported", "") == "yes":
        return

    codes_path = DATA_DIR / "codes.json"
    stats_path = DATA_DIR / "stats.json"
    try:
        codes = json.loads(codes_path.read_text(encoding="utf-8")) if codes_path.exists() else {}
        stats = json.loads(stats_path.read_text(encoding="utf-8")) if stats_path.exists() else {}
        if not isinstance(codes, dict) or not isinstance(stats, dict):
            raise ValueError("Invalid legacy JSON structure")
        with DB:
            for code, movie in codes.items():
                c = normalize_code(code)
                if c and isinstance(movie, str) and movie.strip():
                    DB.execute("""INSERT OR IGNORE INTO movies
                        (code,title,category,year,rating,description,source,updated_at)
                        VALUES (?,?,?,?,?,?,?,?)""",
                        (c, movie.strip(), "Без жанра", "", None, "", "local", utc_now()))
            for uid in stats.get("unique_users", []):
                if str(uid).isdigit():
                    DB.execute("INSERT OR IGNORE INTO users(user_id) VALUES (?)", (int(uid),))
            for c, cnt in stats.get("codes", {}).items():
                if isinstance(cnt, int) and cnt >= 0:
                    DB.execute("INSERT OR IGNORE INTO code_counts(code,views) VALUES (?,?)",
                               (normalize_code(c), cnt))
            meta_set("total_views", max(0, int(stats.get("total_requests", 0))))
            meta_set("legacy_imported", "yes")
        log.info("Imported existing JSON: %s movies", len(codes))
    except (ValueError, TypeError, OSError, json.JSONDecodeError):
        log.exception("Cannot read old JSON. Original files are untouched; import will retry on restart.")


def touch_user(user_id):
    now = utc_now()
    with DB:
        DB.execute("""INSERT INTO users (user_id,first_seen,last_seen) VALUES (?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
            first_seen=COALESCE(users.first_seen, excluded.first_seen), last_seen=excluded.last_seen""",
            (user_id, now, now))


def set_optin(user_id, enabled):
    touch_user(user_id)
    with DB:
        DB.execute("UPDATE users SET news_optin=? WHERE user_id=?", (int(enabled), user_id))


def is_optin(user_id):
    row = DB.execute("SELECT news_optin FROM users WHERE user_id=?", (user_id,)).fetchone()
    return bool(row and row[0])


def row_to_movie(row):
    return dict(row) if row else None


def get_movie(code):
    return row_to_movie(DB.execute("SELECT * FROM movies WHERE code=?", (normalize_code(code),)).fetchone())


def upsert_movie(movie, source="local"):
    DB.execute("""INSERT INTO movies (code,title,category,year,rating,description,source,updated_at)
            VALUES (:code,:title,:category,:year,:rating,:description,:source,:updated_at)
            ON CONFLICT(code) DO UPDATE SET
                title=excluded.title, category=excluded.category, year=excluded.year,
                rating=excluded.rating, description=excluded.description,
                source=excluded.source, updated_at=excluded.updated_at""",
            {**movie, "source": source, "updated_at": utc_now()})


def remove_movie(code):
    with DB:
        DB.execute("DELETE FROM movies WHERE code=?", (normalize_code(code),))


def record_view(user_id, code):
    with DB:
        DB.execute("INSERT INTO movie_views(user_id,code,created_at) VALUES (?,?,?)",
                   (user_id, code, utc_now()))
        DB.execute("""INSERT INTO code_counts(code,views) VALUES (?,1)
            ON CONFLICT(code) DO UPDATE SET views=views+1""", (code,))
        meta_set("total_views", int(meta_get("total_views")) + 1)


def favorites_for(user_id):
    return [dict(r) for r in DB.execute("""SELECT m.* FROM favorites f
        JOIN movies m ON m.code=f.code WHERE f.user_id=? ORDER BY m.title LIMIT 50""", (user_id,))]


def toggle_favorite(user_id, code):
    with DB:
        exists = DB.execute("SELECT 1 FROM favorites WHERE user_id=? AND code=?", (user_id, code)).fetchone()
        if exists:
            DB.execute("DELETE FROM favorites WHERE user_id=? AND code=?", (user_id, code))
            return False
        DB.execute("INSERT INTO favorites(user_id,code) VALUES (?,?)", (user_id, code))
        return True


def has_favorite(user_id, code):
    return DB.execute("SELECT 1 FROM favorites WHERE user_id=? AND code=?", (user_id, code)).fetchone() is not None


def all_movies():
    return [dict(r) for r in DB.execute("SELECT * FROM movies ORDER BY title COLLATE NOCASE")]


def categories():
    result = {}
    for m in all_movies():
        for name in re.split(r"[,;]", m["category"] or "Без жанра"):
            name = name.strip() or "Без жанра"
            result[name] = result.get(name, 0) + 1
    return sorted(result.items(), key=lambda x: x[0].casefold())


def category_key(name):
    return hashlib.sha256(name.encode("utf-8")).hexdigest()[:12]


def menu_keyboard(admin=False):
    rows = [
        [KeyboardButton(text="🎭 Категории"), KeyboardButton(text="🔎 Поиск")],
        [KeyboardButton(text="🎲 Случайный фильм"), KeyboardButton(text="🔥 ТОП-10")],
        [KeyboardButton(text="❤️ Избранное"), KeyboardButton(text="⭐ По рейтингу")],
        [KeyboardButton(text="🔔 Новости")],
    ]
    if admin:
        rows.append([KeyboardButton(text="⚙️ Админ-панель")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def sub_keyboard(missing):
    builder = InlineKeyboardBuilder()
    # Only unsubscribed channels remain as buttons; no channel names are shown.
    for i, (_, url) in enumerate(CHANNELS, start=1):
        if url in missing:
            builder.row(InlineKeyboardButton(text=f"📢 Подписаться {i}", url=url))
    builder.row(InlineKeyboardButton(text="✅ Проверить подписку", callback_data="sub:check"))
    return builder.as_markup()


async def check_subs(user_id):
    missing, errors = [], []
    for channel, url in CHANNELS:
        try:
            member = await bot.get_chat_member(channel, user_id)
            status = getattr(member.status, "value", member.status)
            # Aiogram can return enum values (which subclass str).
            is_member = status in ("member", "administrator", "creator") or (
                status == "restricted" and bool(getattr(member, "is_member", False))
            )
            if not is_member:
                missing.append(url)
        except TelegramAPIError as e:
            log.error("Subscription check failed for %s: %s", channel, e)
            errors.append(channel)
        except Exception:
            log.exception("Unexpected subscription error for %s", channel)
            errors.append(channel)
    return missing, errors


async def ensure_subscribed(message, user_id):
    missing, errors = await check_subs(user_id)
    if errors:
        await message.answer(
            "⚠️ Не удалось проверить подписку. Попробуй позже.\n"
            "Если проблема повторяется, сообщи администратору.",
            reply_markup=sub_keyboard([url for _, url in CHANNELS]),
        )
        return False
    if missing:
        await message.answer(
            "🔒 Подпишись на оба канала, чтобы открыть каталог.\n"
            "Уже подтверждённые подписки не отображаются ниже:",
            reply_markup=sub_keyboard(missing),
        )
        return False
    return True


async def post_welcome(message, user_id):
    touch_user(user_id)
    missing, errors = await check_subs(user_id)
    if errors or missing:
        await message.answer(
            "🎬 Добро пожаловать в Vexonova!\n\n"
            "Подпишись на оба канала и нажми «✅ Проверить подписку».\n"
            "После этого можешь искать фильмы и вводить код, например #A7K2.",
            reply_markup=ReplyKeyboardRemove(),
        )
        await message.answer(
            "⚠️ Проверка временно недоступна. Сообщи администратору." if errors else "👇 Подписка:",
            reply_markup=sub_keyboard([u for _, u in CHANNELS] if errors else missing),
        )
    else:
        await message.answer("✅ Подписки подтверждены! Выбирай фильм 👇", reply_markup=menu_keyboard(user_id == ADMIN_ID))


def movie_markup(user_id, code):
    b = InlineKeyboardBuilder()
    b.button(text="💔 Убрать из избранного" if has_favorite(user_id, code) else "❤️ В избранное",
             callback_data=f"fav:{code}")
    return b.as_markup()


def movie_text(m):
    lines = [f"🎬 {m['title']}", f"🔑 Код: #{m['code']}"]
    if m.get("category"):
        lines.append(f"🎭 Жанр: {m['category']}")
    if m.get("year"):
        lines.append(f"📅 Год: {m['year']}")
    if m.get("rating") is not None:
        lines.append(f"⭐ Рейтинг: {m['rating']:g}/10")
    if m.get("description"):
        lines.append("\n" + clip(m["description"], 1800))
    return "\n".join(lines)


async def show_movie(message, user_id, code):
    movie = get_movie(code)
    if not movie:
        await message.answer("❌ Код не найден.")
        return
    record_view(user_id, movie["code"])
    await message.answer(movie_text(movie), reply_markup=movie_markup(user_id, movie["code"]))


def movie_list_markup(movies):
    builder = InlineKeyboardBuilder()
    for m in movies:
        builder.row(InlineKeyboardButton(text=f"🎬 {clip(m['title'], 43)} · #{m['code']}", callback_data=f"mv:{m['code']}"))
    return builder.as_markup()


async def send_category_list(message):
    cats = categories()
    if not cats:
        await message.answer("📭 Пока нет фильмов. Администратор скоро добавит каталог.")
        return
    b = InlineKeyboardBuilder()
    for name, count in cats[:90]:
        b.row(InlineKeyboardButton(text=f"🎭 {clip(name, 42)} ({count})", callback_data=f"cat:{category_key(name)}:0"))
    await message.answer("🎭 Выбери жанр:", reply_markup=b.as_markup())


def format_movie_input(text):
    """CODE title | genre | year | rating | description; all but title optional."""
    pieces = [p.strip() for p in text.strip().split("|", maxsplit=4)]
    first = pieces[0].split(maxsplit=1)
    if len(first) != 2 or not valid_code(first[0]) or not first[1].strip():
        raise ValueError("Нужно: CODE Название | жанр | год | рейтинг | описание")
    code, title = normalize_code(first[0]), first[1].strip()
    category = pieces[1] if len(pieces) >= 2 and pieces[1] else "Без жанра"
    year = pieces[2] if len(pieces) >= 3 else ""
    rating_txt = pieces[3].replace(",", ".") if len(pieces) >= 4 else ""
    description = pieces[4] if len(pieces) >= 5 else ""
    if year and (not year.isdigit() or not 1888 <= int(year) <= 2100):
        raise ValueError("Год должен быть между 1888 и 2100")
    rating = None
    if rating_txt:
        try:
            rating = float(rating_txt)
        except ValueError as e:
            raise ValueError("Рейтинг должен быть числом от 0 до 10") from e
        if not 0 <= rating <= 10:
            raise ValueError("Рейтинг должен быть от 0 до 10")
    if len(title) > 250 or len(category) > 150 or len(description) > 2000:
        raise ValueError("Слишком длинное название, жанр или описание")
    return dict(code=code, title=title, category=category, year=year,
                rating=rating, description=description)


def sheet_enabled():
    return bool(SHEET_ID)


def get_worksheet():
    """Runs in a thread: gspread is synchronous."""
    import gspread
    credentials_json = os.getenv("GOOGLE_CREDENTIALS_JSON", "").strip()
    credentials_file = os.getenv("GOOGLE_CREDENTIALS_FILE", "").strip()
    if credentials_json:
        client = gspread.service_account_from_dict(json.loads(credentials_json))
    elif credentials_file:
        client = gspread.service_account(filename=credentials_file)
    else:
        raise RuntimeError("Set GOOGLE_CREDENTIALS_JSON or GOOGLE_CREDENTIALS_FILE")
    spreadsheet = client.open_by_key(SHEET_ID)
    try:
        ws = spreadsheet.worksheet(WORKSHEET)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=WORKSHEET, rows=1000, cols=6)
    return ws


def validated_sheet_rows(ws):
    rows = ws.get_all_values()
    if not rows:
        ws.update([HEADERS], "A1:F1")
        return []
    head = [str(x).strip().lower() for x in rows[0][:6]]
    if head != HEADERS:
        raise ValueError("Google Sheet columns must be: " + ", ".join(HEADERS))
    return rows


def load_sheet_snapshot():
    ws = get_worksheet()
    rows = validated_sheet_rows(ws)
    movies = {}
    for row_no, cells in enumerate(rows[1:], start=2):
        vals = (cells + [""] * 6)[:6]
        code = normalize_code(vals[0])
        title = str(vals[1]).strip()
        if not code and not title:
            continue
        if not valid_code(code) or not title:
            log.warning("Invalid sheet row %s: must contain four-character code and title", row_no)
            continue
        if code in movies:
            raise ValueError(f"Duplicate code in sheet: {code} (row {row_no})")
        category = str(vals[2]).strip() or "Без жанра"
        year = str(vals[3]).strip()
        rating_text = str(vals[4]).strip().replace(",", ".")
        rating = None
        if rating_text:
            try:
                rating = float(rating_text)
            except ValueError:
                log.warning("Bad rating at sheet row %s", row_no)
        if rating is not None and not 0 <= rating <= 10:
            rating = None
        movies[code] = dict(code=code, title=clip(title, 250),
                            category=clip(category, 150), year=year if year.isdigit() else "",
                            rating=rating, description=clip(vals[5], 2000))
    return movies


def write_sheet_movie(movie=None, delete_code=None):
    """Writes one record to Google Sheets (in a thread)."""
    ws = get_worksheet()
    rows = validated_sheet_rows(ws)
    code = normalize_code(delete_code if delete_code is not None else movie["code"])
    row_num = next((idx for idx, row in enumerate(rows[1:], 2)
                    if row and normalize_code(row[0]) == code), None)
    if delete_code is not None:
        if row_num is not None:
            ws.delete_rows(row_num)
        return
    values = [movie["code"], movie["title"], movie["category"], movie["year"],
              "" if movie["rating"] is None else movie["rating"], movie["description"]]
    if row_num is None:
        ws.append_row(values, value_input_option="RAW")
    else:
        ws.update([values], f"A{row_num}:F{row_num}")


async def sync_sheet():
    if not sheet_enabled():
        return "Google Таблица не подключена."
    async with sheet_lock:
        snapshot = await asyncio.to_thread(load_sheet_snapshot)
        with DB:
            existing_sheet_codes = {
                r[0] for r in DB.execute("SELECT code FROM movies WHERE source='sheet'")
            }
            for m in snapshot.values():
                upsert_movie(m, source="sheet")
            for stale in existing_sheet_codes - snapshot.keys():
                DB.execute("DELETE FROM movies WHERE code=?", (stale,))
        return f"✅ Загружено {len(snapshot)} фильмов из Google Таблицы."


async def save_movie(movie):
    async with sheet_lock:
        if sheet_enabled():
            await asyncio.to_thread(write_sheet_movie, movie)
        with DB:
            upsert_movie(movie, source="sheet" if sheet_enabled() else "local")


async def delete_movie(code):
    async with sheet_lock:
        if sheet_enabled():
            await asyncio.to_thread(write_sheet_movie, None, code)
        remove_movie(code)


async def periodic_sync():
    while True:
        await asyncio.sleep(SYNC_SECONDS)
        if sheet_enabled():
            try:
                log.info(await sync_sheet())
            except Exception:
                log.exception("Google Sheet synchronization failed; local data remains available")


async def broadcast_news(text):
    users = DB.execute("SELECT user_id FROM users WHERE news_optin=1").fetchall()
    sent = failed = 0
    markup = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔕 Отписаться от новостей", callback_data="news:off")
    ]])
    for row in users:
        uid = row[0]
        try:
            await bot.send_message(uid, "📢 Новости Vexonova\n\n" + text, reply_markup=markup)
            sent += 1
        except TelegramRetryAfter as ex:
            await asyncio.sleep(ex.retry_after + 1)
            try:
                await bot.send_message(uid, "📢 Новости Vexonova\n\n" + text, reply_markup=markup)
                sent += 1
            except TelegramAPIError:
                failed += 1
        except TelegramForbiddenError:
            set_optin(uid, False)
            failed += 1
        except TelegramAPIError:
            failed += 1
            log.exception("Failed to send newsletter to %s", uid)
        await asyncio.sleep(0.09)
    return f"📢 Рассылка завершена. Доставлено: {sent}. Ошибок: {failed}."


async def show_stats(message):
    movies = DB.execute("SELECT COUNT(*) FROM movies").fetchone()[0]
    users = DB.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    subscribers = DB.execute("SELECT COUNT(*) FROM users WHERE news_optin=1").fetchone()[0]
    since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds")
    new_users = DB.execute("SELECT COUNT(*) FROM users WHERE first_seen>=?", (since,)).fetchone()[0]
    views_week = DB.execute("SELECT COUNT(*) FROM movie_views WHERE created_at>=?", (since,)).fetchone()[0]
    days = DB.execute("""SELECT substr(created_at,1,10) as day, count(*) as cnt
        FROM movie_views WHERE created_at>=? GROUP BY day ORDER BY day""", (since,)).fetchall()
    popular = DB.execute("""SELECT m.title, c.views FROM code_counts c
        JOIN movies m ON m.code=c.code ORDER BY c.views DESC LIMIT 10""").fetchall()
    cat_views = {}
    for row in DB.execute("""SELECT m.category, COUNT(*) n FROM movie_views v
        JOIN movies m ON m.code=v.code GROUP BY m.category"""):
        for cat in re.split(r"[,;]", row["category"]):
            cat = cat.strip() or "Без жанра"
            cat_views[cat] = cat_views.get(cat, 0) + row["n"]
    lines = [
        "📊 Расширенная статистика Vexonova", "",
        f"🎬 Фильмов в каталоге: {movies}",
        f"👥 Пользователей: {users}",
        f"🔔 Подписались на новости: {subscribers}",
        f"👀 Просмотров фильмов за всё время: {meta_get('total_views')}",
        f"🆕 Новых пользователей за 7 дней: {new_users}",
        f"📈 Просмотров за 7 дней: {views_week}",
        "", "📅 Просмотры за 7 дней (UTC):",
    ]
    lines += [f"{r['day']}: {r['cnt']}" for r in days] or ["Нет данных"]
    lines += ["", "🔥 Самые популярные фильмы:"]
    lines += [f"{i}. {clip(r['title'], 45)} — {r['views']}" for i, r in enumerate(popular, 1)] or ["Нет просмотров"]
    lines += ["", "🎭 Популярные жанры:"]
    lines += [f"{cat}: {n}" for cat, n in sorted(cat_views.items(), key=lambda x: -x[1])[:10]] or ["Нет данных"]
    for chunk in send_lines(lines):
        await message.answer(chunk)


def admin_panel():
    b = InlineKeyboardBuilder()
    actions = [
        ("➕ Добавить", "adm:add"), ("✏️ Изменить", "adm:edit"),
        ("🗑 Удалить", "adm:delete"), ("📃 Список", "adm:list"),
        ("📊 Статистика", "adm:stats"), ("🔄 Синхронизация", "adm:sync"),
        ("📣 Рассылка", "adm:broadcast"),
    ]
    for title, data in actions:
        b.button(text=title, callback_data=data)
    b.adjust(2)
    return b.as_markup()


async def do_add(message, text, mode="add"):
    try:
        movie = format_movie_input(text)
        previous = get_movie(movie["code"])
        if mode == "add" and previous:
            raise ValueError("Код уже существует. Используй /edit для изменения.")
        if mode == "edit" and not previous:
            raise ValueError("Код не найден. Используй /add для нового фильма.")
        if mode == "edit" and "|" not in text:
            for key in ("category", "year", "rating", "description"):
                movie[key] = previous[key]
        await save_movie(movie)
    except ValueError as e:
        await message.answer(f"❌ {e}")
        return False
    except Exception:
        log.exception("Could not add movie")
        await message.answer("❌ Ошибка сохранения. Проверь доступ к Google Таблице и Render Logs.")
        return False
    await message.answer(f"✅ Сохранено: #{movie['code']} — {movie['title']}")
    return True


async def do_delete(message, code):
    code = normalize_code(code)
    if not get_movie(code):
        await message.answer("❌ Код не найден")
        return False
    try:
        await delete_movie(code)
    except Exception:
        log.exception("Could not delete movie")
        await message.answer("❌ Ошибка удаления из Google Таблицы. Локальные данные не изменены.")
        return False
    await message.answer(f"🗑 Удалён код #{code}")
    return True


@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await post_welcome(message, message.from_user.id)


@dp.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    pending_broadcast.pop(message.from_user.id, None)
    await message.answer("✅ Действие отменено.", reply_markup=menu_keyboard(message.from_user.id == ADMIN_ID))


@dp.callback_query(F.data == "sub:check")
async def cb_check(call: CallbackQuery):
    uid = call.from_user.id
    touch_user(uid)
    missing, errors = await check_subs(uid)
    if errors:
        await call.answer("Не удалось проверить. Бот должен быть администратором обоих каналов.", show_alert=True)
        return
    if missing:
        try:
            await call.message.edit_reply_markup(reply_markup=sub_keyboard(missing))
        except TelegramBadRequest:
            pass
        await call.answer("Подпишись на оставшиеся каналы.", show_alert=True)
    else:
        try:
            await call.message.edit_text("✅ Обе подписки подтверждены!", reply_markup=None)
        except TelegramBadRequest:
            pass
        await call.answer("✅ Готово!")
        await call.message.answer("🎬 Каталог открыт! Введи код или выбери раздел 👇",
                                  reply_markup=menu_keyboard(uid == ADMIN_ID))


@dp.message(Command("admin"))
@dp.message(F.text == "⚙️ Админ-панель")
async def cmd_admin(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return await message.answer("❌ Нет доступа")
    await state.clear()
    await message.answer("⚙️ Админ-панель:", reply_markup=admin_panel())


@dp.message(Command("add"))
async def cmd_add(message: Message):
    if message.from_user.id != ADMIN_ID:
        return await message.answer("❌ Нет доступа")
    text = message.text.partition(" ")[2].strip()
    if not text:
        return await message.answer("Формат:\n/add #A7K2 Титаник | Драма | 1997 | 7.9 | Описание")
    await do_add(message, text)


@dp.message(Command("edit"))
async def cmd_edit(message: Message):
    if message.from_user.id != ADMIN_ID:
        return await message.answer("❌ Нет доступа")
    text = message.text.partition(" ")[2].strip()
    if not text:
        return await message.answer("Формат: /edit #A7K2 Новое название | Жанр | 2024 | 8.1 | Описание")
    await do_add(message, text, mode="edit")


@dp.message(Command("del"))
async def cmd_del(message: Message):
    if message.from_user.id != ADMIN_ID:
        return await message.answer("❌ Нет доступа")
    value = message.text.partition(" ")[2].strip()
    if not value:
        return await message.answer("Формат: /del #A7K2")
    await do_delete(message, value)


@dp.message(Command("list"))
async def cmd_list(message: Message):
    if message.from_user.id != ADMIN_ID:
        return await message.answer("❌ Нет доступа")
    movies = all_movies()
    if not movies:
        return await message.answer("📭 Каталог пуст")
    for chunk in send_lines(["📃 Все фильмы:", ""] +
                            [f"#{m['code']} — {m['title']} ({m['category']})" for m in movies]):
        await message.answer(chunk)


@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    if message.from_user.id != ADMIN_ID:
        return await message.answer("❌ Нет доступа")
    await show_stats(message)


@dp.message(Command("sync"))
async def cmd_sync(message: Message):
    if message.from_user.id != ADMIN_ID:
        return await message.answer("❌ Нет доступа")
    try:
        await message.answer(await sync_sheet())
    except Exception:
        log.exception("Manual sheet sync failed")
        await message.answer("❌ Ошибка Google Таблицы. Проверь доступ, названия колонок и Render Logs.")


@dp.message(Command("broadcast"))
async def cmd_broadcast(message: Message):
    if message.from_user.id != ADMIN_ID:
        return await message.answer("❌ Нет доступа")
    text = message.text.partition(" ")[2].strip()
    if not text:
        return await message.answer("Формат: /broadcast Текст новости")
    await preview_broadcast(message, text)


async def preview_broadcast(message, text):
    if len(text) > 3500:
        return await message.answer("❌ Слишком длинный текст (максимум 3500 символов)")
    pending_broadcast[message.from_user.id] = text
    b = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Отправить", callback_data="adm:send")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="adm:abort")],
    ])
    await message.answer("📣 Предпросмотр рассылки:\n\n" + text + "\n\nПодтвердить отправку?", reply_markup=b)


@dp.callback_query(F.data.startswith("adm:"))
async def cb_admin(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return await call.answer("Нет доступа", show_alert=True)
    action = call.data.split(":", 1)[1]
    await call.answer()
    if action == "add":
        await state.set_state(AdminStates.add)
        return await call.message.answer("➕ Отправь: #A7K2 Название | Жанр | Год | Рейтинг | Описание\n/cancel — отмена")
    if action == "edit":
        await state.set_state(AdminStates.edit)
        return await call.message.answer("✏️ Отправь код и новые данные:\n#A7K2 Название | Жанр | Год | Рейтинг | Описание\n/cancel — отмена")
    if action == "delete":
        await state.set_state(AdminStates.delete)
        return await call.message.answer("🗑 Отправь код фильма (например #A7K2). /cancel — отмена")
    if action == "list":
        return await admin_list_for_call(call)
    if action == "stats":
        return await show_stats(call.message)
    if action == "sync":
        try:
            return await call.message.answer(await sync_sheet())
        except Exception:
            log.exception("Sheet sync from admin panel failed")
            return await call.message.answer("❌ Не удалось синхронизировать таблицу. Смотри Render Logs.")
    if action == "broadcast":
        await state.set_state(AdminStates.broadcast)
        return await call.message.answer("📣 Отправь текст рассылки. /cancel — отмена")
    if action == "abort":
        pending_broadcast.pop(call.from_user.id, None)
        await state.clear()
        return await call.message.edit_text("❌ Рассылка отменена")
    if action == "send":
        if broadcast_lock.locked():
            return await call.message.answer("⏳ Другая рассылка уже выполняется")
        text = pending_broadcast.pop(call.from_user.id, None)
        if not text:
            return await call.message.answer("❌ Текст устарел. Создай рассылку заново.")
        try:
            await call.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        async with broadcast_lock:
            result = await broadcast_news(text)
        return await call.message.answer(result)


async def admin_list_for_call(call):
    movies = all_movies()
    if not movies:
        return await call.message.answer("📭 Каталог пуст")
    for chunk in send_lines(["📃 Все фильмы:", ""] +
                            [f"#{m['code']} — {m['title']} ({m['category']})" for m in movies]):
        await call.message.answer(chunk)


@dp.message(AdminStates.add)
async def state_add_movie(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID or not message.text:
        return
    if await do_add(message, message.text, mode="add"):
        await state.clear()


@dp.message(AdminStates.edit)
async def state_edit_movie(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID or not message.text:
        return
    if await do_add(message, message.text, mode="edit"):
        await state.clear()


@dp.message(AdminStates.delete)
async def state_delete_movie(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID or not message.text:
        return
    if await do_delete(message, message.text):
        await state.clear()


@dp.message(AdminStates.broadcast)
async def state_broadcast(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID or not message.text:
        return
    await state.clear()
    await preview_broadcast(message, message.text.strip())


@dp.message(F.text == "🎭 Категории")
async def menu_categories(message: Message):
    touch_user(message.from_user.id)
    if await ensure_subscribed(message, message.from_user.id):
        await send_category_list(message)


@dp.callback_query(F.data.startswith("cat:"))
async def cb_category(call: CallbackQuery):
    uid = call.from_user.id
    touch_user(uid)
    await call.answer()
    if not await ensure_subscribed(call.message, uid):
        return
    parts = call.data.split(":")
    if len(parts) != 3:
        return
    key = parts[1]
    try:
        page = max(0, int(parts[2]))
    except ValueError:
        return
    name = next((name for name, _ in categories() if category_key(name) == key), None)
    if name is None:
        return await call.message.answer("❌ Жанр не найден")
    movies = [m for m in all_movies() if name in [s.strip() or "Без жанра" for s in re.split(r"[,;]", m["category"])]]
    page = min(page, max(0, (len(movies)-1)//8))
    shown = movies[page*8:(page+1)*8]
    b = InlineKeyboardBuilder()
    for m in shown:
        b.row(InlineKeyboardButton(text=f"🎬 {clip(m['title'], 42)}", callback_data=f"mv:{m['code']}"))
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"cat:{key}:{page-1}"))
    if (page+1)*8 < len(movies):
        nav.append(InlineKeyboardButton(text="➡️ Дальше", callback_data=f"cat:{key}:{page+1}"))
    if nav:
        b.row(*nav)
    await call.message.answer(f"🎭 {name} · {len(movies)} фильмов (стр. {page+1})", reply_markup=b.as_markup())


@dp.message(F.text == "🔎 Поиск")
async def menu_search(message: Message, state: FSMContext):
    touch_user(message.from_user.id)
    if await ensure_subscribed(message, message.from_user.id):
        await state.set_state(UserStates.search)
        await message.answer("🔎 Введи название фильма или его часть. /cancel — отмена")


@dp.message(UserStates.search)
async def state_search(message: Message, state: FSMContext):
    if not message.text:
        return
    if not await ensure_subscribed(message, message.from_user.id):
        await state.clear()
        return
    query = message.text.strip().casefold()
    await state.clear()
    if len(query) < 2:
        return await message.answer("❌ Введи минимум 2 символа")
    found = [m for m in all_movies() if query in m["title"].casefold()][:20]
    if not found:
        return await message.answer("😕 Фильмы не найдены")
    await message.answer(f"🔎 Найдено {len(found)} фильмов (первые 20):", reply_markup=movie_list_markup(found))


@dp.message(F.text == "🎲 Случайный фильм")
async def menu_random(message: Message):
    touch_user(message.from_user.id)
    if not await ensure_subscribed(message, message.from_user.id):
        return
    row = DB.execute("SELECT code FROM movies ORDER BY RANDOM() LIMIT 1").fetchone()
    if row:
        await show_movie(message, message.from_user.id, row[0])
    else:
        await message.answer("📭 В каталоге пока нет фильмов")


@dp.message(F.text == "🔥 ТОП-10")
async def menu_top(message: Message):
    touch_user(message.from_user.id)
    if not await ensure_subscribed(message, message.from_user.id):
        return
    rows = DB.execute("""SELECT m.*, c.views FROM code_counts c
        JOIN movies m ON m.code=c.code WHERE c.views>0 ORDER BY c.views DESC LIMIT 10""").fetchall()
    if not rows:
        return await message.answer("🔥 Рейтинг популярности ещё не сформировался")
    movies = [dict(r) for r in rows]
    lines = ["🔥 ТОП-10 популярных фильмов:", ""]
    lines += [f"{i}. {m['title']} — {m['views']} просмотров" for i, m in enumerate(movies, 1)]
    await message.answer("\n".join(lines), reply_markup=movie_list_markup(movies))


@dp.message(F.text == "⭐ По рейтингу")
async def menu_ratings(message: Message):
    touch_user(message.from_user.id)
    if not await ensure_subscribed(message, message.from_user.id):
        return
    rows = DB.execute("""SELECT * FROM movies WHERE rating IS NOT NULL
        ORDER BY rating DESC, title COLLATE NOCASE LIMIT 10""").fetchall()
    if not rows:
        return await message.answer("⭐ Пока нет фильмов с рейтингами. Добавь оценки в каталоге.")
    movies = [dict(r) for r in rows]
    lines = ["⭐ ТОП фильмов по рейтингу:", ""]
    lines += [f"{i}. {m['title']} — {m['rating']:g}/10" for i, m in enumerate(movies, 1)]
    await message.answer("\n".join(lines), reply_markup=movie_list_markup(movies))


@dp.message(F.text == "❤️ Избранное")
async def menu_favorites(message: Message):
    touch_user(message.from_user.id)
    if not await ensure_subscribed(message, message.from_user.id):
        return
    movies = favorites_for(message.from_user.id)
    if movies:
        await message.answer("❤️ Твои сохранённые фильмы:", reply_markup=movie_list_markup(movies))
    else:
        await message.answer("❤️ Пока пусто. Открой фильм и нажми «В избранное».")


@dp.callback_query(F.data.startswith("mv:"))
async def cb_movie(call: CallbackQuery):
    uid = call.from_user.id
    touch_user(uid)
    await call.answer()
    if await ensure_subscribed(call.message, uid):
        await show_movie(call.message, uid, call.data[3:])


@dp.callback_query(F.data.startswith("fav:"))
async def cb_favorite(call: CallbackQuery):
    uid = call.from_user.id
    touch_user(uid)
    if not await ensure_subscribed(call.message, uid):
        return await call.answer()
    code = normalize_code(call.data[4:])
    if not get_movie(code):
        return await call.answer("Фильм удалён", show_alert=True)
    saved = toggle_favorite(uid, code)
    try:
        await call.message.edit_reply_markup(reply_markup=movie_markup(uid, code))
    except TelegramBadRequest:
        pass
    await call.answer("❤️ Добавлено в избранное" if saved else "🗑 Удалено из избранного")


@dp.message(F.text == "🔔 Новости")
async def menu_news(message: Message):
    touch_user(message.from_user.id)
    if not await ensure_subscribed(message, message.from_user.id):
        return
    subscribed = is_optin(message.from_user.id)
    b = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="🔕 Отписаться от новостей" if subscribed else "🔔 Подписаться на новости",
            callback_data="news:off" if subscribed else "news:on",
        )
    ]])
    await message.answer(
        "🔔 Получать сообщения о новых фильмах и анонсах?\n"
        "Это добровольно; отключить можно в любой момент.", reply_markup=b)


@dp.callback_query(F.data.startswith("news:"))
async def cb_news(call: CallbackQuery):
    enabled = call.data == "news:on"
    touch_user(call.from_user.id)
    set_optin(call.from_user.id, enabled)
    await call.answer("✅ Подписка на новости включена" if enabled else "🔕 Новости отключены")
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass
    await call.message.answer("✅ Новости включены" if enabled else "🔕 Новости отключены")


@dp.message(F.text & ~F.text.startswith("/"))
async def user_code(message: Message):
    touch_user(message.from_user.id)
    if not await ensure_subscribed(message, message.from_user.id):
        return
    code = normalize_code(message.text)
    if get_movie(code):
        await show_movie(message, message.from_user.id, code)
    else:
        await message.answer("❌ Код не найден. Используй меню для поиска фильма.")


@dp.message()
async def unknown_command(message: Message):
    await message.answer("Неизвестная команда. Нажми /start или выбери раздел из меню.")


async def main():
    init_db()
    task = asyncio.create_task(periodic_sync())
    try:
        if sheet_enabled():
            try:
                log.info(await sync_sheet())
            except Exception:
                log.exception("Initial Google Sheet sync failed; bot will use local database")
        log.info("Vexonova running; %s movies", DB.execute("SELECT COUNT(*) FROM movies").fetchone()[0])
        await dp.start_polling(bot)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await bot.session.close()
        DB.close()


if __name__ == "__main__":
    asyncio.run(main())
