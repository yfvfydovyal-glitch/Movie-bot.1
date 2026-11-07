import asyncio
import json
import logging
import os

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardRemove,
)

TOKEN = os.getenv("TOKEN")
if not TOKEN:
    raise ValueError("❌ TOKEN не найден в Render Environment Variables")

ADMIN_ID = 7288303373
CHANNELS = [
    ("@vexonova", "https://t.me/vexonova"),
    ("@vexonova_channel", "https://t.me/vexonova_channel"),
]

CODES_FILE = "/data/codes.json"
STATS_FILE = "/data/stats.json"

bot = Bot(token=TOKEN)
dp = Dispatcher()
logging.basicConfig(level=logging.INFO)


def ensure_files():
    os.makedirs("/data", exist_ok=True)
    if not os.path.exists(CODES_FILE):
        save_json(CODES_FILE, {})
    if not os.path.exists(STATS_FILE):
        save_json(STATS_FILE, {
            "total_requests": 0,
            "unique_users": [],
            "codes": {},
        })


def load_json(path):
    with open(path, "r", encoding="utf-8") as file:
        return json.load(file)


def save_json(path, value):
    with open(path, "w", encoding="utf-8") as file:
        json.dump(value, file, ensure_ascii=False, indent=4)


def normalize_code(value: str) -> str:
    return value.strip().lstrip("#").upper()


def display_code(value: str) -> str:
    return "#" + normalize_code(value)


def update_stats(user_id: int, code: str):
    stats = load_json(STATS_FILE)
    stats["total_requests"] += 1
    if user_id not in stats["unique_users"]:
        stats["unique_users"].append(user_id)
    stats["codes"][code] = stats["codes"].get(code, 0) + 1
    save_json(STATS_FILE, stats)


async def check_subscriptions(user_id: int):
    """Returns (not_subscribed_channels, channels_with_check_errors)."""
    missing = []
    errors = []
    for username, _ in CHANNELS:
        try:
            member = await bot.get_chat_member(username, user_id)
            subscribed = member.status in ("member", "administrator", "creator")
            if member.status == "restricted":
                subscribed = member.is_member
            if not subscribed:
                missing.append(username)
        except Exception:
            logging.exception("Ошибка проверки подписки: %s", username)
            errors.append(username)
    return missing, errors


def subscription_keyboard(missing, errors):
    """Only show buttons for channels that aren't confirmed as subscribed."""
    pending = set(missing) | set(errors)
    if not pending:
        return None

    rows = []
    for number, (username, url) in enumerate(CHANNELS, start=1):
        if username in pending:
            rows.append([
                InlineKeyboardButton(text=f"📢 Подписаться {number}", url=url)
            ])
    rows.append([
        InlineKeyboardButton(
            text="✅ Проверить подписку", callback_data="check_subscriptions"
        )
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def subscription_text(missing, errors):
    if not missing and not errors:
        return (
            "✅ Подписка на оба канала подтверждена!\n\n"
            "🎬 Теперь отправь код фильма, например #A7K2 или A7K2."
        )
    confirmed = len(CHANNELS) - len(missing) - len(errors)
    result = (
        "🎬 Vexonova\n\n"
        "📢 Чтобы узнать название фильма, подпишись на оба канала.\n"
        "Нажми кнопки ниже, затем «✅ Проверить подписку».\n\n"
        f"✅ Подтверждено подписок: {confirmed} из {len(CHANNELS)}."
    )
    if errors:
        result += (
            "\n\n⚠️ Не удалось проверить некоторые подписки. "
            "Убедись, что бот — администратор обоих каналов."
        )
    return result


async def send_subscription_screen(message: Message):
    missing, errors = await check_subscriptions(message.from_user.id)
    await message.answer(
        subscription_text(missing, errors),
        reply_markup=subscription_keyboard(missing, errors),
    )
    return not missing and not errors


@dp.message(CommandStart())
async def start(message: Message):
    # Remove the old large reply keyboard from the previous version of the bot.
    await message.answer("👋 Привет!", reply_markup=ReplyKeyboardRemove())
    await send_subscription_screen(message)


@dp.message(F.text.in_({"📢 Подпишись", "📢 Підписатися", "✅ Проверить", "✅ Перевірити"}))
async def old_buttons(message: Message):
    # Keep older Telegram keyboard buttons working, too.
    await send_subscription_screen(message)


@dp.callback_query(F.data == "check_subscriptions")
async def verify_subscriptions(callback: CallbackQuery):
    missing, errors = await check_subscriptions(callback.from_user.id)
    await callback.answer(
        "✅ Подписка подтверждена!" if not missing and not errors
        else "Подписки обновлены"
    )
    if isinstance(callback.message, Message):
        try:
            await callback.message.edit_text(
                subscription_text(missing, errors),
                reply_markup=subscription_keyboard(missing, errors),
            )
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                raise


@dp.message(Command("add"))
async def add_code(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ Нет доступа")
        return
    parts = message.text.split(maxsplit=2)
    if len(parts) < 3 or not normalize_code(parts[1]) or not parts[2].strip():
        await message.answer("❌ Формат: /add #A7K2 Титаник")
        return
    code = normalize_code(parts[1])
    movie = parts[2].strip()
    codes = load_json(CODES_FILE)
    codes[code] = movie
    save_json(CODES_FILE, codes)
    await message.answer(f"✅ Добавлено: {display_code(code)} → {movie}")


@dp.message(Command("del"))
async def delete_code(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ Нет доступа")
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not normalize_code(parts[1]):
        await message.answer("❌ Формат: /del #A7K2")
        return
    code = normalize_code(parts[1])
    codes = load_json(CODES_FILE)
    if code not in codes:
        await message.answer("❌ Код не найден")
        return
    del codes[code]
    save_json(CODES_FILE, codes)
    await message.answer(f"🗑 Удалён код: {display_code(code)}")


async def send_long_text(message: Message, lines):
    part = ""
    for line in lines:
        line += "\n"
        if len(part) + len(line) > 3500 and part:
            await message.answer(part)
            part = ""
        if len(line) > 3500:
            for i in range(0, len(line), 3500):
                if part:
                    await message.answer(part)
                    part = ""
                await message.answer(line[i:i + 3500])
        else:
            part += line
    if part:
        await message.answer(part)


@dp.message(Command("list"))
async def list_codes(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ Нет доступа")
        return
    codes = load_json(CODES_FILE)
    if not codes:
        await message.answer("📭 Список кодов пуст")
        return
    lines = ["📃 Все коды:", ""] + [
        f"{display_code(code)} — {movie}" for code, movie in codes.items()
    ]
    await send_long_text(message, lines)


@dp.message(Command("stats"))
async def stats_command(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ Нет доступа")
        return
    stats = load_json(STATS_FILE)
    lines = [
        "📊 Аналитика бота:", "",
        f"📩 Всего запросов: {stats.get('total_requests', 0)}",
        f"👥 Уникальных пользователей: {len(stats.get('unique_users', []))}",
        "",
    ]
    counts = stats.get("codes", {})
    if counts:
        lines.append("🔥 Популярность кодов:")
        for code, count in sorted(counts.items(), key=lambda x: x[1], reverse=True):
            lines.append(f"{display_code(code)} — {count} раз(а)")
    else:
        lines.append("Пока статистики по кодам нет")
    await send_long_text(message, lines)


@dp.message(F.text & ~F.text.startswith("/"))
async def get_code(message: Message):
    missing, errors = await check_subscriptions(message.from_user.id)
    if missing or errors:
        await message.answer(
            subscription_text(missing, errors),
            reply_markup=subscription_keyboard(missing, errors),
        )
        return
    code = normalize_code(message.text)
    codes = load_json(CODES_FILE)
    if code in codes:
        update_stats(message.from_user.id, code)
        await message.answer(f"🎬 Фильм: {codes[code]}")
    else:
        await message.answer("❌ Код не найден")


async def main():
    ensure_files()
    logging.info("Vexonova Bot запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
