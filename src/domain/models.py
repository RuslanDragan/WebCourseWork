"""Доменні об'єкти та інваріанти предметної області (відповідно до рис. Д.10 методичних вказівок)."""
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class UserRole(str, Enum):
    """Ролі користувачів системи (таблиця Д.2)."""
    APPLICANT = "APPLICANT"      # Заявник (викладач, організатор навчального заходу)
    DISPATCHER = "DISPATCHER"    # Диспетчер навчального відділу


class BookingStatus(str, Enum):
    """Стани життєвого циклу бронювання (рис. Д.10, таблиця Д.11)."""
    PENDING = "PENDING"          # Очікує погодження (займає слот)
    CONFIRMED = "CONFIRMED"      # Підтверджено диспетчером (займає слот)
    CANCELLED = "CANCELLED"      # Скасовано заявником або диспетчером (історія)
    REJECTED = "REJECTED"        # Відхилено диспетчером із зазначенням причини (історія)

    @property
    def is_active_lock(self) -> bool:
        """Чи блокує поточний статус часовий слот для інших заявок."""
        return self in (BookingStatus.PENDING, BookingStatus.CONFIRMED)


class DomainRuleError(Exception):
    """Порушення предметного правила або інваріанта доменної моделі."""
    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = 400,
        details: list[dict] | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or []


@dataclass
class Room:
    """Навчальна аудиторія з місткістю та переліком оснащення."""
    id: str
    name: str
    building: str
    capacity: int
    equipment: list[str] = field(default_factory=list)
    active: bool = True
    unavailable_reason: str | None = None


@dataclass
class User:
    """Користувач системи (внутрішня доменна модель)."""
    id: str
    username: str
    display_name: str
    role: UserRole
    department: str = ""


@dataclass
class Slot:
    """Фіксований часовий слот навчальної аудиторії (рис. Д.10)."""
    id: str
    room_id: str
    slot_date: str
    pair_number: int
    starts_at: str
    ends_at: str

    def is_time_allowed(self, reference_now: datetime) -> bool:
        """Перевіряє, що слот ще не розпочався відносно поточного моменту (aware Europe/Kyiv)."""
        from src.config import APP_TZ

        slot_start = datetime.fromisoformat(self.starts_at)
        if slot_start.tzinfo is None:
            slot_start = slot_start.replace(tzinfo=APP_TZ)
        else:
            slot_start = slot_start.astimezone(APP_TZ)

        if reference_now.tzinfo is None:
            ref_aware = reference_now.replace(tzinfo=APP_TZ)
        else:
            ref_aware = reference_now.astimezone(APP_TZ)

        return slot_start > ref_aware


@dataclass
class Booking:
    """Заявка на бронювання часового слота (рис. Д.10)."""
    id: str
    slot_id: str
    user_id: str
    status: BookingStatus
    purpose: str
    expected_attendees: int
    created_at: str
    updated_at: str
    decision_comment: str | None = None
    decided_by: str | None = None
    version: int = 1

    def confirm(self, dispatcher_id: str, comment: str = "Погоджено диспетчером") -> None:
        """Переводить заявку зі стану PENDING у стан CONFIRMED."""
        if self.status != BookingStatus.PENDING:
            raise DomainRuleError(
                code="INVALID_STATUS_TRANSITION",
                message=f"Неможливо погодити заявку у стані {self.status.value}. Допустимий перехід лише з PENDING.",
                status_code=409,
            )
        self.status = BookingStatus.CONFIRMED
        self.decided_by = dispatcher_id
        self.decision_comment = comment.strip() or "Погоджено диспетчером"
        self.version += 1

    def reject(self, dispatcher_id: str, reason: str) -> None:
        """Переводить заявку зі стану PENDING у стан REJECTED з обов'язковою причиною."""
        if self.status != BookingStatus.PENDING:
            raise DomainRuleError(
                code="INVALID_STATUS_TRANSITION",
                message=f"Неможливо відхилити заявку у стані {self.status.value}. Допустимий перехід лише з PENDING.",
                status_code=409,
            )
        clean_reason = (reason or "").strip()
        if len(clean_reason) < 5:
            raise DomainRuleError(
                code="REJECTION_REASON_REQUIRED",
                message="Причина відхилення заявки є обов'язковою (щонайменше 5 символів).",
                status_code=400,
            )
        self.status = BookingStatus.REJECTED
        self.decided_by = dispatcher_id
        self.decision_comment = clean_reason
        self.version += 1

    def cancel(self, actor: User, reason: str = "Скасовано користувачем") -> None:
        """Скасовує активну заявку (PENDING або CONFIRMED) власником або диспетчером."""
        if actor.role != UserRole.DISPATCHER and actor.id != self.user_id:
            raise DomainRuleError(
                code="FORBIDDEN_NOT_OWNER",
                message="Заявник може скасовувати лише власні бронювання.",
                status_code=403,
            )
        if self.status not in (BookingStatus.PENDING, BookingStatus.CONFIRMED):
            raise DomainRuleError(
                code="INVALID_STATUS_TRANSITION",
                message=f"Неможливо скасувати заявку у стані {self.status.value}, оскільки вона вже не є активною.",
                status_code=409,
            )
        self.status = BookingStatus.CANCELLED
        self.decision_comment = (reason or "").strip() or "Скасовано користувачем"
        if actor.role == UserRole.DISPATCHER:
            self.decided_by = actor.id
        self.version += 1


@dataclass
class BookingEvent:
    """Запис історії зміни станів бронювання (рис. Д.9)."""
    id: str
    booking_id: str
    actor_id: str
    old_status: str | None
    new_status: str
    comment: str
    occurred_at: str
