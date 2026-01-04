import os
import telebot
import google.generativeai as genai
from dotenv import load_dotenv

load_dotenv()

# Настройка Telegram
bot = telebot.TeleBot(os.getenv("TELEGRAM_TOKEN"))

# Настройка Gemini с явным указанием версии API (решает 404)
genai.configure(api_key=os.getenv("GEMINI_API_KEY"), transport='rest')

# Инициализация модели
model = genai.GenerativeModel(model_name="gemini-1.5-flash")

@bot.message_handler(commands=['start'])
def send_welcome(message):
    bot.reply_to(message, "✅ Бот обновлен и готов к работе! Пришлите вашу SEO-задачу.")

@bot.message_handler(func=lambda message: True)
def handle_message(message):
    try:
        # Прямой вызов генерации
        response = model.generate_content(f"Ты SEO-эксперт. Ответь пользователю: {message.text}")
        bot.reply_to(message, response.text)
    except Exception as e:
        # Вывод ошибки в чат для диагностики
        bot.reply_to(message, f"❌ Ошибка: {str(e)}")

if __name__ == "__main__":
    bot.remove_webhook()
    bot.polling(none_stop=True, skip_pending=True)
