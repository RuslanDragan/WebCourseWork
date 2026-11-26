"""
Інтеграційні та безпекові тести адміністративного контуру і захисту вебсистеми:
- Тижневий календар (7 днів x 6 пар)
- Керування доступністю аудиторій диспетчером (409 при наявності активних заявок)
- Журнал аудиту рішень диспетчера
- CSRF-захист, перевірка Origin (включно з Origin: null), заголовки безпеки (CSP, X-Frame-Options, nosniff)
- Обмеження спроб входу (429 Too Many Requests після 5 невдалих спроб)
- Бронювання альтернативи в 1 клік та скасування заявки заявником
"""
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
    db_path = tmp_path / "test_stage67.db"
    monkeypatch.setattr(config, "DATABASE_PATH", db_path)
    monkeypatch.setattr(db_module, "DATABASE_PATH", db_path)
    seed_database(db_path)
    return db_path


def _login(client: TestClient, username: str, password: str) -> dict[str, str]:
    res = client.post("/api/auth/login", json={"username": username, "password": password})
    assert res.status_code == 200
    return {"X-CSRF-Token": res.json()["csrfToken"]}


def test_weekly_calendar_7x6_query(isolated_db: Path) -> None:
    """Перевірка вибірки тижневого календаря (7 днів x 6 пар = 42 слоти для однієї аудиторії)."""
    d = demo_dates()
    with TestClient(app) as client:
        res = client.get(
            "/api/slots",
            params={"dateFrom": d["mon"], "dateTo": d["sun"], "roomId": "room-101"},
        )
        assert res.status_code == 200
        slots = res.json()
        assert len(slots) == 42
        assert all(s["roomId"] == "room-101" for s in slots)
        # 1-ша пара в понеділок зайнята демонстраційною підтвердженою заявкою
        mon_p1 = next(s for s in slots if s["slotDate"] == d["mon"] and s["pairNumber"] == 1)
        assert mon_p1["isOccupied"] is True
        assert mon_p1["activeBookingStatus"] == "CONFIRMED"


def test_dispatcher_room_availability_and_conflict_guard(isolated_db: Path) -> None:
    """
    Керування доступністю аудиторій:
    - заявник не може закривати аудиторію (403);
    - диспетчер не може закрити аудиторію, якщо в ній є майбутні активні заявки (409 зі списком деталей);
    - диспетчер успішно закриває вільну аудиторію (200), нове бронювання в неї блокується (409 ROOM_UNAVAILABLE);
    - після відкриття бронювання знову можливе (201).
    """
    d = demo_dates()
    with TestClient(app) as client:
        app_headers = _login(client, "kovalenko", "ApplicantPass2026!")
        forbidden_res = client.patch(
            "/api/rooms/room-104/availability",
            headers=app_headers,
            json={"active": False, "reason": "Спроба закриття заявником"},
        )
        assert forbidden_res.status_code == 403

        disp_headers = _login(client, "dispatcher", "DispatcherPass2026!")

        # room-101 має майбутнє CONFIRMED бронювання -> 409 зі списком конфліктних бронювань
        conflict_res = client.patch(
            "/api/rooms/room-101/availability",
            headers=disp_headers,
            json={"active": False, "reason": "Аварійне відключення електроенергії"},
        )
        assert conflict_res.status_code == 409
        body = conflict_res.json()
        assert body["code"] == "ROOM_HAS_ACTIVE_BOOKINGS"
        assert len(body["details"]) == 1
        assert body["details"][0]["bookingId"] == "bk-demo-confirmed-01"

        # room-104 має лише CANCELLED заявку -> закриття дозволено
        close_res = client.patch(
            "/api/rooms/room-104/availability",
            headers=disp_headers,
            json={"active": False, "reason": "Проведення планового ремонту проєктора"},
        )
        assert close_res.status_code == 200
        assert close_res.json()["active"] is False
        assert "ремонту" in close_res.json()["unavailableReason"]

        # Заявник не може забронювати закриту аудиторію
        app_headers = _login(client, "kovalenko", "ApplicantPass2026!")
        blocked_bk = client.post(
            "/api/bookings",
            headers=app_headers,
            json={
                "slotId": slot_id("room-104", d["mon"], 2),
                "purpose": "Заняття в закритій аудиторії",
                "expectedAttendees": 20,
            },
        )
        assert blocked_bk.status_code == 409
        assert blocked_bk.json()["code"] == "ROOM_UNAVAILABLE"

        # Диспетчер відкриває аудиторію, і заявник успішно створює бронювання
        disp_headers = _login(client, "dispatcher", "DispatcherPass2026!")
        open_res = client.patch(
            "/api/rooms/room-104/availability",
            headers=disp_headers,
            json={"active": True, "reason": "Ремонт завершено, обладнання справне"},
        )
        assert open_res.status_code == 200
        assert open_res.json()["active"] is True

        app_headers = _login(client, "kovalenko", "ApplicantPass2026!")
        ok_bk = client.post(
            "/api/bookings",
            headers=app_headers,
            json={
                "slotId": slot_id("room-104", d["mon"], 2),
                "purpose": "Заняття після відкриття аудиторії",
                "expectedAttendees": 20,
            },
        )
        assert ok_bk.status_code == 201


def test_dispatcher_audit_log_and_applicant_cancel(isolated_db: Path) -> None:
    """Перевірка журналу аудиту диспетчера та скасування/відхилення заявок."""
    with TestClient(app) as client:
        app_headers = _login(client, "kovalenko", "ApplicantPass2026!")
        # Заявнику журнал аудиту недоступний
        assert client.get("/api/audit").status_code == 403

        # Скасування власної PENDING заявки заявником
        cancel_res = client.post(
            "/api/bookings/bk-demo-pending-02/cancel",
            headers=app_headers,
            json={"reason": "Перенесено консультацію"},
        )
        assert cancel_res.status_code == 200
        assert cancel_res.json()["status"] == "CANCELLED"

        disp_headers = _login(client, "dispatcher", "DispatcherPass2026!")
        client.patch(
            "/api/rooms/room-107/availability",
            headers=disp_headers,
            json={"active": True, "reason": "Аудиторію введено в експлуатацію"},
        )

        audit_all = client.get("/api/audit")
        assert audit_all.status_code == 200
        events = audit_all.json()
        assert len(events) >= 8
        entity_types = {e["entityType"] for e in events}
        assert entity_types == {"BOOKING", "ROOM"}

        audit_room = client.get("/api/audit", params={"entityType": "ROOM"})
        assert audit_room.status_code == 200
        assert all(e["entityType"] == "ROOM" for e in audit_room.json())


def test_security_headers_csrf_and_origin(isolated_db: Path) -> None:
    """Перевірка заголовків безпеки, обов'язковості X-CSRF-Token та перевірки Origin (включно з Origin: null)."""
    d = demo_dates()
    target_slot = slot_id("room-105", d["wed"], 2)
    with TestClient(app) as client:
        # Перевірка HTTP-заголовків безпеки на головній сторінці й API
        page_res = client.get("/")
        assert page_res.status_code == 200
        assert page_res.headers.get("x-content-type-options") == "nosniff"
        assert page_res.headers.get("x-frame-options") == "DENY"
        assert "default-src 'self'" in page_res.headers.get("content-security-policy", "")
        assert page_res.headers.get("referrer-policy") == "strict-origin-when-cross-origin"

        _login(client, "kovalenko", "ApplicantPass2026!")

        # Мутація без X-CSRF-Token відхиляється (403)
        no_csrf = client.post(
            "/api/bookings",
            json={"slotId": target_slot, "purpose": "Запит без CSRF токена", "expectedAttendees": 10},
        )
        assert no_csrf.status_code == 403
        assert no_csrf.json()["code"] == "INVALID_CSRF_TOKEN"

        # Мутація з підробленим X-CSRF-Token відхиляється (403)
        bad_csrf = client.post(
            "/api/bookings",
            headers={"X-CSRF-Token": "0" * 64},
            json={"slotId": target_slot, "purpose": "Запит з фальшивим CSRF", "expectedAttendees": 10},
        )
        assert bad_csrf.status_code == 403

        # Мутація з чужим заголовком Origin відхиляється (403)
        me_res = client.get("/api/auth/me")
        valid_csrf = me_res.json()["csrfToken"]
        bad_origin = client.post(
            "/api/bookings",
            headers={"X-CSRF-Token": valid_csrf, "Origin": "https://attacker.example.com"},
            json={"slotId": target_slot, "purpose": "Міжсайтовий запит", "expectedAttendees": 10},
        )
        assert bad_origin.status_code == 403
        assert bad_origin.json()["code"] == "INVALID_ORIGIN"

        # Sandboxed / opaque Origin: null також блокується (OWASP CSRF)
        null_origin = client.post(
            "/api/bookings",
            headers={"X-CSRF-Token": valid_csrf, "Origin": "null"},
            json={"slotId": target_slot, "purpose": "Запит із Origin: null", "expectedAttendees": 10},
        )
        assert null_origin.status_code == 403
        assert null_origin.json()["code"] == "INVALID_ORIGIN"


def test_login_rate_limiting_429(isolated_db: Path) -> None:
    """Блокування входу після 5 невдалих спроб кодом 429 Too Many Requests."""
    with TestClient(app) as client:
        for _ in range(5):
            r = client.post("/api/auth/login", json={"username": "kovalenko", "password": "WrongPassword!"})
            assert r.status_code == 401

        # 6-та спроба блокується навіть із правильним паролем
        locked = client.post("/api/auth/login", json={"username": "kovalenko", "password": "ApplicantPass2026!"})
        assert locked.status_code == 429
        assert locked.json()["code"] == "TOO_MANY_LOGIN_ATTEMPTS"


def test_logout_filters_rejection_and_validation_errors(isolated_db: Path) -> None:
    """Додаткове покриття: logout, GET /api/bookings фільтри, REJECT, CAPACITY_EXCEEDED, 404, 400 validation."""
    d = demo_dates()
    with TestClient(app) as client:
        app_headers = _login(client, "kovalenko", "ApplicantPass2026!")

        # Власні бронювання (фільтри і заборона allRequests=true для заявника)
        my_list = client.get("/api/bookings")
        assert my_list.status_code == 200
        assert len(my_list.json()) >= 3
        assert client.get("/api/bookings", params={"allRequests": True}).status_code == 403
        assert client.get("/api/bookings", params={"status": "UNKNOWN"}).status_code == 400

        # Перевищення місткості (room-105 вміщує 24, просимо 50 -> 400 CAPACITY_EXCEEDED)
        cap_err = client.post(
            "/api/bookings",
            headers=app_headers,
            json={
                "slotId": slot_id("room-105", d["thu"], 1),
                "purpose": "Занадто велика група студентів",
                "expectedAttendees": 50,
            },
        )
        assert cap_err.status_code == 400
        assert cap_err.json()["code"] == "CAPACITY_EXCEEDED"

        # Валідаційна помилка Pydantic DTO (занадто коротка мета -> 400 VALIDATION_ERROR з fieldErrors)
        val_err = client.post(
            "/api/bookings",
            headers=app_headers,
            json={"slotId": slot_id("room-105", d["thu"], 1), "purpose": "Ок", "expectedAttendees": 10},
        )
        assert val_err.status_code == 400
        assert val_err.json()["code"] == "VALIDATION_ERROR"
        assert len(val_err.json()["fieldErrors"]) >= 1

        # Неіснуюче бронювання та неіснуючий слот (404)
        assert client.get("/api/bookings/bk-missing").status_code == 404
        assert (
            client.post(
                "/api/bookings",
                headers=app_headers,
                json={"slotId": "slot-nonexistent", "purpose": "Спроба неіснуючого слота", "expectedAttendees": 5},
            ).status_code
            == 404
        )

        # Вихід (logout) інвалідує сесію, а повторний logout із простроченою кукою теж повертає 204
        assert client.post("/api/auth/logout", headers=app_headers).status_code == 204
        assert client.get("/api/auth/me").status_code == 401
        client.cookies.set(config.SESSION_COOKIE_NAME, "expired-or-deleted-session-token")
        assert client.post("/api/auth/logout").status_code == 204
        client.cookies.clear()

        # Вхід не чутливий до регістру логіна (Dispatcher -> dispatcher)
        disp_headers = _login(client, "Dispatcher", "DispatcherPass2026!")
        # Ідемпотентне повторне відкриття вже активної аудиторії не дублює події аудиту
        audit_before = len(client.get("/api/audit", params={"entityType": "ROOM"}).json())
        idem_res = client.patch(
            "/api/rooms/room-101/availability",
            headers=disp_headers,
            json={"active": True, "reason": "Підтвердження активного стану"},
        )
        assert idem_res.status_code == 200
        audit_after = len(client.get("/api/audit", params={"entityType": "ROOM"}).json())
        assert audit_after == audit_before
        all_pending = client.get("/api/bookings", params={"allRequests": True, "status": "PENDING"}).json()
        assert len(all_pending) >= 1
        rej_res = client.post(
            f"/api/bookings/{all_pending[0]['id']}/decision",
            headers=disp_headers,
            json={"action": "REJECT", "comment": "Заплановане профілактичне обслуговування"},
        )
        assert rej_res.status_code == 200
        assert rej_res.json()["status"] == "REJECTED"

