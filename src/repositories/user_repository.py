"""Репозиторій автентифікації, користувачів та серверних сесій."""
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import secrets
import sqlite3

from src.domain.models import User, UserRole


def hash_password(password: str, salt: bytes | None = None) -> str:
    """
    Хешування пароля за допомогою криптографічної функції scrypt (OWASP / NIST SP 800-63B).
    Параметри: N=16384 (2^14), r=8, p=1, dklen=64.
    """
    raw_salt = salt or secrets.token_bytes(16)
    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=raw_salt,
        n=16384,
        r=8,
        p=1,
        dklen=64,
    )
    return f"scrypt$16384$8$1${raw_salt.hex()}${derived.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    """Перевіряє пароль за збереженим scrypt-хешем за сталий час (hmac.compare_digest)."""
    try:
        parts = stored_hash.split("$")
        if len(parts) != 6 or parts[0] != "scrypt":
            return False
        n_val, r_val, p_val = int(parts[1]), int(parts[2]), int(parts[3])
        salt = bytes.fromhex(parts[4])
        expected = bytes.fromhex(parts[5])
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=n_val,
            r=r_val,
            p=p_val,
            dklen=len(expected),
        )
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


class SqlUserRepository:
    """Репозиторій роботи з обліковими записами та сесіями у SQLite."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def find_by_username(self, username: str) -> tuple[User, str] | None:
        """Повертає кортеж (User, password_hash) за логіном користувача без чутливості до регістру."""
        row = self.conn.execute(
            "SELECT id, username, display_name, role, password_hash, department "
            "FROM users WHERE LOWER(username) = ?",
            (username.strip().lower(),),
        ).fetchone()
        if not row:
            return None
        user = User(
            id=row["id"],
            username=row["username"],
            display_name=row["display_name"],
            role=UserRole(row["role"]),
            department=row["department"],
        )
        return user, row["password_hash"]

    def find_by_id(self, user_id: str) -> User | None:
        """Повертає доменний об'єкт користувача за його ідентифікатором."""
        row = self.conn.execute(
            "SELECT id, username, display_name, role, department FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        if not row:
            return None
        return User(
            id=row["id"],
            username=row["username"],
            display_name=row["display_name"],
            role=UserRole(row["role"]),
            department=row["department"],
        )

    def create_session(self, user_id: str, max_age_seconds: int = 28800) -> str:
        """Створює захищений серверний токен сесії та очищає прострочені сесії."""
        token = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        expires = now + timedelta(seconds=max_age_seconds)
        self.conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (now_iso,))
        self.conn.execute(
            "INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (token, user_id, now_iso, expires.isoformat()),
        )
        return token

    def get_user_by_session(self, token: str) -> User | None:
        """Повертає користувача за чинним токеном сесії."""
        if not token:
            return None
        now_iso = datetime.now(timezone.utc).isoformat()
        row = self.conn.execute(
            """
            SELECT u.id, u.username, u.display_name, u.role, u.department
            FROM sessions s
            JOIN users u ON u.id = s.user_id
            WHERE s.token = ? AND s.expires_at > ?
            """,
            (token, now_iso),
        ).fetchone()
        if not row:
            return None
        return User(
            id=row["id"],
            username=row["username"],
            display_name=row["display_name"],
            role=UserRole(row["role"]),
            department=row["department"],
        )

    def delete_session(self, token: str) -> None:
        """Видаляє сесію під час виходу з системи."""
        if token:
            self.conn.execute("DELETE FROM sessions WHERE token = ?", (token,))

    def count_recent_failed_attempts(
        self, username: str, window_minutes: int = 15, client_ip: str = ""
    ) -> int:
        """Підраховує кількість невдалих спроб входу за останні window_minutes хвилин для пари (логін, IP)."""
        now = datetime.now(timezone.utc)
        stale_cutoff = (now - timedelta(hours=24)).isoformat()
        self.conn.execute("DELETE FROM login_attempts WHERE attempted_at < ?", (stale_cutoff,))
        cutoff = (now - timedelta(minutes=window_minutes)).isoformat()
        row = self.conn.execute(
            "SELECT COUNT(*) AS cnt FROM login_attempts "
            "WHERE username = ? AND client_ip = ? AND attempted_at >= ?",
            (username.strip().lower(), client_ip.strip(), cutoff),
        ).fetchone()
        return int(row["cnt"]) if row else 0

    def record_failed_attempt(self, username: str, client_ip: str = "") -> None:
        """Фіксує невдалу спробу входу для подальшого обмеження (rate limit)."""
        now_iso = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            "INSERT INTO login_attempts (username, client_ip, attempted_at) VALUES (?, ?, ?)",
            (username.strip().lower(), client_ip.strip(), now_iso),
        )

    def clear_failed_attempts(self, username: str, client_ip: str = "") -> None:
        """Очищає лічильник невдалих спроб після успішної автентифікації."""
        self.conn.execute(
            "DELETE FROM login_attempts WHERE username = ? AND client_ip = ?",
            (username.strip().lower(), client_ip.strip()),
        )
