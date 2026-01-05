import os
import logging
from google import genai  # Импорт для SDK 2026
import telebot
from telebot.types import Message
from dotenv import load_dotenv
from requests.exceptions import RequestException
import time
import threading
from flask import Flask
import os

# Создаем микро-сервер для "обмана" проверок Render
app = Flask(__name__)

@app.route('/')
def health_check():
    return "Bot is running!", 200

def run_flask():
    # Render передает порт в переменную окружения PORT
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)

# Запускаем сервер в фоновом потоке, чтобы он не мешал боту
threading.Thread(target=run_flask, daemon=True).start()

# ДАЛЕЕ ИДЕТ ВАШ ВЕСЬ ОСТАЛЬНОЙ КОД БОТА...

# Загрузка переменных окружения (для локального тестирования)
load_dotenv()

# Настройка логирования (в файл и консоль для отладки на Render)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler("bot.log"), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# Настройка Telegram бота
bot = telebot.TeleBot(os.getenv("TELEGRAM_TOKEN"))

# Проверка API-ключа
api_key = os.getenv("GEMINI_API_KEY")
if not api_key:
    logger.error("GEMINI_API_KEY не задан!")
    raise ValueError("GEMINI_API_KEY не задан!")

# Создание клиента Gemini
client = genai.Client()

# Отладка: Вывод списка доступных моделей (проверьте в логах Render)
try:
    for m in client.models.list():
        if 'generateContent' in m.supported_actions:
            logger.info(f"Доступная модель: {m.name}")
except Exception as e:
    logger.error(f"Ошибка списка моделей: {str(e)}")

@bot.message_handler(commands=['start'])
def send_welcome(message: Message):
    bot.reply_to(message, "✅ Бот обновлен и готов к SEO-задачам! Напишите запрос, например: 'Оптимизируй текст для ключевого слова SEO в 2026'.")

@bot.message_handler(commands=['help'])
def send_help(message: Message):
    help_text = (
        "🛠️ Команды:\n"
        "/start - Запуск бота\n"
        "/help - Эта справка\n\n"
        "Примеры запросов:\n"
        "- Анализ ключевых слов: 'Проанализируй ключи для сайта о маркетинге'\n"
        "- Генерация контента: 'Напиши статью 500 слов о SEO в 2026 году'\n"
        "- Оптимизация: 'Оптимизируй этот текст: [вставьте текст]'\n"
        "- С фото: Отправьте изображение с подписью для анализа (например, скрин сайта)"
    )
    bot.reply_to(message, help_text)

@bot.message_handler(content_types=['text', 'photo'])
def handle_message(message: Message):
    try:
        # Улучшенный системный промпт
        system_prompt = (
            "Ты профессиональный SEO-эксперт с 10+ лет опыта в 2026 году. Учитывай тренды: AI-powered search (SGE+), voice SEO, zero-click, E-E-A-T 2.0. "
            "Анализируй запрос: ключи, семантика, on-page/off-page, структура, мобильность, скорость. "
            "Предлагай улучшения для Google/Yandex/Bing. Ответь кратко, структурировано, на русском. Если не SEO, перенаправь."
        )

        # Подготовка контента
        content = [system_prompt]
        if message.photo:
            file_info = bot.get_file(message.photo[-1].file_id)
            downloaded_file = bot.download_file(file_info.file_path)
            content.append({"mime_type": "image/jpeg", "data": downloaded_file})
            content.append(message.caption or "Анализируй это изображение в контексте SEO")
        else:
            content.append(message.text)

        # Генерация с ретраями
        for attempt in range(3):
            try:
                response = client.models.generate_content(
                    model="gemini-2.5-flash",  # Актуальная модель; альтернатива: "gemini-3-flash"
                    contents=content
                )
                break
            except RequestException as re:
                logger.warning(f"Сетевая ошибка, попытка {attempt+1}: {str(re)}")
                time.sleep(2 ** attempt)
        else:
            raise Exception("Не удалось сгенерировать после 3 попыток")

        # Обработка ответа
        text = response.text.strip()
        if not text and hasattr(response.prompt_feedback, 'block_reason'):
            text = f"⚠️ Заблокировано: {response.prompt_feedback.block_reason}. Уточните запрос."

        if text:
            for i in range(0, len(text), 4000):
                bot.reply_to(message, text[i:i+4000])
        else:
            bot.reply_to(message, "⚠️ Пустой ответ. Перефразируйте.")

    except Exception as e:
        logger.error(f"Ошибка: {str(e)}")
        bot.reply_to(message, f"❌ Ошибка API: {str(e)}.")

if __name__ == "__main__":
    bot.remove_webhook()
    logger.info("Бот запущен...")
    while True:
        try:
            bot.polling(none_stop=True, interval=0, timeout=20)
        except Exception as e:
            logger.error(f"Polling ошибка: {str(e)}. Перезапуск...")
            time.sleep(5)
