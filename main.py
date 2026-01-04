import os
import telebot
import google.generativeai as genai
from dotenv import load_dotenv

load_dotenv()

# Настройка ключей
bot = telebot.TeleBot(os.getenv("TELEGRAM_TOKEN"))
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
model = genai.GenerativeModel('gemini-1.5-flash-latest')

# Системная инструкция для ИИ, чтобы он вел себя как SEO-эксперт
SYSTEM_PROMPT = "Ты — профессиональный SEO-оптимизатор и контент-менеджер. Твоя задача: писать уникальные статьи, подбирать ключевые слова и составлять контент-планы."

@bot.message_handler(commands=['start'])
def send_welcome(message):
    welcome_text = (
        "👋 Привет! Я ваш AI SEO-мастер.\n\n"
        "Я могу:\n"
        "1. ✍️ Написать SEO-статью по вашей теме.\n"
        "2. 🔑 Подоброать ключевые слова.\n"
        "3. 📅 Составить контент-план.\n\n"
        "Просто напишите мне вашу задачу!"
    )
    bot.reply_to(message, welcome_text)

@bot.message_handler(func=lambda message: True)
def handle_message(message):
    # Отправляем уведомление, что бот «думает»
    sent_message = bot.reply_to(message, "🔍 Анализирую запрос и генерирую ответ...")
    
    try:
        # Формируем запрос к Gemini с учетом роли эксперта
        full_prompt = f"{SYSTEM_PROMPT}\n\nПользователь просит: {message.text}"
        response = model.generate_content(full_prompt)
        
        # Редактируем сообщение «думает» на готовый ответ
        bot.edit_message_text(response.text, chat_id=sent_message.chat.id, message_id=sent_message.message_id)
    except Exception as e:
        bot.edit_message_text(f"❌ Произошла ошибка: {str(e)}", chat_id=sent_message.chat.id, message_id=sent_message.message_id)

if __name__ == "__main__":
    bot.polling(none_stop=True)
