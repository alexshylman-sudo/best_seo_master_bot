import os
import logging
import threading
import time
from flask import Flask
from google import genai
import telebot
from telebot.types import Message
from dotenv import load_dotenv

# 1. Настройка окружения и логирования
load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# 2. Flask-сервер для Health Check на Render
app = Flask(__name__)

@app.route('/')
def health_check():
    return "SEO Master Bot is active!", 200

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    # Использование встроенного сервера Flask для внутренних нужд Render
    app.run(host='0.0.0.0', port=port)

# 3. Инициализация Telegram и Gemini
bot = telebot.TeleBot(os.getenv("TELEGRAM_TOKEN"))
client = genai.Client()

@bot.message_handler(commands=['start'])
def send_welcome(message: Message):
    welcome_text = (
        "✅ **Бот активирован (Tier 1)!**\n\n"
        "Я готов к глубокому SEO-анализу. Пришлите мне:\n"
        "1. Текст для проверки на ключи и LSI.\n"
        "2. Скриншот сайта для аудита юзабилити.\n"
        "3. Ссылку или тему для контент-плана."
    )
    bot.reply_to(message, welcome_text, parse_mode='Markdown')

@bot.message_handler(content_types=['text', 'photo'])
def handle_message(message: Message):
    try:
        # Улучшенный системный промпт для экспертных ответов
        system_prompt = (
            "Ты — ведущий SEO-эксперт. Твои ответы должны быть практическими, "
            "содержать конкретные рекомендации по оптимизации и оформлены в Markdown."
        )
        content = [system_prompt]

        if message.photo:
            file_info = bot.get_file(message.photo[-1].file_id)
            downloaded_file = bot.download_file(file_info.file_path)
            content.append({"mime_type": "image/jpeg", "data": downloaded_file})
            content.append(message.caption or "Проведи SEO-аудит этого скриншота: оцени структуру, призывы к действию и визуальную иерархию.")
        else:
            content.append(message.text)

        # Генерация контента с оптимизированным ожиданием для Tier 1
        response = None
        for attempt in range(2):
            try:
                response = client.models.generate_content(
                    model="gemini-2.0-flash", 
                    contents=content
                )
                break
            except Exception as e:
                if "429" in str(e):
                    logger.warning(f"Кратковременное ограничение. Пауза 5 сек...")
                    time.sleep(5)
                else:
                    raise e
        
        if response and response.text:
            text = response.text
            # Разбивка длинных ответов (лимит Telegram 4096 символов)
            for i in range(0, len(text), 4000):
                bot.reply_to(message, text[i:i+4000], parse_mode='Markdown')
        else:
            bot.reply_to(message, "⚠️ Модель не смогла сформировать ответ. Попробуйте перефразировать запрос.")

    except Exception as e:
        logger.error(f"Ошибка обработки: {e}")
        bot.reply_to(message, f"❌ Произошла техническая ошибка. Попробуйте позже.")

# 4. Запуск бота
if __name__ == "__main__":
    # Запуск Flask в отдельном потоке
    threading.Thread(target=run_flask, daemon=True).start()
    
    bot.remove_webhook()
    logger.info("Бот запущен в режиме Infinity Polling...")
    
    # infinity_polling более надежен для продакшена и не пропускает сообщения
    bot.infinity_polling(timeout=20, long_polling_timeout=10)
