import os
import logging
import google.generative_ai as genai  # Новый SDK с дефисом
import telebot
from dotenv import load_dotenv

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

# Настройка Gemini API
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))  # transport='rest' можно добавить, если нужно

# Отладка: Вывод списка доступных моделей (проверьте в логах Render)
try:
    for model in genai.list_models():
        if 'generateContent' in model.supported_generation_methods:
            logger.info(f"Доступная модель: {model.name}")
except Exception as e:
    logger.error(f"Ошибка при получении списка моделей: {str(e)}")

@bot.message_handler(commands=['start'])
def send_welcome(message):
    bot.reply_to(message, "✅ Бот обновлен и готов к SEO-задачам! Напишите запрос, например: 'Оптимизируй текст для ключевого слова SEO'.")

@bot.message_handler(commands=['help'])
def send_help(message):
    help_text = (
        "🛠️ Команды:\n"
        "/start - Запуск бота\n"
        "/help - Эта справка\n\n"
        "Примеры запросов:\n"
        "- Анализ ключевых слов: 'Проанализируй ключи для сайта о маркетинге'\n"
        "- Генерация контента: 'Напиши статью 500 слов о SEO в 2026 году'\n"
        "- Оптимизация: 'Оптимизируй этот текст: [вставьте текст]'"
    )
    bot.reply_to(message, help_text)

@bot.message_handler(func=lambda message: True)
def handle_message(message):
    try:
        # Инициализация модели (актуальная версия)
        model = genai.GenerativeModel('gemini-2.5-flash')  # Или 'gemini-3-flash-preview' для новейшей

        # Улучшенный системный промпт для SEO-эксперта
        system_prompt = (
            "Ты профессиональный SEO-эксперт с 10+ лет опыта. "
            "Анализируй запрос пользователя, предлагай оптимизации: ключевые слова, мета-теги, структуру контента, "
            "улучшения для поисковиков (Google, Yandex). Ответь кратко, структурировано, на русском. "
            "Если запрос не о SEO, вежливо перенаправь на тему."
        )

        # Генерация контента
        response = model.generate_content(
            [system_prompt, message.text]  # Мультимодальный ввод: системный + пользовательский
        )

        # Обработка ответа (учитываем candidates и safety blocks)
        if response.candidates:
            text = response.candidates[0].content.parts[0].text.strip()
        else:
            text = response.text.strip() if hasattr(response, 'text') else ""

        if text:
            # Разбиение длинного текста на части (лимит Telegram ~4096 символов)
            for i in range(0, len(text), 4000):
                bot.reply_to(message, text[i:i+4000])
        else:
            bot.reply_to(message, "⚠️ ИИ вернул пустой ответ. Попробуйте перефразировать запрос.")

    except Exception as e:
        logger.error(f"Ошибка при обработке сообщения: {str(e)}")
        bot.reply_to(message, f"❌ Ошибка API: {str(e)}. Проверьте ключ или модель.")

if __name__ == "__main__":
    # Сброс вебхука (на случай конфликтов)
    bot.remove_webhook()
    logger.info("Бот запущен...")
    # Polling с none_stop=True для непрерывной работы
    bot.polling(none_stop=True)
