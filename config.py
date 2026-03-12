import os
from dotenv import load_dotenv

load_dotenv()

UPSTREAM_BASE_URL = "https://grsai.dakka.com.cn"
MIN_CREDITS = int(os.getenv("MIN_CREDITS", "100"))
CREDITS_REFRESH_INTERVAL = int(os.getenv("CREDITS_REFRESH_INTERVAL", "300"))
PORT = int(os.getenv("PORT", "8000"))
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")

_raw_keys = os.getenv("GRSAI_API_KEYS", "")
API_KEYS = [k.strip() for k in _raw_keys.split(",") if k.strip()]
