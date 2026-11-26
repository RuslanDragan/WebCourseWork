"""Конфігурація середовища вебсистеми бронювання навчальних аудиторій."""
from datetime import datetime
import os
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent.parent


def load_dotenv_file(dotenv_path: Path | None = None) -> None:
    """Завантажує змінні з локального файлу .env без сторонніх залежностей (не перезаписуючи системні)."""
    target = dotenv_path or (BASE_DIR / ".env")
    if not target.is_file():
        return
    for raw_line in target.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)


load_dotenv_file()

APP_ENV = os.getenv("APP_ENV", "development")
APP_HOST = os.getenv("APP_HOST", "127.0.0.1")
APP_PORT = int(os.getenv("APP_PORT", "8000"))

DEFAULT_DB_PATH = BASE_DIR / "data" / "auditorium_booking.db"
DATABASE_PATH = Path(os.getenv("DATABASE_PATH", str(DEFAULT_DB_PATH)))

TIMEZONE = os.getenv("TIMEZONE", "Europe/Kyiv")
APP_TZ = ZoneInfo(TIMEZONE)


def now_app_tz() -> datetime:
    """Повертає поточний момент часу із однозначним часовим поясом (Europe/Kyiv)."""
    return datetime.now(APP_TZ)
SESSION_COOKIE_NAME = os.getenv("SESSION_COOKIE_NAME", "booking_session_id")
SESSION_MAX_AGE_SECONDS = int(os.getenv("SESSION_MAX_AGE_SECONDS", "28800"))

MIGRATIONS_DIR = BASE_DIR / "migrations"

# Секрет для підпису CSRF-токенів (HMAC-SHA256). У production задається лише через змінну середовища.
SECRET_KEY = os.getenv("SECRET_KEY", "dev-only-insecure-secret-change-me")
# Дозволені джерела (Origin) для змінювальних запитів; порожній список = лише той самий хост.
ALLOWED_ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o.strip()]

# Обмеження спроб входу (захист від підбору пароля)
LOGIN_MAX_FAILED_ATTEMPTS = int(os.getenv("LOGIN_MAX_FAILED_ATTEMPTS", "5"))
LOGIN_LOCK_MINUTES = int(os.getenv("LOGIN_LOCK_MINUTES", "15"))

# Розклад стандартних навчальних пар (Europe/Kyiv)
ACADEMIC_PAIRS = {
    1: ("08:30", "10:05"),
    2: ("10:20", "11:55"),
    3: ("12:10", "13:45"),
    4: ("14:15", "15:50"),
    5: ("16:05", "17:40"),
    6: ("17:55", "19:30"),
}
