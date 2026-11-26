"""Сервіс автентифікації, захисту від підбору пароля, CSRF та перевірки повноважень."""
import hashlib
import hmac

from src.config import LOGIN_LOCK_MINUTES, LOGIN_MAX_FAILED_ATTEMPTS, SECRET_KEY
from src.domain.models import DomainRuleError, User, UserRole
from src.repositories.user_repository import SqlUserRepository, hash_password, verify_password

_DUMMY_PASSWORD_HASH = hash_password("TimingGuardDummyPass2026!", salt=b"0123456789abcdef")


def generate_csrf_token(session_token: str) -> str:
    """Генерує HMAC-SHA256 CSRF-токен, прив'язаний до серверної сесії."""
    return hmac.new(
        SECRET_KEY.encode("utf-8"),
        f"csrf:{session_token}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def verify_csrf_token(session_token: str, provided_token: str | None) -> bool:
    """Перевіряє валідність CSRF-токена за сталий час."""
    if not session_token or not provided_token:
        return False
    expected = generate_csrf_token(session_token)
    return hmac.compare_digest(expected, provided_token.strip())


class AuthService:
    """Керування входом, виходом, rate-limiting та перевіркою ролей."""

    def __init__(self, user_repo: SqlUserRepository):
        self.user_repo = user_repo

    def authenticate(
        self,
        username: str,
        password: str,
        max_age_seconds: int = 28800,
        client_ip: str = "",
    ) -> tuple[User, str]:
        """Перевіряє обмеження спроб входу (429), облікові дані за сталий час та створює серверну сесію."""
        failed_cnt = self.user_repo.count_recent_failed_attempts(
            username, window_minutes=LOGIN_LOCK_MINUTES, client_ip=client_ip
        )
        if failed_cnt >= LOGIN_MAX_FAILED_ATTEMPTS:
            raise DomainRuleError(
                code="TOO_MANY_LOGIN_ATTEMPTS",
                message=(
                    f"Забагато невдалих спроб входу ({LOGIN_MAX_FAILED_ATTEMPTS}). "
                    f"Повторіть спробу через {LOGIN_LOCK_MINUTES} хв."
                ),
                status_code=429,
            )

        found = self.user_repo.find_by_username(username)
        if not found:
            verify_password(password, _DUMMY_PASSWORD_HASH)
            self.user_repo.record_failed_attempt(username, client_ip=client_ip)
            raise DomainRuleError(
                code="INVALID_CREDENTIALS",
                message="Невірний логін або пароль користувача.",
                status_code=401,
            )
        user, password_hash = found
        if not verify_password(password, password_hash):
            self.user_repo.record_failed_attempt(username, client_ip=client_ip)
            raise DomainRuleError(
                code="INVALID_CREDENTIALS",
                message="Невірний логін або пароль користувача.",
                status_code=401,
            )
        self.user_repo.clear_failed_attempts(username, client_ip=client_ip)
        token = self.user_repo.create_session(user.id, max_age_seconds=max_age_seconds)
        return user, token

    def require_authenticated(self, token: str | None) -> User:
        """Гарантує наявність чинної сесії користувача."""
        if not token:
            raise DomainRuleError(
                code="UNAUTHORIZED",
                message="Для виконання цієї дії необхідно увійти до системи.",
                status_code=401,
            )
        user = self.user_repo.get_user_by_session(token)
        if not user:
            raise DomainRuleError(
                code="SESSION_EXPIRED",
                message="Сесія недійсна або її строк дії закінчився. Увійдіть повторно.",
                status_code=401,
            )
        return user

    @staticmethod
    def require_dispatcher(user: User) -> None:
        """Гарантує, що користувач має роль диспетчера (DISPATCHER)."""
        if user.role != UserRole.DISPATCHER:
            raise DomainRuleError(
                code="FORBIDDEN_ROLE",
                message="Недостатньо прав доступу: операція дозволена лише диспетчеру.",
                status_code=403,
            )
