"""
Модульні тести: доменні сутності, інваріанти переходів станів, безпека хешування/CSRF,
завантаження .env та алгоритм ранжованого пошуку найближчої вільної альтернативи.
"""
from datetime import datetime, timezone
from pathlib import Path
import sys

import pytest

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import src.config as config
import src.database as db_module
from seed.seed_data import demo_dates, seed_database, slot_id
from src.domain.models import Booking, BookingStatus, DomainRuleError, Slot, User, UserRole
from src.repositories.booking_repository import SqlBookingRepository
from src.repositories.room_repository import SqlRoomRepository
from src.repositories.user_repository import hash_password, verify_password
from src.services.auth_service import generate_csrf_token, verify_csrf_token
from src.services.booking_service import BookingService


def _make_user(role: UserRole, uid: str = "u-1") -> User:
    return User(id=uid, username="user", display_name="Test User", role=role)


def _make_booking(status: BookingStatus = BookingStatus.PENDING, uid: str = "u-1") -> Booking:
    return Booking(
        id="bk-unit-1",
        slot_id="slot-1",
        user_id=uid,
        status=status,
        purpose="Лабораторне заняття",
        expected_attendees=20,
        created_at="2026-11-10T10:00:00+02:00",
        updated_at="2026-11-10T10:00:00+02:00",
        version=1,
    )


def test_booking_state_machine_transitions() -> None:
    """Перевірка допустимих та заборонених переходів станів бронювання."""
    bk = _make_booking(BookingStatus.PENDING)
    assert bk.status.is_active_lock is True

    bk.confirm(dispatcher_id="disp-1", comment="Погоджено")
    assert bk.status == BookingStatus.CONFIRMED
    assert bk.version == 2
    assert bk.status.is_active_lock is True

    # Повторне підтвердження вже підтвердженої заявки заборонено (409)
    with pytest.raises(DomainRuleError) as exc_info:
        bk.confirm(dispatcher_id="disp-1", comment="Повтор")
    assert exc_info.value.status_code == 409
    assert exc_info.value.code == "INVALID_STATUS_TRANSITION"

    # Скасування власником з CONFIRMED дозволено
    owner = _make_user(UserRole.APPLICANT, uid="u-1")
    bk.cancel(actor=owner, reason="Зміна планів")
    assert bk.status == BookingStatus.CANCELLED
    assert bk.status.is_active_lock is False
    assert bk.version == 3

    # Повторне скасування неактивної заявки заборонено (409)
    with pytest.raises(DomainRuleError) as exc_cancel:
        bk.cancel(actor=owner)
    assert exc_cancel.value.status_code == 409


def test_booking_reject_requires_reason_and_owner_check() -> None:
    """Відхилення без причини (<5 симв.) блокується; чужий заявник не може скасувати заявку."""
    bk = _make_booking(BookingStatus.PENDING, uid="u-owner")
    with pytest.raises(DomainRuleError) as exc_short:
        bk.reject(dispatcher_id="disp-1", reason="ні")
    assert exc_short.value.code == "REJECTION_REASON_REQUIRED"

    stranger = _make_user(UserRole.APPLICANT, uid="u-stranger")
    with pytest.raises(DomainRuleError) as exc_owner:
        bk.cancel(actor=stranger)
    assert exc_owner.value.status_code == 403
    assert exc_owner.value.code == "FORBIDDEN_NOT_OWNER"


def test_slot_time_validation() -> None:
    """Перевірка порівняння часу початку слота в Europe/Kyiv (зимовий +02:00, літній +03:00 та UTC-сервер)."""
    from seed.seed_data import _ts

    assert _ts("2026-11-20", "10:20") == "2026-11-20T10:20:00+02:00"
    assert _ts("2026-07-15", "10:20") == "2026-07-15T10:20:00+03:00"

    slot = Slot(
        id="s1",
        room_id="r1",
        slot_date="2026-11-20",
        pair_number=2,
        starts_at="2026-11-20T10:20:00+02:00",
        ends_at="2026-11-20T11:55:00+02:00",
    )
    assert slot.is_time_allowed(datetime(2026, 11, 20, 10, 0)) is True
    assert slot.is_time_allowed(datetime(2026, 11, 20, 10, 20)) is False
    assert slot.is_time_allowed(datetime(2026, 11, 20, 11, 0)) is False

    # Сценарій сервера в UTC: 10:20+02:00 == 08:20 UTC; о 08:25 UTC слот уже розпочався 5 хв тому
    assert slot.is_time_allowed(datetime(2026, 11, 20, 8, 15, tzinfo=timezone.utc)) is True
    assert slot.is_time_allowed(datetime(2026, 11, 20, 8, 25, tzinfo=timezone.utc)) is False

    # Літній слот (+03:00): 10:20+03:00 == 07:20 UTC
    summer_slot = Slot(
        id="s2",
        room_id="r1",
        slot_date="2026-07-15",
        pair_number=2,
        starts_at="2026-07-15T10:20:00+03:00",
        ends_at="2026-07-15T11:55:00+03:00",
    )
    assert summer_slot.is_time_allowed(datetime(2026, 7, 15, 7, 15, tzinfo=timezone.utc)) is True
    assert summer_slot.is_time_allowed(datetime(2026, 7, 15, 7, 25, tzinfo=timezone.utc)) is False


def test_password_scrypt_and_csrf_hmac() -> None:
    """Модульна перевірка криптографічного хешування пароля та CSRF-токена."""
    stored = hash_password("StrongPass2026!")
    assert stored.startswith("scrypt$16384$8$1$")
    assert verify_password("StrongPass2026!", stored) is True
    assert verify_password("WrongPass!", stored) is False
    assert verify_password("any", "invalid-hash-format") is False

    csrf = generate_csrf_token("session-abc-123")
    assert verify_csrf_token("session-abc-123", csrf) is True
    assert verify_csrf_token("session-abc-123", "tampered-token") is False
    assert verify_csrf_token("other-session", csrf) is False
    assert verify_csrf_token("", csrf) is False


def test_alternative_ranking_algorithm_rules(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Перевірка алгоритму пошуку найближчої альтернативи:
    - не включає цільовий слот, зайняті слоти та неактивні аудиторії (room-107);
    - враховує місткість та обладнання;
    - ранжує за спаданням similarityScore (найближча пара і той самий корпус мають найвищий бал).
    """
    db_path = tmp_path / "unit_alt.db"
    monkeypatch.setattr(config, "DATABASE_PATH", db_path)
    monkeypatch.setattr(db_module, "DATABASE_PATH", db_path)
    seed_database(db_path)
    d = demo_dates()

    conn = db_module.get_connection(db_path)
    try:
        service = BookingService(SqlBookingRepository(conn), SqlRoomRepository(conn))
        occupied_slot = slot_id("room-102", d["mon"], 2)
        alts = service.find_nearest_alternatives(
            slot_id=occupied_slot,
            min_capacity=30,
            required_equipment=["Проєктор", "ПК"],
            limit=5,
        )
        assert len(alts) > 0
        # Жодна альтернатива не є закритою room-107 або тим самим зайнятим слотом
        assert all(a["roomId"] != "room-107" and a["slotId"] != occupied_slot for a in alts)
        # Усі задовольняють місткість і оснащення
        assert all(a["capacity"] >= 30 for a in alts)
        # Перевірка монотонного незростання рейтингу близькості
        scores = [a["similarityScore"] for a in alts]
        assert scores == sorted(scores, reverse=True)
        # Та сама пара (2-га) в тій самій аудиторії або іншому ПК-класі (room-103 зайнята на 2-гу пару) -> 1-ша або 3-тя пара у room-102 має бути найвищою
        assert alts[0]["pairNumber"] in (1, 3)
        assert alts[0]["roomId"] in ("room-102", "room-103")

        # Якщо зараз 13:00 у день d["mon"], пари 1–3 уже розпочалися/минули -> пропонуються лише пари >= 4
        mon_dt = datetime.fromisoformat(f"{d['mon']}T13:00:00").replace(tzinfo=config.APP_TZ)
        late_alts = service.find_nearest_alternatives(
            slot_id=slot_id("room-102", d["mon"], 4),
            min_capacity=25,
            required_equipment=[],
            reference_now=mon_dt,
        )
        assert len(late_alts) > 0
        assert all(a["pairNumber"] >= 4 for a in late_alts)
    finally:
        conn.close()


def test_dotenv_loader_and_past_booking_immutability(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Перевірка парсера .env, захисту від порожньої причини скасування та незмінності минулих заявок."""
    env_file = tmp_path / ".env"
    env_file.write_text("# comment\nCUSTOM_TEST_KEY='hello_dotenv'\n", encoding="utf-8")
    monkeypatch.delenv("CUSTOM_TEST_KEY", raising=False)
    config.load_dotenv_file(env_file)
    assert config.os.getenv("CUSTOM_TEST_KEY") == "hello_dotenv"

    # Перевірка fallback-коментаря та decided_by при скасуванні диспетчером із пробілами
    bk = _make_booking(BookingStatus.CONFIRMED, uid="u-owner")
    disp = _make_user(UserRole.DISPATCHER, uid="u-disp")
    bk.cancel(actor=disp, reason="   ")
    assert bk.decision_comment == "Скасовано користувачем"
    assert bk.decided_by == "u-disp"

    # Заявку, часовий слот якої вже минув, не можна скасувати чи погодити заднім числом (409)
    db_path = tmp_path / "immut.db"
    monkeypatch.setattr(config, "DATABASE_PATH", db_path)
    monkeypatch.setattr(db_module, "DATABASE_PATH", db_path)
    seed_database(db_path)
    d = demo_dates()
    conn = db_module.get_connection(db_path)
    try:
        service = BookingService(SqlBookingRepository(conn), SqlRoomRepository(conn))
        after_mon = datetime.fromisoformat(f"{d['mon']}T20:00:00").replace(tzinfo=config.APP_TZ)
        applicant = User(id="usr-applicant-1", username="kovalenko", display_name="Коваленко", role=UserRole.APPLICANT)
        dispatcher = User(id="usr-dispatcher-1", username="dispatcher", display_name="Мельник", role=UserRole.DISPATCHER)

        with pytest.raises(DomainRuleError) as exc_c:
            service.cancel_booking(applicant, "bk-demo-pending-02", reference_now=after_mon)
        assert exc_c.value.status_code == 409
        assert exc_c.value.code == "PAST_BOOKING_IMMUTABLE"

        with pytest.raises(DomainRuleError) as exc_d:
            service.decide_booking(dispatcher, "bk-demo-pending-02", "CONFIRM", "Ок", reference_now=after_mon)
        assert exc_d.value.status_code == 409
        assert exc_d.value.code == "PAST_BOOKING_IMMUTABLE"
    finally:
        conn.close()
