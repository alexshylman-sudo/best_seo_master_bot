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

# 2. Создание Flask-сервера для "оживления" Render (Health Check)
app = Flask(__name__)

@app.route('/')
def health_check():
    return "Bot is running!", 200

def run_flask():
    # Render передает порт в переменную PORT, по умолчанию 10000
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)

# 3. Инициализация Telegram и Gemini
bot = telebot.TeleBot(os.getenv("TELEGRAM_TOKEN"))
client = genai.Client()

@bot.message_handler(commands=['start'])
def send_welcome(message: Message):
    bot.reply_to(message, "✅ Бот запущен с поддержкой 24/7! Пришлите SEO-запрос или фото сайта.")

@bot.message_handler(content_types=['text', 'photo'])
def handle_message(message: Message):
    try:
        system_prompt = "Ты профессиональный SEO-эксперт. Отвечай кратко и по делу на русском языке."
        content = [system_prompt]

        if message.photo:
            file_info = bot.get_file(message.photo[-1].file_id)
            downloaded_file = bot.download_file(file_info.file_path)
            content.append({"mime_type": "image/jpeg", "data": downloaded_file})
            content.append(message.caption or "Проанализируй скриншот")
        else:
            content.append(message.text)

        # Генерация контента (используем стабильную модель)
        response = client.models.generate_content(
            model="gemini-2.0-flash", 
            contents=content
        )
        
        bot.reply_to(message, response.text if response.text else "⚠️ ИИ не смог сформировать ответ.")

    except Exception as e:
        logger.error(f"Ошибка: {e}")
        bot.reply_to(message, f"❌ Ошибка API: {str(e)}")

# 4. Запуск
if __name__ == "__main__":
    # Запускаем Flask в отдельном потоке (решает проблему Port scan timeout)
    threading.Thread(target=run_flask, daemon=True).start()
    
    bot.remove_webhook()
    logger.info("Бот запущен...")
    
    # Бесконечный цикл с перезапуском при сбоях сети
    while True:
        try:
            bot.polling(none_stop=True, interval=0, timeout=20)
        except Exception as e:
            logger.error(f"Ошибка Polling: {e}. Перезапуск через 5 сек...")
            time.sleep(5)
