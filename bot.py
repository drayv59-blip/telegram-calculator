import logging
import os
import threading
import asyncio
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from enum import Enum

import pytz
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# ============ Конфигурация ============
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN не установлен!")

ADMIN_ID = int(os.environ.get("ADMIN_ID", 0))
PORT = int(os.environ.get("PORT", 8000))
TIMEZONE = "Europe/Moscow"
HEARTBEAT_INTERVAL = 3600  # 1 час

# ============ Логирование ============
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# ============ Константы ============
class MealType(Enum):
    """Типы приёмов пищи с коэффициентами"""
    BREAKFAST = (1.33, "Завтрак")
    LUNCH = (1.33, "Обед")
    DINNER = (1.38, "Ужин")
    NIGHT = (1.30, "Ночь")


class InsulinConfig:
    """Конфигурация инсулина"""
    FCHI_RATIO = 2.5
    TARGET_SUGAR = 5.5
    SUGAR_THRESHOLD = 7.0


# ============ Health Check (для Render/Railway) ============
class HealthHandler(BaseHTTPRequestHandler):
    """HTTP обработчик для проверки здоровья приложения"""
    
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        pass


def run_health_server():
    """Запуск HTTP сервера для health-check"""
    try:
        server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
        logger.info(f"✅ Health-сервер запущен на порту {PORT}")
        server.serve_forever()
    except Exception as e:
        logger.error(f"❌ Ошибка health-сервера: {e}")


# ============ Heartbeat Manager (сигнал жизни) ============
class HeartbeatManager:
    """Менеджер для отправки периодических сигналов жизни"""
    
    def __init__(self, app, admin_id: int, interval: int = 3600):
        self.app = app
        self.admin_id = admin_id
        self.interval = interval
        self.is_running = False
        self.task = None
    
    async def start(self):
        """Запустить отправку heartbeat"""
        if self.admin_id == 0:
            logger.warning("⚠️ ADMIN_ID не установлен, heartbeat отключён")
            return
        
        self.is_running = True
        self.task = asyncio.create_task(self._heartbeat_loop())
        logger.info(f"💓 Heartbeat запущен (интервал: {self.interval}с = {self.interval//60}мин)")
    
    async def stop(self):
        """Остановить отправку heartbeat"""
        self.is_running = False
        if self.task:
            self.task.cancel()
        logger.info("⏹️ Heartbeat остановлен")
    
    async def _heartbeat_loop(self):
        """Бесконечный цикл отправки сигналов"""
        while self.is_running:
            try:
                await asyncio.sleep(self.interval)
                
                current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                message = (
                    f"💓 <b>HEARTBEAT - Бот живой!</b>\n\n"
                    f"🤖 <b>Статус:</b> ✅ Работает\n"
                    f"⏰ <b>Время:</b> <code>{current_time}</code>\n"
                    f"📊 <b>Версия:</b> 2.0 с Heartbeat\n"
                    f"🌍 <b>Зона:</b> {TIMEZONE}"
                )
                
                try:
                    await self.app.bot.send_message(
                        chat_id=self.admin_id,
                        text=message,
                        parse_mode="HTML"
                    )
                    logger.info(f"💓 Heartbeat отправлен администратору (ID: {self.admin_id})")
                except Exception as e:
                    logger.error(f"❌ Ошибка отправки heartbeat: {e}")
                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"❌ Ошибка в heartbeat цикле: {e}")


# ============ Логика расчётов ============
def get_meal_by_hour(hour: int) -> tuple[float, str]:
    """Определить тип приёма пищи по времени"""
    if 6 <= hour < 11:
        return MealType.BREAKFAST.value
    elif 11 <= hour < 16:
        return MealType.LUNCH.value
    elif 16 <= hour < 22:
        return MealType.DINNER.value
    else:
        return MealType.NIGHT.value


def get_auto_uk() -> tuple[float, str]:
    """Получить автоматический УК (коэффициент углеводов) по времени"""
    tz = pytz.timezone(TIMEZONE)
    now = datetime.now(tz)
    uk, meal_name = get_meal_by_hour(now.hour)
    return uk, meal_name


def mround_05(x: float) -> float:
    """Округлить до ближайшего 0.5"""
    return round(x * 2) / 2


def calculate_insulin_dose(
    sugar_level: float,
    carbs: float,
    uk: float | None = None
) -> dict:
    """Расчёт дозы инсулина"""
    if uk is None:
        uk, meal_name = get_auto_uk()
    else:
        meal_name = "Ручной ввод"
    
    dose_food = round((carbs / 12) * uk, 1)
    
    if sugar_level > InsulinConfig.SUGAR_THRESHOLD:
        dps = round((sugar_level - InsulinConfig.TARGET_SUGAR) / InsulinConfig.FCHI_RATIO, 1)
    else:
        dps = 0.0
    
    total_dose = mround_05(dose_food + dps)
    
    return {
        "sugar_level": sugar_level,
        "carbs": carbs,
        "uk": uk,
        "meal_name": meal_name,
        "dose_food": dose_food,
        "dps": dps,
        "total_dose": total_dose,
        "xe": round(carbs / 12, 1)
    }


def format_result(calc_result: dict) -> str:
    """Форматировать результат расчёта"""
    return (
        f"📊 <b>Результат расчета ({calc_result['meal_name']}):</b>\n\n"
        f"• Старт СК: <b>{calc_result['sugar_level']}</b> ммоль/л\n"
        f"• Еда: <b>{calc_result['carbs']}г УГ</b> ({calc_result['xe']} ХЕ)\n"
        f"• Ваш УК: <b>{calc_result['uk']}</b>\n"
        f"───────────────\n"
        f"🍚 Доза на еду: <b>{calc_result['dose_food']} ед.</b>\n"
        f"📉 ДПС (снижение): <b>{calc_result['dps']} ед.</b>\n\n"
        f"💉 <b>ИТОГОВАЯ ДОЗА: {calc_result['total_dose']} ед. Фиаспа</b>"
    )


# ============ Telegram Handlers ============
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /start"""
    msg = (
        "👋 Привет! Я калькулятор Артёма.\n\n"
        "Теперь я <b>сам определяю коэффициент по времени суток</b>!\n\n"
        "Просто отправь 2 цифры (Сахар и Еду в граммах):\n"
        "<b>/c [Сахар] [Углеводы в граммах]</b>\n\n"
        "📌 <i>Пример:</i> <code>/c 9.3 72</code>\n\n"
        "<i>(Если нужно ввести свой УК вручную, напишите 3 цифры: <code>/c 9.3 72 1.4</code>)</i>\n\n"
        "━━━━━━━━━━━━━━━━━\n"
        "📋 <b>Другие команды:</b>\n"
        "/status - проверить статус бота\n"
        "/help - помощь"
    )
    await update.message.reply_text(msg, parse_mode="HTML")
    logger.info(f"👤 Пользователь {update.effective_user.id} запустил бота")


async def calc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /c для расчёта дозы"""
    try:
        args = context.args
        
        if len(args) < 2:
            await update.message.reply_text(
                "⚠️ Ошибка! Введите хотя бы 2 значения: Сахар и Еду.\n"
                "Пример: <code>/c 9.3 72</code>",
                parse_mode="HTML",
            )
            return
        
        try:
            sugar_level = float(args[0].replace(",", "."))
            carbs = float(args[1].replace(",", "."))
            uk = None
            
            if len(args) >= 3:
                uk = float(args[2].replace(",", "."))
        except ValueError:
            await update.message.reply_text(
                "⚠️ Ошибка формата! Используйте точку или запятую как разделитель.\n"
                "Пример: <code>/c 9.3 72</code> или <code>/c 9,3 72</code>",
                parse_mode="HTML",
            )
            return
        
        if sugar_level < 0 or carbs < 0 or (uk and uk <= 0):
            await update.message.reply_text(
                "⚠️ Ошибка! Значения должны быть положительными числами.",
                parse_mode="HTML",
            )
            return
        
        result = calculate_insulin_dose(sugar_level, carbs, uk)
        response = format_result(result)
        
        await update.message.reply_text(response, parse_mode="HTML")
        logger.info(
            f"📊 Пользователь {update.effective_user.id} рассчитал дозу: "
            f"СК={sugar_level}, УГ={carbs}, результат={result['total_dose']}"
        )
        
    except Exception as e:
        logger.error(f"❌ Ошибка в calc: {e}")
        await update.message.reply_text(
            "❌ Произошла ошибка при расчёте. Попробуйте ещё раз.",
            parse_mode="HTML",
        )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /status"""
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    tz = pytz.timezone(TIMEZONE)
    now = datetime.now(tz)
    _, meal_name = get_meal_by_hour(now.hour)
    
    message = (
        f"✅ <b>СТАТУС БОТА:</b>\n\n"
        f"🤖 <b>Статус:</b> <b>Живой и работает!</b>\n"
        f"⏰ <b>Время:</b> <code>{current_time}</code>\n"
        f"🍽️ <b>Текущий приём пищи:</b> {meal_name}\n"
        f"💓 <b>Heartbeat:</b> <b>Активен</b> (отправляется каждый час)\n"
        f"📍 <b>Временная зона:</b> {TIMEZONE}\n"
        f"🔧 <b>Health Check:</b> Работает на порту {PORT}"
    )
    await update.message.reply_text(message, parse_mode="HTML")
    logger.info(f"📊 Пользователь {update.effective_user.id} проверил статус")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /help"""
    msg = (
        "<b>📖 СПРАВКА</b>\n\n"
        "<b>Основные команды:</b>\n"
        "/start - начало работы\n"
        "/c - расчёт дозы инсулина\n"
        "/status - статус бота\n"
        "/help - эта справка\n\n"
        "<b>Примеры использования:</b>\n"
        "<code>/c 9.3 72</code> - авто УК\n"
        "<code>/c 9.3 72 1.4</code> - ручной УК\n\n"
        "<b>Параметры:</b>\n"
        "• [Сахар] - уровень глюкозы в крови (ммоль/л)\n"
        "• [Углеводы] - количество углеводов (граммы)\n"
        "• [УК] - коэффициент углеводов (опционально)"
    )
    await update.message.reply_text(msg, parse_mode="HTML")


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик ошибок"""
    logger.error(f"❌ Ошибка: {context.error}", exc_info=context.error)


# ============ Main ============
async def main():
    """Главная асинхронная функция"""
    
    # Запуск health-check сервера в отдельном потоке
    health_thread = threading.Thread(target=run_health_server, daemon=True)
    health_thread.start()
    
    # Создание приложения
    app = ApplicationBuilder().token(TOKEN).build()
    
    # Добавление обработчиков команд
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("c", calc))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_error_handler(error_handler)
    
    # Создание heartbeat менеджера
    heartbeat = HeartbeatManager(app, ADMIN_ID, HEARTBEAT_INTERVAL)
    
    async with app:
        await app.initialize()
        await app.start()
        
        # Запуск heartbeat
        await heartbeat.start()
        
        logger.info("=" * 50)
        logger.info("🤖 БОТ ЗАПУЩЕН УСПЕШНО!")
        logger.info("=" * 50)
        logger.info(f"💓 Heartbeat интервал: {HEARTBEAT_INTERVAL}с ({HEARTBEAT_INTERVAL//60}мин)")
        logger.info(f"📍 Временная зона: {TIMEZONE}")
        logger.info(f"🔧 Health Check порт: {PORT}")
        if ADMIN_ID != 0:
            logger.info(f"👤 ID администратора: {ADMIN_ID}")
        else:
            logger.warning("⚠️ ADMIN_ID не установлен - heartbeat отключён!")
        logger.info("=" * 50)
        
        try:
            # Бесконечный ждун
            await asyncio.Event().wait()
        except KeyboardInterrupt:
            logger.info("⏹️ Бот остановлен пользователем")
        finally:
            await heartbeat.stop()
            await app.stop()
            logger.info("🛑 Бот полностью остановлен")


if __name__ == "__main__":
    asyncio.run(main())