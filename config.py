import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Telegram Bot
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

# OmGTU API
SCHEDULE_API = "https://rasp.omgtu.ru/api/schedule/group/{group_id}?start={date}&finish={date}&lng=1"
SEARCH_API = "https://rasp.omgtu.ru/api/search?term={term}&type=group"

# Timezone (Omsk UTC+6)
OMSK_OFFSET = 6

# Database
DB_PATH = os.getenv("DB_PATH", "data/schedule_bot.db")

# Hourly schedule check interval (seconds)
CHECK_INTERVAL_SECONDS = 3600


def get_proxy():
    """Detects local HTTP proxy if configured."""
    proxy_url = os.getenv("HTTP_PROXY") or os.getenv("HTTPS_PROXY")
    if proxy_url:
        return proxy_url
    import socket
    s = socket.socket()
    s.settimeout(0.3)
    try:
        s.connect(("127.0.0.1", 10809))
        s.close()
        return "http://127.0.0.1:10809"
    except Exception:
        return None
