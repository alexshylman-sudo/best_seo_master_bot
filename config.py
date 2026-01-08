import os
from telebot import TeleBot
from google import genai
from dotenv import load_dotenv

# --- CONFIGURATION ---
load_dotenv()

ADMIN_ID = 203473623
SUPPORT_ID = 203473623
DB_URL = os.getenv("DATABASE_URL")
TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
APP_URL = os.getenv("APP_URL")

# Initialize Bot and AI Client
bot = TeleBot(TOKEN)
client = genai.Client(api_key=GEMINI_KEY)

# --- GLOBAL STATES ---
USER_CONTEXT = {}
UPLOAD_STATE = {}
LINK_UPLOAD_STATE = {}
SURVEY_STATE = {}
TEMP_PROMPTS = {}
TEMP_LINKS = {}
COMPETITOR_STATE = {}

# --- GIF DICTIONARY ---
STEP_GIFS = {
    "scan": "https://ecosteni.ru/wp-content/uploads/2026/01/202601081821.gif",
    "survey": "https://ecosteni.ru/wp-content/uploads/2026/01/202601082231.gif",
    "competitors": "https://ecosteni.ru/wp-content/uploads/2026/01/202601082256.gif",
    "links": "https://ecosteni.ru/wp-content/uploads/2026/01/202601082306.gif",
    "gallery": "https://ecosteni.ru/wp-content/uploads/2026/01/a_girl_stands_202601082318.gif",
    "img_prompts": "https://ecosteni.ru/wp-content/uploads/2026/01/202601090015.gif",
    "text_prompts": "https://media.giphy.com/media/l0HlPybHMx6D3iaHO/giphy.gif",
    "cms": "https://media.giphy.com/media/3oKIPnAiaMCws8nOsE/giphy.gif",
    "article": "https://media.giphy.com/media/l0HlHFRbY9C4FtA7i/giphy.gif",
    "strategy": "https://media.giphy.com/media/3o7TKvxnBdHP2IulJP/giphy.gif",
    "done": "https://media.giphy.com/media/l0MYt5jPR6QX5pnqM/giphy.gif"
}