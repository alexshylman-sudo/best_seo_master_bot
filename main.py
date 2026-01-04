import os
import telebot
import google.generativeai as genai
from dotenv import load_dotenv

load_dotenv()

# Настройка Telegram
bot = telebot.TeleBot(os.getenv("TELEGRAM_TOKEN"))

# Настройка Gemini (REST решает проблему 404 на Render)
genai.configure(api_key=os.getenv("GEMINI_API_KEY"), transport='rest')

@bot.message_handler(commands=['start'])
def send_welcome(message):
    bot.reply_to(message, "✅ Бот обновлен и готов к SEO-задачам!")

@bot.message_handler(func=lambda message: True)
def handle_message(message):
    try:
        # Прямое указание полной версии модели
        model = genai.GenerativeModel(model_name="models/gemini-1.5-flash")
        
        response = model.generate_content(
            f"Ты профессиональный SEO-эксперт. Ответь пользователю: {message.text}"
        )
        
        if response.text:
            bot.reply_to(message, response.text)
        else:
            bot.reply_to(message, "⚠️ ИИ вернул пустой ответ.")
            
    except Exception as e:
        bot.reply_to(message, f"❌ Ошибка API: {str(e)}")

if __name__ == "__main__":
    # Сброс вебхука решает ошибку 409
    bot.remove_webhook()
    print("Бот запущен...")
    bot.polling(none_stop=True, skip_pending=True)
