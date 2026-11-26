"""
Автоматизовані інтеграційні тести основного наскрізного сценарію бронювання.
Перевіряють запуск із порожньою БД, міграції, seed-наповнення, читання каталогу, створення заявки,
відмову для зайнятого слота (409), минулої дати (400), неактивної аудиторії (409), чужої заявки (403, TC-ACCESS-02),
погодження диспетчером, алгоритм пошуку найближчої вільної альтернативи та конкурентні запити.
"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys

from fastapi.testclient import TestClient
import pytest

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import src.config as config
import src.database as db_module
from seed.seed_data import demo_dates, seed_database, slot_id
from src.main import app


@pytest.fixture()
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Створює ізольовану SQLite базу даних для кожного тесту та наповнює її seed-даними."""
    test_db_path = tmp_path / "test_booking.db"
    monkeypatch.setattr(config, "DATABASE_PATH", test_db_path)
    monkeypatch.setattr(db_module, "DATABASE_PATH", test_db_path)
    seed_database(test_db_path)
    # Перевірка ідемпотентності повторного запуску seed-скрипту
    seed_database(test_db_path)
    return test_db_path


def _login(client: TestClient, username: str, password: str) -> dict[str, str]:
    res = client.post("/api/auth/login", json={"username": username, "password": password})
    assert res.status_code == 200
    csrf = res.json()["csrfToken"]
    return {"X-CSRF-Token": csrf}


def test_health_and_catalog_filtering(isolated_db: Path) -> None:
    """Перевірка запуску, з'єднання з БД та фільтрації каталогу аудиторій і слотів."""
    d = demo_dates()
    with TestClient(app) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["status"] == "UP"
        assert health.json()["roomsCount"] == 7

        rooms_res = client.get("/api/rooms", params={"minCapacity": 50, "onlyActive": True})
        assert rooms_res.status_code == 200
        rooms = rooms_res.json()
        assert len(rooms) == 2
        assert all(r["capacity"] >= 50 and r["active"] is True for r in rooms)

        slots_res = client.get("/api/slots", params={"date": d["mon"], "equipment": "Інтерактивна дошка"})
        assert slots_res.status_code == 200
        slots = slots_res.json()
        assert len(slots) > 0
        assert all("Інтерактивна дошка" in s["roomEquipment"] for s in slots)


def test_end_to_end_booking_lifecycle(isolated_db: Path) -> None:
    """
    Наскрізний сценарій:
    Вхід заявника -> Створення заявки на вільний слот -> Перевірка збереження ->
    Повторна спроба (409) -> Вхід диспетчера -> Погодження заявки.
    """
    d = demo_dates()
    target_slot = slot_id("room-104", d["mon"], 1)
    with TestClient(app) as client:
        # 1. Спроба створити заявку без сесії -> 401
        unauth_res = client.post(
            "/api/bookings",
            json={"slotId": target_slot, "purpose": "Практичне заняття з вебдизайну", "expectedAttendees": 25},
        )
        assert unauth_res.status_code == 401

        # 2. Вхід заявника (kovalenko)
        headers_app = _login(client, "kovalenko", "ApplicantPass2026!")

        # 3. Створення бронювання на вільний слот (FR-BOOK-01)
        create_res = client.post(
            "/api/bookings",
            headers=headers_app,
            json={
                "slotId": target_slot,
                "purpose": "Практичне заняття з вебдизайну для групи АІ-243",
                "expectedAttendees": 25,
            },
        )
        assert create_res.status_code == 201
        created_bk = create_res.json()
        booking_id = created_bk["id"]
        assert created_bk["status"] == "PENDING"
        assert created_bk["slotId"] == target_slot
        assert len(created_bk["history"]) == 1

        # 4. Повторне створення на той самий слот повертає 409 Conflict (без дубліката)
        dup_res = client.post(
            "/api/bookings",
            headers=headers_app,
            json={
                "slotId": target_slot,
                "purpose": "Спроба повторного бронювання того самого слота",
                "expectedAttendees": 20,
            },
        )
        assert dup_res.status_code == 409
        assert dup_res.json()["code"] == "SLOT_ALREADY_BOOKED"

        # 5. Вхід диспетчера та погодження заявки
        headers_disp = _login(client, "dispatcher", "DispatcherPass2026!")
        confirm_res = client.post(
            f"/api/bookings/{booking_id}/decision",
            headers=headers_disp,
            json={"action": "CONFIRM", "comment": "Аудиторію погоджено за розкладом"},
        )
        assert confirm_res.status_code == 200
        confirmed_bk = confirm_res.json()
        assert confirmed_bk["status"] == "CONFIRMED"
        assert len(confirmed_bk["history"]) == 2


def test_negative_rules_and_access_control(isolated_db: Path) -> None:
    """
    Перевірка негативних сценаріїв та розмежування прав доступу:
    - TC-ACCESS-02: заборона читання чужої заявки (403)
    - заборона бронювання на минулу дату (400)
    - заборона бронювання неактивної аудиторії (409)
    - пошук найближчої альтернативи
    """
    d = demo_dates()
    with TestClient(app) as client:
        headers_app = _login(client, "kovalenko", "ApplicantPass2026!")

        # TC-ACCESS-02: Заявник kovalenko намагається відкрити чужу заявку користувача bondarenko
        foreign_res = client.get("/api/bookings/bk-demo-other-user-03")
        assert foreign_res.status_code == 403
        assert foreign_res.json()["code"] == "FORBIDDEN_PRIVATE_BOOKING"

        # Заборона створення на минулу дату
        past_res = client.post(
            "/api/bookings",
            headers=headers_app,
            json={
                "slotId": slot_id("room-101", d["past"], 1),
                "purpose": "Спроба бронювання слота в минулому",
                "expectedAttendees": 20,
            },
        )
        assert past_res.status_code == 400
        assert past_res.json()["code"] == "PAST_SLOT_NOT_ALLOWED"

        # Заборона створення в неактивній аудиторії (room-107)
        inactive_res = client.post(
            "/api/bookings",
            headers=headers_app,
            json={
                "slotId": slot_id("room-107", d["mon"], 1),
                "purpose": "Спроба бронювання аудиторії на ремонті",
                "expectedAttendees": 20,
            },
        )
        assert inactive_res.status_code == 409
        assert inactive_res.json()["code"] == "ROOM_UNAVAILABLE"

        # Пошук найближчої вільної альтернативи для зайнятого слота (room-102, mon, 2 пара)
        alt_res = client.get(f"/api/slots/{slot_id('room-102', d['mon'], 2)}/alternatives", params={"minCapacity": 25})
        assert alt_res.status_code == 200
        alternatives = alt_res.json()
        assert len(alternatives) > 0
        assert alternatives[0]["similarityScore"] >= alternatives[-1]["similarityScore"]


def test_concurrent_booking_requests(isolated_db: Path) -> None:
    """
    Перевірка конкурентного доступу (Д.2.2, Д.5.2):
    Два одночасні запити на один вільний слот повинні дати рівно один успіх (201) та одну відмову (409),
    без часткових записів у таблиці bookings.
    """
    from src.domain.models import DomainRuleError
    from src.repositories.booking_repository import SqlBookingRepository
    from src.repositories.room_repository import SqlRoomRepository
    from src.repositories.user_repository import SqlUserRepository
    from src.services.booking_service import BookingService

    d = demo_dates()
    target_slot_id = slot_id("room-106", d["tue"], 3)

    def worker(username: str) -> int:
        conn = db_module.get_connection(isolated_db)
        try:
            user_tuple = SqlUserRepository(conn).find_by_username(username)
            assert user_tuple is not None
            user, _ = user_tuple
            service = BookingService(SqlBookingRepository(conn), SqlRoomRepository(conn))
            try:
                service.create(
                    user=user,
                    slot_id=target_slot_id,
                    purpose=f"Конкурентний запит від {username}",
                    expected_attendees=30,
                )
                return 201
            except DomainRuleError as exc:
                return exc.status_code
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(worker, "kovalenko"), pool.submit(worker, "bondarenko")]
        statuses = sorted(f.result() for f in futures)

    assert statuses == [201, 409]

    # Переконуємося, що в БД записано рівно одну активну заявку для цього слота
    conn = db_module.get_connection(isolated_db)
    try:
        cnt = conn.execute(
            "SELECT COUNT(*) AS c FROM bookings WHERE slot_id = ? AND status IN ('PENDING', 'CONFIRMED')",
            (target_slot_id,),
        ).fetchone()["c"]
        assert cnt == 1
    finally:
        conn.close()
