import threading
import time
import schedule
import requests
from flask import Flask

# Import Configuration and Database Init
from config import bot, APP_URL
from database import init_db

# Import all handler modules to register them with the bot
import handlers_core
import handlers_wizard
import handlers_extra

app = Flask(__name__)
@app.route('/')
def h(): return "Alive", 200

def run_scheduler():
    while True:
        try:
            schedule.run_pending()
            if APP_URL: requests.get(APP_URL, timeout=10)
        except: pass
        time.sleep(600)

if __name__ == "__main__":
    init_db()
    threading.Thread(target=lambda: app.run(host='0.0.0.0', port=10000, use_reloader=False), daemon=True).start()
    threading.Thread(target=run_scheduler, daemon=True).start()
    print("🤖 Бот запущен...")
    while True:
        try:
            bot.infinity_polling(skip_pending=True, timeout=60, long_polling_timeout=60)
        except Exception as e:
            print(f"Polling Error: {e}")
            time.sleep(5)