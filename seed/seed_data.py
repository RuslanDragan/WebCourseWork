"""
Скрипт початкового наповнення бази даних синтетичними демонстраційними записами (seed).
Забезпечує ідемпoтентний запуск (безпечний повторний запуск без неконтрольованого дублювання)
та створює нормальні записи, граничні значення, зайняті слоти, неактивну аудиторію і записи іншого користувача.
"""
from datetime import date, datetime, timedelta
from pathlib import Path
import json
import os
import sys

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.config import ACADEMIC_PAIRS, APP_TZ
from src.database import get_connection, run_migrations, transaction
from src.repositories.user_repository import hash_password


def demo_base_monday(today: date | None = None) -> date:
    """
    Перший день демонстраційного тижня — найближчий наступний понеділок (строго після сьогодні).
    Завдяки цьому демо-слоти завжди в майбутньому, а тести не «старіють» з плином календаря.
    Для відтворюваного запуску дату можна зафіксувати змінною середовища SEED_BASE_DATE=YYYY-MM-DD.
    """
    override = os.getenv("SEED_BASE_DATE")
    if override:
        return date.fromisoformat(override)
    today = today or datetime.now(APP_TZ).date()
    days_ahead = (7 - today.weekday()) % 7 or 7
    return today + timedelta(days=days_ahead)


def demo_dates() -> dict[str, str]:
    """Іменовані демонстраційні дати (ISO 8601): минула дата та 7 днів наступного тижня (Пн–Нд)."""
    monday = demo_base_monday()
    names = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    result = {name: (monday + timedelta(days=i)).isoformat() for i, name in enumerate(names)}
    result["past"] = (monday - timedelta(days=15)).isoformat()
    return result


def slot_id(room_id: str, day: str, pair: int) -> str:
    """Формує детермінований ідентифікатор слота (той самий формат, що й у seed)."""
    return f"slot-{room_id}-{day}-p{pair}"


def _ts(day: str, hhmm: str) -> str:
    return datetime.fromisoformat(f"{day}T{hhmm}:00").replace(tzinfo=APP_TZ).isoformat()


def seed_database(db_path: Path | None = None) -> None:
    """Наповнює базу даних демонстраційним набором аудиторій, користувачів, слотів та бронювань."""
    run_migrations(db_path)
    conn = get_connection(db_path)

    # Фіксована сіль для детермінованої швидкодії seed-скрипту
    demo_salt = bytes.fromhex("a1b2c3d4e5f60718293a4b5c6d7e8f90")
    applicant_hash = hash_password("ApplicantPass2026!", salt=demo_salt)
    dispatcher_hash = hash_password("DispatcherPass2026!", salt=demo_salt)

    users = [
        (
            "usr-applicant-1",
            "kovalenko",
            "Коваленко Олена Ігорівна (Заявник)",
            "APPLICANT",
            applicant_hash,
            "Кафедра інформаційних систем",
        ),
        (
            "usr-applicant-2",
            "bondarenko",
            "Бондаренко Андрій Сергійович (Інший заявник)",
            "APPLICANT",
            applicant_hash,
            "Кафедра програмної інженерії",
        ),
        (
            "usr-dispatcher-1",
            "dispatcher",
            "Мельник Вікторія Петрівна (Диспетчер)",
            "DISPATCHER",
            dispatcher_hash,
            "Навчально-методичний відділ",
        ),
    ]

    rooms = [
        (
            "room-101",
            "Ауд. 401-Ф (Лекційна зала)",
            "Корпус ІКС (Ф)",
            120,
            json.dumps(["Проєктор", "Мікрофон", "Інтерактивна дошка", "Кондиціонер"], ensure_ascii=False),
            1,
        ),
        (
            "room-102",
            "Ауд. 405-Ф (Комп'ютерний клас №1)",
            "Корпус ІКС (Ф)",
            30,
            json.dumps(["ПК (30 шт.)", "Проєктор", "Локальна мережа"], ensure_ascii=False),
            1,
        ),
        (
            "room-103",
            "Ауд. 408-Ф (Комп'ютерний клас №2)",
            "Корпус ІКС (Ф)",
            32,
            json.dumps(["ПК (32 шт.)", "Проєктор", "Інтерактивна дошка", "Локальна мережа"], ensure_ascii=False),
            1,
        ),
        (
            "room-104",
            "Ауд. 212-Г (Семінарська аудиторія)",
            "Головний корпус (Г)",
            45,
            json.dumps(["Проєктор", "Маркерна дошка"], ensure_ascii=False),
            1,
        ),
        (
            "room-105",
            "Ауд. 310-Г (Лабораторія мережевих технологій)",
            "Головний корпус (Г)",
            24,
            json.dumps(["ПК (24 шт.)", "Мережеві стенди", "Проєктор"], ensure_ascii=False),
            1,
        ),
        (
            "room-106",
            "Ауд. 115-А (Мала потокова аудиторія)",
            "Адміністративний корпус (А)",
            80,
            json.dumps(["Проєктор", "Мікрофон", "Система відеоконференцій"], ensure_ascii=False),
            1,
        ),
        (
            "room-107",
            "Ауд. 502-Ф (На ремонті — недоступна)",
            "Корпус ІКС (Ф)",
            40,
            json.dumps(["Проєктор", "Маркерна дошка"], ensure_ascii=False),
            0,  # Неактивна аудиторія для перевірки негативного сценарію
        ),
    ]

    d = demo_dates()
    # Дати слотів: одна минула дата (для перевірки відмови на минулий час) та повний 7-денний тиждень (Пн–Нд)
    dates = [d["past"], d["mon"], d["tue"], d["wed"], d["thu"], d["fri"], d["sat"], d["sun"]]
    ev_day_1 = (date.fromisoformat(d["mon"]) - timedelta(days=5)).isoformat()
    ev_day_2 = (date.fromisoformat(d["mon"]) - timedelta(days=4)).isoformat()
    ev_day_3 = (date.fromisoformat(d["mon"]) - timedelta(days=3)).isoformat()

    try:
        with transaction(conn):
            for u in users:
                conn.execute(
                    """
                    INSERT INTO users (id, username, display_name, role, password_hash, department)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        display_name = excluded.display_name,
                        role = excluded.role,
                        department = excluded.department
                    """,
                    u,
                )

            for r in rooms:
                conn.execute(
                    """
                    INSERT INTO rooms (id, name, building, capacity, equipment_json, active)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        name = excluded.name,
                        building = excluded.building,
                        capacity = excluded.capacity,
                        equipment_json = excluded.equipment_json,
                        active = excluded.active
                    """,
                    r,
                )

            for slot_date in dates:
                for r in rooms:
                    room_id = r[0]
                    for pair_num, (start_t, end_t) in ACADEMIC_PAIRS.items():
                        s_id = slot_id(room_id, slot_date, pair_num)
                        conn.execute(
                            """
                            INSERT INTO slots (id, room_id, slot_date, pair_number, starts_at, ends_at)
                            VALUES (?, ?, ?, ?, ?, ?)
                            ON CONFLICT(id) DO NOTHING
                            """,
                            (s_id, room_id, slot_date, pair_num, _ts(slot_date, start_t), _ts(slot_date, end_t)),
                        )

            # Початкові демонстраційні заявки (власні, чужі, підтверджені, очікуючі, скасовані)
            sample_bookings = [
                (
                    "bk-demo-confirmed-01",
                    slot_id("room-101", d["mon"], 1),
                    "usr-applicant-1",
                    "CONFIRMED",
                    "Потокова лекція з дисципліни «Веб-технології та веб-дизайн» для груп АІ-241..243",
                    95,
                    "Погоджено згідно з графіком навчального процесу",
                    "usr-dispatcher-1",
                    _ts(ev_day_2, "09:15"),
                    _ts(ev_day_2, "10:00"),
                    2,
                ),
                (
                    "bk-demo-pending-02",
                    slot_id("room-102", d["mon"], 2),
                    "usr-applicant-1",
                    "PENDING",
                    "Лабораторний практикум із проєктування REST API та реляційних БД",
                    28,
                    None,
                    None,
                    _ts(ev_day_3, "11:20"),
                    _ts(ev_day_3, "11:20"),
                    1,
                ),
                (
                    "bk-demo-other-user-03",
                    slot_id("room-103", d["mon"], 2),
                    "usr-applicant-2",
                    "CONFIRMED",
                    "Модульний контроль з алгоритмів та структур даних (інший викладач)",
                    30,
                    "Аудиторію закріплено за кафедрою ПІ",
                    "usr-dispatcher-1",
                    _ts(ev_day_2, "14:10"),
                    _ts(ev_day_2, "15:00"),
                    2,
                ),
                (
                    "bk-demo-cancelled-04",
                    slot_id("room-104", d["mon"], 3),
                    "usr-applicant-1",
                    "CANCELLED",
                    "Консультація перед захистом курсових робіт (перенесено на інший день)",
                    25,
                    "Скасовано заявником через зміну розкладу",
                    None,
                    _ts(ev_day_1, "12:00"),
                    _ts(ev_day_1, "16:30"),
                    2,
                ),
            ]

            for b in sample_bookings:
                conn.execute(
                    """
                    INSERT INTO bookings (
                        id, slot_id, user_id, status, purpose, expected_attendees,
                        decision_comment, decided_by, created_at, updated_at, version
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO NOTHING
                    """,
                    b,
                )

            sample_events = [
                (
                    "evt-demo-01a",
                    "bk-demo-confirmed-01",
                    "usr-applicant-1",
                    None,
                    "PENDING",
                    "Створено заявку на бронювання",
                    _ts(ev_day_2, "09:15"),
                ),
                (
                    "evt-demo-01b",
                    "bk-demo-confirmed-01",
                    "usr-dispatcher-1",
                    "PENDING",
                    "CONFIRMED",
                    "Погоджено згідно з графіком навчального процесу",
                    _ts(ev_day_2, "10:00"),
                ),
                (
                    "evt-demo-02a",
                    "bk-demo-pending-02",
                    "usr-applicant-1",
                    None,
                    "PENDING",
                    "Створено заявку на бронювання",
                    _ts(ev_day_3, "11:20"),
                ),
                (
                    "evt-demo-03a",
                    "bk-demo-other-user-03",
                    "usr-applicant-2",
                    None,
                    "PENDING",
                    "Створено заявку на бронювання",
                    _ts(ev_day_2, "14:10"),
                ),
                (
                    "evt-demo-03b",
                    "bk-demo-other-user-03",
                    "usr-dispatcher-1",
                    "PENDING",
                    "CONFIRMED",
                    "Аудиторію закріплено за кафедрою ПІ",
                    _ts(ev_day_2, "15:00"),
                ),
                (
                    "evt-demo-04a",
                    "bk-demo-cancelled-04",
                    "usr-applicant-1",
                    None,
                    "PENDING",
                    "Створено заявку на бронювання",
                    _ts(ev_day_1, "12:00"),
                ),
                (
                    "evt-demo-04b",
                    "bk-demo-cancelled-04",
                    "usr-applicant-1",
                    "PENDING",
                    "CANCELLED",
                    "Скасовано заявником через зміну розкладу",
                    _ts(ev_day_1, "16:30"),
                ),
            ]
            for ev in sample_events:
                conn.execute(
                    """
                    INSERT INTO booking_events (
                        id, booking_id, actor_id, old_status, new_status, comment, occurred_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO NOTHING
                    """,
                    ev,
                )

            conn.execute(
                """
                INSERT INTO room_events (id, room_id, actor_id, active, reason, occurred_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO NOTHING
                """,
                (
                    "rev-demo-107",
                    "room-107",
                    "usr-dispatcher-1",
                    0,
                    "Плановий капітальний ремонт мультимедійної мережі до кінця місяця",
                    _ts(ev_day_1, "08:30"),
                ),
            )
    finally:
        conn.close()


if __name__ == "__main__":
    seed_database()
    print("Демонстраційні дані успішно завантажено до бази даних.")
