"""Контракт та SQL-реалізація репозиторію бронювань (відповідно до рис. Д.12 методичних вказівок)."""
from abc import ABC, abstractmethod
from datetime import datetime, timezone
import sqlite3
import uuid

from src.config import now_app_tz
from src.database import transaction
from src.domain.models import Booking, BookingStatus, DomainRuleError, Slot


class BookingRepository(ABC):
    """Інтерфейс доступу до сховища бронювань («interface» BookingRepository на рис. Д.12)."""

    @abstractmethod
    def create_active(
        self,
        booking: Booking,
        actor_id: str,
        comment: str,
        reference_now: datetime | None = None,
    ) -> Booking:
        """Атомарно створює активне бронювання та запис історії всередині транзакції."""

    @abstractmethod
    def find_by_id(self, booking_id: str) -> Booking | None:
        """Повертає доменний об'єкт бронювання за ідентифікатором."""

    @abstractmethod
    def update_status(
        self,
        booking: Booking,
        old_status: BookingStatus,
        actor_id: str,
        comment: str,
    ) -> Booking:
        """Атомарно оновлює статус бронювання з перевіркою версії та записом в історію."""


class SqlBookingRepository(BookingRepository):
    """Реляційна реалізація репозиторію бронювань у СКБД SQLite (SqlBookingRepository на рис. Д.12)."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def create_active(
        self,
        booking: Booking,
        actor_id: str,
        comment: str,
        reference_now: datetime | None = None,
    ) -> Booking:
        """
        Виконує атомарне створення бронювання та запису в booking_events (рис. Д.3, Д.8).
        Усередині транзакції BEGIN IMMEDIATE повторно перевіряє існування слота, активність
        аудиторії, місткість, допустимість часу (Europe/Kyiv) та відсутність активної заявки.
        У разі одночасного запиту на той самий слот унікальний частковий індекс idx_bookings_active_slot
        генерує sqlite3.IntegrityError, транзакція відкочується і повертається керований конфлікт 409.
        """
        event_id = f"evt-{uuid.uuid4().hex[:12]}"
        check_now = reference_now or now_app_tz()
        try:
            with transaction(self.conn):
                slot_row = self.conn.execute(
                    """
                    SELECT s.id, s.room_id, s.slot_date, s.pair_number, s.starts_at, s.ends_at,
                           r.active AS room_active, r.capacity AS room_capacity, r.name AS room_name
                    FROM slots s
                    JOIN rooms r ON r.id = s.room_id
                    WHERE s.id = ?
                    """,
                    (booking.slot_id,),
                ).fetchone()
                if not slot_row:
                    raise DomainRuleError(
                        code="SLOT_NOT_FOUND",
                        message="Указаний часовий слот не знайдено в розкладі.",
                        status_code=404,
                    )
                if not bool(slot_row["room_active"]):
                    raise DomainRuleError(
                        code="ROOM_UNAVAILABLE",
                        message="Обрана аудиторія наразі виведена з експлуатації (неактивна) і недоступна для бронювання.",
                        status_code=409,
                    )
                if booking.expected_attendees > int(slot_row["room_capacity"]):
                    raise DomainRuleError(
                        code="CAPACITY_EXCEEDED",
                        message=(
                            f"Очікувана кількість учасників ({booking.expected_attendees}) перевищує місткість "
                            f"аудиторії «{slot_row['room_name']}» ({slot_row['room_capacity']} місць)."
                        ),
                        status_code=400,
                    )
                tx_slot = Slot(
                    id=slot_row["id"],
                    room_id=slot_row["room_id"],
                    slot_date=slot_row["slot_date"],
                    pair_number=int(slot_row["pair_number"]),
                    starts_at=slot_row["starts_at"],
                    ends_at=slot_row["ends_at"],
                )
                if not tx_slot.is_time_allowed(check_now):
                    raise DomainRuleError(
                        code="PAST_SLOT_NOT_ALLOWED",
                        message="Неможливо створити бронювання на минулу дату або часовий слот, що вже розпочався.",
                        status_code=400,
                    )

                existing = self.conn.execute(
                    """
                    SELECT id, status FROM bookings
                    WHERE slot_id = ? AND status IN ('PENDING', 'CONFIRMED')
                    """,
                    (booking.slot_id,),
                ).fetchone()
                if existing:
                    raise DomainRuleError(
                        code="SLOT_ALREADY_BOOKED",
                        message="Обраний часовий слот уже зайнятий іншою активною заявкою.",
                        status_code=409,
                    )

                self.conn.execute(
                    """
                    INSERT INTO bookings (
                        id, slot_id, user_id, status, purpose, expected_attendees,
                        decision_comment, decided_by, created_at, updated_at, version
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        booking.id,
                        booking.slot_id,
                        booking.user_id,
                        booking.status.value,
                        booking.purpose,
                        booking.expected_attendees,
                        booking.decision_comment,
                        booking.decided_by,
                        booking.created_at,
                        booking.updated_at,
                        booking.version,
                    ),
                )

                self.conn.execute(
                    """
                    INSERT INTO booking_events (
                        id, booking_id, actor_id, old_status, new_status, comment, occurred_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        booking.id,
                        actor_id,
                        None,
                        booking.status.value,
                        comment,
                        booking.created_at,
                    ),
                )
            return booking
        except sqlite3.IntegrityError as exc:
            raise DomainRuleError(
                code="SLOT_ALREADY_BOOKED",
                message="Конфлікт одночасного бронювання: обраний слот щойно був зайнятий.",
                status_code=409,
            ) from exc

    def find_by_id(self, booking_id: str) -> Booking | None:
        """Повертає доменний об'єкт Booking за його ID."""
        row = self.conn.execute(
            """
            SELECT id, slot_id, user_id, status, purpose, expected_attendees,
                   decision_comment, decided_by, created_at, updated_at, version
            FROM bookings
            WHERE id = ?
            """,
            (booking_id,),
        ).fetchone()
        if not row:
            return None
        return Booking(
            id=row["id"],
            slot_id=row["slot_id"],
            user_id=row["user_id"],
            status=BookingStatus(row["status"]),
            purpose=row["purpose"],
            expected_attendees=int(row["expected_attendees"]),
            decision_comment=row["decision_comment"],
            decided_by=row["decided_by"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            version=int(row["version"]),
        )

    def update_status(
        self,
        booking: Booking,
        old_status: BookingStatus,
        actor_id: str,
        comment: str,
    ) -> Booking:
        """Атомарно зберігає новий стан заявки та додає подію в booking_events."""
        now_iso = datetime.now(timezone.utc).isoformat()
        booking.updated_at = now_iso
        event_id = f"evt-{uuid.uuid4().hex[:12]}"
        expected_prev_version = booking.version - 1

        with transaction(self.conn):
            cursor = self.conn.execute(
                """
                UPDATE bookings
                SET status = ?, decision_comment = ?, decided_by = ?, updated_at = ?, version = ?
                WHERE id = ? AND version = ?
                """,
                (
                    booking.status.value,
                    booking.decision_comment,
                    booking.decided_by,
                    booking.updated_at,
                    booking.version,
                    booking.id,
                    expected_prev_version,
                ),
            )
            if cursor.rowcount == 0:
                raise DomainRuleError(
                    code="CONCURRENT_MODIFICATION_CONFLICT",
                    message="Запис було змінено іншим користувачем. Оновіть дані та повторіть дію.",
                    status_code=409,
                )

            self.conn.execute(
                """
                INSERT INTO booking_events (
                    id, booking_id, actor_id, old_status, new_status, comment, occurred_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    booking.id,
                    actor_id,
                    old_status.value,
                    booking.status.value,
                    comment,
                    now_iso,
                ),
            )
        return booking

    def get_booking_details_dict(self, booking_id: str) -> dict | None:
        """Повертає розширене подання заявки разом з історією переходів станів."""
        row = self.conn.execute(
            """
            SELECT
                b.id, b.slot_id, b.user_id, b.status, b.purpose, b.expected_attendees,
                b.decision_comment, b.created_at, b.updated_at, b.version,
                s.room_id, s.slot_date, s.pair_number, s.starts_at, s.ends_at,
                r.name AS room_name, r.building,
                u.display_name AS applicant_name
            FROM bookings b
            JOIN slots s ON s.id = b.slot_id
            JOIN rooms r ON r.id = s.room_id
            JOIN users u ON u.id = b.user_id
            WHERE b.id = ?
            """,
            (booking_id,),
        ).fetchone()
        if not row:
            return None

        events = self.conn.execute(
            """
            SELECT e.id, e.old_status, e.new_status, e.comment, e.occurred_at, u.display_name AS actor_name
            FROM booking_events e
            JOIN users u ON u.id = e.actor_id
            WHERE e.booking_id = ?
            ORDER BY e.occurred_at ASC, e.id ASC
            """,
            (booking_id,),
        ).fetchall()

        return {
            "id": row["id"],
            "slotId": row["slot_id"],
            "roomId": row["room_id"],
            "roomName": row["room_name"],
            "building": row["building"],
            "slotDate": row["slot_date"],
            "pairNumber": int(row["pair_number"]),
            "startsAt": row["starts_at"],
            "endsAt": row["ends_at"],
            "userId": row["user_id"],
            "applicantName": row["applicant_name"],
            "status": row["status"],
            "purpose": row["purpose"],
            "expectedAttendees": int(row["expected_attendees"]),
            "decisionComment": row["decision_comment"],
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
            "version": int(row["version"]),
            "history": [
                {
                    "id": ev["id"],
                    "oldStatus": ev["old_status"],
                    "newStatus": ev["new_status"],
                    "actorName": ev["actor_name"],
                    "comment": ev["comment"],
                    "occurredAt": ev["occurred_at"],
                }
                for ev in events
            ],
        }

    def list_bookings(
        self,
        user_id: str | None = None,
        status: str | None = None,
        limit: int = 200,
    ) -> list[dict]:
        """Повертає список заявок за 2 пакетні SQL-запити (без проблеми N+1)."""
        query = """
            SELECT
                b.id, b.slot_id, b.user_id, b.status, b.purpose, b.expected_attendees,
                b.decision_comment, b.created_at, b.updated_at, b.version,
                s.room_id, s.slot_date, s.pair_number, s.starts_at, s.ends_at,
                r.name AS room_name, r.building,
                u.display_name AS applicant_name
            FROM bookings b
            JOIN slots s ON s.id = b.slot_id
            JOIN rooms r ON r.id = s.room_id
            JOIN users u ON u.id = b.user_id
            WHERE 1=1
        """
        params: list[object] = []
        if user_id:
            query += " AND b.user_id = ?"
            params.append(user_id)
        if status:
            query += " AND b.status = ?"
            params.append(status)
        query += " ORDER BY b.created_at DESC, b.id DESC LIMIT ?"
        params.append(limit)

        rows = self.conn.execute(query, params).fetchall()
        if not rows:
            return []

        booking_ids = [r["id"] for r in rows]
        placeholders = ",".join("?" for _ in booking_ids)
        ev_rows = self.conn.execute(
            f"""
            SELECT e.id, e.booking_id, e.old_status, e.new_status, e.comment, e.occurred_at,
                   u.display_name AS actor_name
            FROM booking_events e
            JOIN users u ON u.id = e.actor_id
            WHERE e.booking_id IN ({placeholders})
            ORDER BY e.occurred_at ASC, e.id ASC
            """,
            booking_ids,
        ).fetchall()

        events_by_booking: dict[str, list[dict]] = {bid: [] for bid in booking_ids}
        for ev in ev_rows:
            events_by_booking[ev["booking_id"]].append(
                {
                    "id": ev["id"],
                    "oldStatus": ev["old_status"],
                    "newStatus": ev["new_status"],
                    "actorName": ev["actor_name"],
                    "comment": ev["comment"],
                    "occurredAt": ev["occurred_at"],
                }
            )

        return [
            {
                "id": row["id"],
                "slotId": row["slot_id"],
                "roomId": row["room_id"],
                "roomName": row["room_name"],
                "building": row["building"],
                "slotDate": row["slot_date"],
                "pairNumber": int(row["pair_number"]),
                "startsAt": row["starts_at"],
                "endsAt": row["ends_at"],
                "userId": row["user_id"],
                "applicantName": row["applicant_name"],
                "status": row["status"],
                "purpose": row["purpose"],
                "expectedAttendees": int(row["expected_attendees"]),
                "decisionComment": row["decision_comment"],
                "createdAt": row["created_at"],
                "updatedAt": row["updated_at"],
                "version": int(row["version"]),
                "history": events_by_booking.get(row["id"], []),
            }
            for row in rows
        ]

    def list_audit_events(self, limit: int = 100, entity_type: str | None = None) -> list[dict]:
        """Повертає зведений хронологічний журнал аудиту рішень диспетчера та змін доступності."""
        events: list[dict] = []

        if entity_type in (None, "", "BOOKING"):
            bk_rows = self.conn.execute(
                """
                SELECT
                    e.id,
                    e.booking_id AS entity_id,
                    r.name || ' • ' || s.slot_date || ' (' || s.pair_number || ' пара)' AS target_label,
                    u.display_name AS actor_name,
                    e.old_status AS old_state,
                    e.new_status AS new_state,
                    e.comment,
                    e.occurred_at
                FROM booking_events e
                JOIN bookings b ON b.id = e.booking_id
                JOIN slots s ON s.id = b.slot_id
                JOIN rooms r ON r.id = s.room_id
                JOIN users u ON u.id = e.actor_id
                ORDER BY e.occurred_at DESC, e.id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            for r in bk_rows:
                events.append(
                    {
                        "id": r["id"],
                        "entityType": "BOOKING",
                        "entityId": r["entity_id"],
                        "targetLabel": r["target_label"],
                        "actorName": r["actor_name"],
                        "oldState": r["old_state"],
                        "newState": r["new_state"],
                        "comment": r["comment"],
                        "occurredAt": r["occurred_at"],
                    }
                )

        if entity_type in (None, "", "ROOM"):
            rm_rows = self.conn.execute(
                """
                SELECT
                    re.id,
                    re.room_id AS entity_id,
                    r.name || ' (' || r.building || ')' AS target_label,
                    u.display_name AS actor_name,
                    re.active,
                    re.reason AS comment,
                    re.occurred_at
                FROM room_events re
                JOIN rooms r ON r.id = re.room_id
                JOIN users u ON u.id = re.actor_id
                ORDER BY re.occurred_at DESC, re.id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            for r in rm_rows:
                is_active = bool(r["active"])
                events.append(
                    {
                        "id": r["id"],
                        "entityType": "ROOM",
                        "entityId": r["entity_id"],
                        "targetLabel": r["target_label"],
                        "actorName": r["actor_name"],
                        "oldState": "ЗАКРИТА" if is_active else "АКТИВНА",
                        "newState": "АКТИВНА" if is_active else "ЗАКРИТА",
                        "comment": r["comment"],
                        "occurredAt": r["occurred_at"],
                    }
                )

        events.sort(key=lambda item: (item["occurredAt"], item["id"]), reverse=True)
        return events[:limit]
