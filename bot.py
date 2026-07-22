import os
import logging
import asyncio
from datetime import datetime, timedelta, timezone
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from aiohttp import web

# --- 1. НАСТРОЙКА ЛОГИРОВАНИЯ ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# --- 2. НАСТРОЙКИ И КОЭФФИЦИЕНТЫ ---
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", os.getenv("BOT_TOKEN", ""))
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
PORT = int(os.getenv("PORT", "8000"))

TARGET_BG = 5.5   # Целевой сахар (ммоль/л)
ISF = 2.5         # ФЧИ (Фактор чувствительности)

# Глобальные переменные для heartbeat
app_instance = None
heartbeat_running = False

def get_k1(hour: int) -> float:
    """Возвращает K1 в зависимости от времени суток"""
    if 6 <= hour < 11:
        return 1.33  # Завтрак
    elif 11 <= hour < 16:
        return 1.33  # Обед
    elif 16 <= hour < 22:
        return 1.38  # Ужин
    else:
        return 1.30  # Ночь

def get_moscow_time() -> datetime:
    """Возвращает текущее московское время (UTC+3)"""
    return datetime.now(timezone.utc) + timedelta(hours=3)

def get_period_name(hour: int) -> str:
    """Возвращает название приема пищи"""
    if 6 <= hour < 11:
        return "🌅 Завтрак"
    elif 11 <= hour < 16:
        return "🌞 Обед"
    elif 16 <= hour < 22:
        return "🌆 Ужин"
    else:
        return "🌙 Ночь"

# --- 3. ОБРАБОТЧИКИ КОМАНД ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_text = (
        "👋 **Привет, Артём и Светлана!**\n\n"
        "Я обновленный калькулятор инсулина Фиасп.\n\n"
        "📥 **Способы расчета:**\n"
        "1️⃣ **Сейчас:** `/c [СК] [Углеводы_в_г]`\n"
        "   Пример: `/c 7.6 84`\n\n"
        "2️⃣ **За прошедшее время:** `/c [СК] [Углеводы_в_г] [Время]`\n"
        "   Пример: `/c 13.1 0 02:30` (если вводите ночью задним числом)\n\n"
        "📋 **Другие команды:**\n"
        "`/help` - Справка\n"
        "`/status` - Статус бота\n\n"
        "⚙️ *Время берется МСК (UTC+3). Коэффициенты: K1 (Завтрак/Обед=1.33, Ужин=1.38, Ночь=1.30), ФЧИ=2.5*"
    )
    await update.message.reply_text(welcome_text, parse_mode='Markdown')

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "ℹ️ **СПРАВКА ПО КАЛЬКУЛЯТОРУ**\n\n"
        "🎯 **Основная команда:**\n"
        "`/c [СК] [Углеводы]` - Расчет дозы инсулина\n\n"
        "📊 **Параметры:**\n"
        "• **СК** - Уровень сахара в крови (ммоль/л)\n"
        "  Примеры: 7.6, 7,6 (запятая также поддерживается)\n\n"
        "• **Углеводы** - Количество углеводов в граммах\n"
        "  Примеры: 84, 45, 0 (для расчета только ДПС)\n\n"
        "• **Время** (опционально) - Время в формате HH:MM или H\n"
        "  Пример: `/c 13.1 0 02:30`\n"
        "  Если не указано, используется текущее МСК время\n\n"
        "📈 **Примеры использования:**\n"
        "`/c 7.6 84` - Завтрак с углеводами\n"
        "`/c 13.1 0` - Только ДПС, нет еды\n"
        "`/c 5.5 40 14:30` - Обед в 14:30\n\n"
        "⏰ **Коэффициенты по времени:**\n"
        "🌅 06:00-11:00 - K1=1.33 (Завтрак)\n"
        "🌞 11:00-16:00 - K1=1.33 (Обед)\n"
        "🌆 16:00-22:00 - K1=1.38 (Ужин)\n"
        "🌙 22:00-06:00 - K1=1.30 (Ночь)\n\n"
        "📌 **Доп. информация:**\n"
        "• Целевой СК: 5.5 ммоль/л\n"
        "• ФЧИ: 2.5 ед/(ммоль/л)\n"
        "• Дозы округляются до 0.5 единиц\n"
        "• 1 ХЕ = 12 г углеводов"
    )
    await update.message.reply_text(help_text, parse_mode='Markdown')

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msk_time = get_moscow_time()
    hour = msk_time.hour
    period = get_period_name(hour)
    k1 = get_k1(hour)
    
    status_text = (
        "🤖 **СТАТУС БОТА**\n"
        "═══════════════════════\n"
        f"✅ **Статус:** Работает\n"
        f"⏰ **Время МСК:** {msk_time.strftime('%H:%M:%S')}\n"
        f"📅 **Дата:** {msk_time.strftime('%d.%m.%Y')}\n"
        f"{period}\n"
        f"⚙️ **K1 сейчас:** {k1}\n"
        f"🎯 **Целевой СК:** {TARGET_BG} ммоль/л\n"
        f"💉 **ФЧИ:** {ISF} ед/(ммоль/л)\n"
        f"📊 **Версия:** 2.1 (с Heartbeat и Health Check)\n"
        "═══════════════════════\n"
        "💪 Готов помочь с расчетами!"
    )
    await update.message.reply_text(status_text, parse_mode='Markdown')

async def calculate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if len(context.args) < 2:
            await update.message.reply_text(
                "⚠️ **Ошибка формата!**\n"
                "Пример: `/c 7.6 84` или `/c 13.1 0 02:30`",
                parse_mode='Markdown'
            )
            return

        current_bg = float(context.args[0].replace(',', '.'))
        carbs_g = float(context.args[1].replace(',', '.'))
        xe = carbs_g / 12.0  # 1 ХЕ = 12 г

        # Определение часа (текущее МСК или переданное в параметре)
        time_str = ""
        if len(context.args) >= 3:
            time_input = context.args[2]  # Формат HH:MM или H
            try:
                parts = time_input.split(':')
                now_hour = int(parts[0])
                if now_hour < 0 or now_hour > 23:
                    raise ValueError("Час должен быть от 0 до 23")
                time_str = f" (указано время: {time_input})"
            except (ValueError, IndexError) as e:
                await update.message.reply_text(
                    "⚠️ **Неверный формат времени!**\n"
                    "Используйте: HH:MM или H\n"
                    "Пример: `/c 13.1 0 02:30` или `/c 13.1 0 2`",
                    parse_mode='Markdown'
                )
                logger.warning(f"Ошибка времени: {e}")
                return
        else:
            msk_time = get_moscow_time()
            now_hour = msk_time.hour
            time_str = f" ({msk_time.strftime('%H:%M')} МСК)"

        k1 = get_k1(now_hour)
        period = get_period_name(now_hour)

        # Расчет дозы
        dose_food = xe * k1
        dose_dps = (current_bg - TARGET_BG) / ISF if current_bg > TARGET_BG else 0.0

        total_dose = dose_food + dose_dps
        rounded_dose = round(total_dose * 2) / 2  # Округление до 0.5 ед.

        response = (
            f"📊 **Расчет дозы Фиаспа:**\n"
            f"═══════════════════════════\n"
            f"🔹 Текущий СК: **{current_bg} ммоль/л**\n"
            f"🔹 Еда: **{carbs_g} г угл.** ({xe:.2f} ХЕ)\n"
            f"🕒 Расчетный час: **{now_hour}:00**{time_str}\n"
            f"{period}\n"
            f"⚙️ Используемый K1: **{k1}**\n"
            f"═══════════════════════════\n"
            f"🥗 Доза на еду: **{dose_food:.2f} ед.**\n"
            f"💉 ДПС (до {TARGET_BG}): **{dose_dps:.2f} ед.**\n"
            f"═══════════════════════════\n"
            f"🎯 **ИТОГО К УКОЛУ: {rounded_dose} ед. Фиаспа**\n"
            f"*(точное значение: {total_dose:.2f})*"
        )

        await update.message.reply_text(response, parse_mode='Markdown')
        logger.info(f"Расчет: СК={current_bg}, Угл={carbs_g}g, Час={now_hour} -> {rounded_dose}U")

    except ValueError as e:
        await update.message.reply_text(
            "⚠️ **Ошибка в числах!**\n"
            "Пример: `/c 7.6 84` (используйте точку или запятую)\n"
            f"Ошибка: {str(e)}",
            parse_mode='Markdown'
        )
        logger.warning(f"Ошибк�� значений: {e}")
    except Exception as e:
        logger.error(f"Ошибка при расчете: {e}")
        await update.message.reply_text("❌ Произошла ошибка при расчете.")

# --- 4. HEARTBEAT (периодический сигнал жизни) ---
async def heartbeat_task(context: ContextTypes.DEFAULT_TYPE):
    """Отправляет периодический сигнал жизни боту"""
    if ADMIN_ID == 0:
        logger.warning("⚠️ ADMIN_ID не установлен. Heartbeat отключен.")
        return
    
    try:
        msk_time = get_moscow_time()
        heartbeat_message = (
            f"💓 **HEARTBEAT - Бот живой!**\n\n"
            f"🤖 **Статус:** ✅ Работает\n"
            f"⏰ **Время:** {msk_time.strftime('%H:%M:%S')} МСК\n"
            f"📅 **Дата:** {msk_time.strftime('%d.%m.%Y')}\n"
            f"📊 **Версия:** 2.1\n"
            f"🌍 **Зона:** Europe/Moscow (UTC+3)"
        )
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=heartbeat_message,
            parse_mode='Markdown'
        )
        logger.info(f"✅ Heartbeat отправлен пользователю {ADMIN_ID}")
    except Exception as e:
        logger.error(f"❌ Ошибка при отправке heartbeat: {e}")

# --- 5. HEALTH CHECK (HTTP сервер для хостинга) ---
async def health_check(request):
    """HTTP endpoint для health check"""
    return web.Response(text="OK", status=200)

async def start_health_check_server():
    """Запускает HTTP сервер для health check"""
    try:
        app_web = web.Application()
        app_web.router.add_get('/health', health_check)
        runner = web.AppRunner(app_web)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', PORT)
        await site.start()
        logger.info(f"✅ Health Check сервер запущен на порту {PORT}")
    except Exception as e:
        logger.error(f"❌ Ошибка при запуске Health Check сервера: {e}")

# --- 6. ЗАПУСК ---
async def main():
    global app_instance, heartbeat_running
    
    if not BOT_TOKEN or BOT_TOKEN == "":
        print("❌ ОШИБКА: Задайте токен TELEGRAM_BOT_TOKEN!")
        return
    
    print("🚀 Бот запускается...")
    
    # Создание приложения
    app_instance = Application.builder().token(BOT_TOKEN).build()
    
    # Добавление обработчиков команд
    app_instance.add_handler(CommandHandler("start", start))
    app_instance.add_handler(CommandHandler("help", help_command))
    app_instance.add_handler(CommandHandler("status", status_command))
    app_instance.add_handler(CommandHandler("c", calculate))
    
    # Добавление job для heartbeat (каждый час)
    if ADMIN_ID != 0:
        app_instance.job_queue.run_repeating(
            heartbeat_task,
            interval=3600,  # Каждый час
            first=10  # Первый запуск через 10 секунд
        )
        logger.info(f"✅ Heartbeat включен для пользователя {ADMIN_ID}")
        heartbeat_running = True
    else:
        logger.warning("⚠️ ADMIN_ID не установлен. Heartbeat отключен.")
    
    # Запуск Health Check сервера
    await start_health_check_server()
    
    print("✅ Бот успешно запущен!")
    logger.info("✅ Бот успешно запущен!")
    
    # Запуск бота
    await app_instance.run_polling()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Бот остановлен пользователем")
        logger.info("Бот остановлен пользователем")
