import asyncio
import json
import os

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton
from aiogram.filters import CommandStart

TOKEN = os.getenv("TOKEN")

if not TOKEN:
    raise ValueError("❌ TOKEN не найден в Render Environment Variables")

bot = Bot(token=TOKEN)
dp = Dispatcher()

# ДВА ОБЯЗАТЕЛЬНЫХ КАНАЛА
CHANNELS = {
    "@vexonova": "https://t.me/vexonova",
    "@vexonova_channel": "https://t.me/vexonova_channel"
}

ADMIN_ID = 7288303373

CODES_FILE = "/data/codes.json"
STATS_FILE = "/data/stats.json"


# СОЗДАНИЕ ФАЙЛОВ
def ensure_files():
    os.makedirs("/data", exist_ok=True)

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


# КНОПКИ
keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📢 Подпишись")],
        [KeyboardButton(text="✅ Проверить")]
    ],
    resize_keyboard=True
)


# ССЫЛКИ НА КАНАЛЫ
def channels_text():
    return (
        "📢 Наши Telegram-каналы:\n\n"
        "1️⃣ https://t.me/vexonova\n"
        "2️⃣ https://t.me/vexonova_channel"
    )


# ПРОВЕРКА ПОДПИСКИ НА ОБА КАНАЛА
async def check_sub(user_id):
    missing_channels = []
    errors = []

    for channel, link in CHANNELS.items():
        try:
            member = await bot.get_chat_member(channel, user_id)

            is_subscribed = member.status in [
                "member",
                "administrator",
                "creator"
            ]

            if member.status == "restricted":
                is_subscribed = member.is_member

            if not is_subscribed:
                missing_channels.append(link)

        except Exception as e:
            print(f"Ошибка проверки {channel}: {e}")
            errors.append(channel)

    return missing_channels, errors


# СООБЩЕНИЕ О НЕОБХОДИМОЙ ПОДПИСКЕ
async def require_subscription(message):
    missing, errors = await check_sub(message.from_user.id)

    if errors:
        await message.answer(
            "⚠️ Не удалось проверить подписку.\n"
            "Попробуй позже или сообщи администратору."
        )
        return False

    if missing:
        text = (
            "❌ Для получения названия фильма "
            "нужно подписаться на ОБА канала!\n\n"
            "Ты ещё не подписан на:\n\n"
        )

        for link in missing:
            text += f"👉 {link}\n"

        text += "\nПодпишись и нажми «✅ Проверить»."

        await message.answer(text, reply_markup=keyboard)
        return False

    return True


# КОМАНДА /start
@dp.message(CommandStart())
async def start(message: Message):
    await message.answer(
        "Привет! 👋\n\n"
        "🎬 Добро пожаловать в Vexonova!\n\n"
        "Здесь ты можешь узнать название фильма по коду.\n\n"
        "Для начала подпишись на оба канала:\n\n"
        "1️⃣ https://t.me/vexonova\n"
        "2️⃣ https://t.me/vexonova_channel\n\n"
        "✅ После подписки нажми «Проверить».\n\n"
        "🔎 Затем введи код фильма, например:\n"
        "#A7K2 или A7K2",
        reply_markup=keyboard
    )


# КНОПКА ПОДПИСКИ
@dp.message(F.text == "📢 Подпишись")
async def sub(message: Message):
    await message.answer(
        channels_text() +
        "\n\n✅ Подпишись на оба канала, "
        "затем нажми «Проверить»."
    )


# КНОПКА ПРОВЕРКИ
@dp.message(F.text == "✅ Проверить")
async def verify(message: Message):
    if await require_subscription(message):
        await message.answer(
            "✅ Отлично! Ты подписан на оба канала!\n\n"
            "🎬 Теперь можешь вводить коды фильмов.\n\n"
            "Например: #A7K2 или A7K2"
        )


# ДОБАВЛЕНИЕ ФИЛЬМА
# /add #A7K2 Титаник
@dp.message(F.text.startswith("/add"))
async def add_code(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ У тебя нет доступа")
        return

    try:
        parts = message.text.split(maxsplit=2)

        if len(parts) < 3:
            await message.answer(
                "❌ Формат: /add #A7K2 Титаник"
            )
            return

        code = normalize_code(parts[1])
        movie = parts[2].strip()

        if not code or not movie:
            await message.answer(
                "❌ Формат: /add #A7K2 Титаник"
            )
            return

        codes = load_codes()
        codes[code] = movie
        save_codes(codes)

        await message.answer(
            f"✅ Добавлено:\n"
            f"{display_code(code)} → {movie}"
        )

    except Exception as e:
        print(f"Ошибка добавления: {e}")
        await message.answer("❌ Ошибка добавления фильма")


# УДАЛЕНИЕ ФИЛЬМА
# /del #A7K2
@dp.message(F.text.startswith("/del"))
async def delete_code(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ У тебя нет доступа")
        return

    try:
        parts = message.text.split(maxsplit=1)

        if len(parts) < 2:
            await message.answer(
                "❌ Формат: /del #A7K2"
            )
            return

        code = normalize_code(parts[1])
        codes = load_codes()

        if code in codes:
            del codes[code]
            save_codes(codes)

            await message.answer(
                f"🗑 Код удалён: {display_code(code)}"
            )
        else:
            await message.answer("❌ Код не найден")

    except Exception as e:
        print(f"Ошибка удаления: {e}")
        await message.answer("❌ Ошибка удаления кода")


# СПИСОК ВСЕХ ФИЛЬМОВ
@dp.message(F.text == "/list")
async def list_codes(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ У тебя нет доступа")
        return

    codes = load_codes()

    if not codes:
        await message.answer("📭 Список кодов пуст")
        return

    lines = ["📃 Все коды:\n"]

    for code, movie in codes.items():
        lines.append(
            f"{display_code(code)} — {movie}"
        )

    text = "\n".join(lines)

    for i in range(0, len(text), 3500):
        await message.answer(text[i:i + 3500])


# СТАТИСТИКА БОТА
@dp.message(F.text == "/stats")
async def stats_command(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ У тебя нет доступа")
        return

    stats = load_stats()

    total_requests = stats.get("total_requests", 0)
    unique_users_count = len(
        stats.get("unique_users", [])
    )
    codes_stats = stats.get("codes", {})

    text = (
        "📊 Аналитика бота:\n\n"
        f"📩 Всего запросов: {total_requests}\n"
        f"👥 Уникальных пользователей: "
        f"{unique_users_count}\n\n"
    )

    if codes_stats:
        text += "🔥 Популярность кодов:\n"

        sorted_codes = sorted(
            codes_stats.items(),
            key=lambda x: x[1],
            reverse=True
        )

        for code, count in sorted_codes:
            text += (
                f"{display_code(code)} — "
                f"{count} раз(а)\n"
            )
    else:
        text += "Пока статистики по кодам нет"

    for i in range(0, len(text), 3500):
        await message.answer(text[i:i + 3500])


# ПОИСК ФИЛЬМА ПО КОДУ
@dp.message(F.text & ~F.text.startswith("/"))
async def get_code(message: Message):

    # СНАЧАЛА ПРОВЕРЯЕМ ОБЕ ПОДПИСКИ
    if not await require_subscription(message):
        return

    codes = load_codes()
    code = normalize_code(message.text)

    if code in codes:
        update_stats(message.from_user.id, code)

        await message.answer(
            f"🎬 Фильм: {codes[code]}"
        )
    else:
        await message.answer(
            "❌ Код не найден.\n"
            "Проверь правильность кода и попробуй снова."
        )


# ЗАПУСК БОТА
async def main():
    ensure_files()
    print("🚀 Vexonova Bot запущен!")
    print("✅ Проверка подписки на 2 канала активна")

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
