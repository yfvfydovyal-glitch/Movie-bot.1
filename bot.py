import asyncio
import json
import os
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton
from aiogram.filters import CommandStart

TOKEN = os.getenv("TOKEN")

if not TOKEN:
    raise ValueError("❌ TOKEN не знайдено в Render Environment Variables")

bot = Bot(token=TOKEN)
dp = Dispatcher()

CHANNEL = "@vexonova"
ADMIN_ID =  7288303373  # ВСТАВ СВІЙ ЧИСЛОВИЙ ID
 
CODES_FILE = "/data/codes.json"
STATS_FILE = "/data/stats.json"

def ensure_files():
    if not os.path.exists(CODES_FILE):
        with open(CODES_FILE, "w", encoding="utf-8") as f:
            json.dump({}, f, ensure_ascii=False, indent=4)

    if not os.path.exists(STATS_FILE):
        with open(STATS_FILE, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "total_requests": 0,
                    "unique_users": [],
                    "codes": {}
                },
                f,
                ensure_ascii=False,
                indent=4
            )


def load_codes():
    with open(CODES_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_codes(codes):
    with open(CODES_FILE, "w", encoding="utf-8") as f:
        json.dump(codes, f, ensure_ascii=False, indent=4)


def load_stats():
    with open(STATS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_stats(stats):
    with open(STATS_FILE, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=4)


def normalize_code(raw_code: str) -> str:
    code = raw_code.strip().upper()
    if code.startswith("#"):
        code = code[1:]
    return code


def display_code(raw_code: str) -> str:
    return f"#{normalize_code(raw_code)}"


def update_stats(user_id: int, code: str):
    stats = load_stats()

    stats["total_requests"] += 1

    if user_id not in stats["unique_users"]:
        stats["unique_users"].append(user_id)

    if code not in stats["codes"]:
        stats["codes"][code] = 0

    stats["codes"][code] += 1
    save_stats(stats)


keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📢 Підписатися")],
        [KeyboardButton(text="✅ Перевірити")]
    ],
    resize_keyboard=True
)


async def check_sub(user_id):
    try:
        member = await bot.get_chat_member(CHANNEL, user_id)
        return member.status in ["member", "administrator", "creator"]
    except Exception:
        return False


@dp.message(CommandStart())
async def start(message: Message):
    await message.answer(
        f"Привіт 👋\n\n"
        f"Підпишись на канал {CHANNEL}\n"
        f"Потім натисни 'Перевірити'\n"
        f"Після цього введи код, наприклад: #1022 або 1022",
        reply_markup=keyboard
    )


@dp.message(F.text == "📢 Підписатися")
async def sub(message: Message):
    await message.answer(f"Ось канал 👉 https://t.me/{CHANNEL.replace('@', '')}")


@dp.message(F.text == "✅ Перевірити")
async def verify(message: Message):
    if await check_sub(message.from_user.id):
        await message.answer("✅ Ти підписаний! Тепер введи код, наприклад: #1022 або 1022")
    else:
        await message.answer("❌ Ти ще не підписаний!")


# /add #1022 Тітанік
# /add 1022 Тітанік
@dp.message(F.text.startswith("/add"))
async def add_code(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ У тебе немає доступу")
        return

    try:
        parts = message.text.split(maxsplit=2)
        if len(parts) < 3:
            await message.answer("❌ Формат: /add #1022 Тітанік")
            return

        code = normalize_code(parts[1])
        movie = parts[2].strip()

        codes = load_codes()
        codes[code] = movie
        save_codes(codes)

        await message.answer(f"✅ Додано: {display_code(code)} → {movie}")
    except Exception:
        await message.answer("❌ Формат: /add #1022 Тітанік")


# /del #1022
# /del 1022
@dp.message(F.text.startswith("/del"))
async def delete_code(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ У тебе немає доступу")
        return

    try:
        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            await message.answer("❌ Формат: /del #1022")
            return

        code = normalize_code(parts[1])

        codes = load_codes()

        if code in codes:
            del codes[code]
            save_codes(codes)
            await message.answer(f"🗑 Видалено код: {display_code(code)}")
        else:
            await message.answer("❌ Код не знайдено")
    except Exception:
        await message.answer("❌ Формат: /del #1022")


@dp.message(F.text == "/list")
async def list_codes(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ У тебе немає доступу")
        return

    codes = load_codes()

    if not codes:
        await message.answer("📭 Список кодів порожній")
        return

    lines = ["📃 Всі коди:\n"]
    for code, movie in codes.items():
        lines.append(f"{display_code(code)} — {movie}")

    text = "\n".join(lines)

    for i in range(0, len(text), 3500):
        await message.answer(text[i:i + 3500])


@dp.message(F.text == "/stats")
async def stats_command(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ У тебе немає доступу")
        return

    stats = load_stats()

    total_requests = stats.get("total_requests", 0)
    unique_users_count = len(stats.get("unique_users", []))
    codes_stats = stats.get("codes", {})

    text = (
        f"📊 Аналітика:\n\n"
        f"Всього запитів: {total_requests}\n"
        f"Унікальних користувачів: {unique_users_count}\n\n"
    )

    if codes_stats:
        text += "🔥 Популярність кодів:\n"
        sorted_codes = sorted(codes_stats.items(), key=lambda x: x[1], reverse=True)
        for code, count in sorted_codes:
            text += f"{display_code(code)} — {count} раз(ів)\n"
    else:
        text += "Поки що статистики по кодах немає"

    for i in range(0, len(text), 3500):
        await message.answer(text[i:i + 3500])


@dp.message(F.text & ~F.text.startswith("/"))
async def get_code(message: Message):
    codes = load_codes()

    if not await check_sub(message.from_user.id):
        await message.answer("❌ Спочатку підпишись!")
        return

    code = normalize_code(message.text)

    if code in codes:
        update_stats(message.from_user.id, code)
        await message.answer(f"🎬 Фільм: {codes[code]}")
    else:
        await message.answer("❌ Код не знайдено")


async def main():
    ensure_files()
    print("Бот з адмінкою та аналітикою працює 🚀")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
