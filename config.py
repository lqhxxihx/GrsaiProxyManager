import os
import bcrypt
from dotenv import load_dotenv

load_dotenv()

UPSTREAM_BASE_URL = "https://grsaiapi.com"
MIN_CREDITS = int(os.getenv("MIN_CREDITS", "100"))
CREDITS_REFRESH_INTERVAL = int(os.getenv("CREDITS_REFRESH_INTERVAL", "300"))
PORT = int(os.getenv("PORT", "8000"))

DEFAULT_PASSWORD = "admin123456"
DATA_DIR = os.getenv("DATA_DIR", "data")
os.makedirs(DATA_DIR, exist_ok=True)
PASSWORD_PATH = os.path.join(DATA_DIR, ".password")

# 从 .password 文件读取密码哈希，不存在则自动生成默认密码
def _read_password_hash() -> str:
    try:
        with open(PASSWORD_PATH, encoding="utf-8") as f:
            h = f.read().strip()
            if h:
                return h
    except FileNotFoundError:
        pass
    # 自动生成默认密码哈希并写入
    h = bcrypt.hashpw(DEFAULT_PASSWORD.encode(), bcrypt.gensalt()).decode()
    try:
        with open(PASSWORD_PATH, "w", encoding="utf-8") as f:
            f.write(h)
    except Exception:
        pass
    return h

ADMIN_PASSWORD_HASH = _read_password_hash()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")

_raw_keys = os.getenv("GRSAI_API_KEYS", "")
API_KEYS = [k.strip() for k in _raw_keys.split(",") if k.strip()]
