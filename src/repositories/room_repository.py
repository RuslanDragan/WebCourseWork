"""Репозиторій навчальних аудиторій та часових слотів."""
from datetime import datetime, timezone
import json
import sqlite3
import uuid

from src.config import APP_TZ, now_app_tz
from src.database import transaction
from src.domain.models import DomainRuleError, Room, Slot


class SqlRoomRepository:
    """Доступ до даних аудиторій та розкладу слотів."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def _get_latest_close_reasons(self) -> dict[str, str]:
        """Повертає останню зафіксовану причину закриття для неактивних аудиторій."""
        rows = self.conn.execute(
            """
            SELECT re.room_id, re.reason
            FROM room_events re
            WHERE re.rowid IN (
                SELECT MAX(rowid)
                FROM room_events
                GROUP BY room_id
            ) AND re.active = 0
            """
        ).fetchall()
        return {row["room_id"]: row["reason"] for row in rows}

    def list_rooms(
        self,
        min_capacity: int | None = None,
        equipment_tag: str | None = None,
        building: str | None = None,
        only_active: bool = False,
    ) -> list[Room]:
        """Повертає відфільтрований каталог аудиторій."""
        query = "SELECT id, name, building, capacity, equipment_json, active FROM rooms WHERE 1=1"
        params: list[object] = []

        if only_active:
            query += " AND active = 1"
        if min_capacity is not None:
            query += " AND capacity >= ?"
            params.append(min_capacity)
        if building:
            query += " AND building = ?"
            params.append(building.strip())

        query += " ORDER BY name ASC, id ASC"
        rows = self.conn.execute(query, params).fetchall()
        reasons_map = self._get_latest_close_reasons()

        rooms: list[Room] = []
        eq_filter = equipment_tag.strip().lower() if equipment_tag else None
        for r in rows:
            equipment_list: list[str] = json.loads(r["equipment_json"] or "[]")
            if eq_filter:
                if not any(eq_filter in item.lower() for item in equipment_list):
                    continue
            is_act = bool(r["active"])
            rooms.append(
                Room(
                    id=r["id"],
                    name=r["name"],
                    building=r["building"],
                    capacity=int(r["capacity"]),
                    equipment=equipment_list,
                    active=is_act,
                    unavailable_reason=None if is_act else reasons_map.get(r["id"]),
                )
            )
        return rooms

    def find_room_by_id(self, room_id: str) -> Room | None:
        """Повертає аудиторію за її ідентифікатором."""
        row = self.conn.execute(
            "SELECT id, name, building, capacity, equipment_json, active FROM rooms WHERE id = ?",
            (room_id,),
        ).fetchone()
        if not row:
            return None
        is_act = bool(row["active"])
        reason = None
        if not is_act:
            ev_row = self.conn.execute(
                "SELECT reason FROM room_events WHERE room_id = ? AND active = 0 ORDER BY occurred_at DESC LIMIT 1",
                (room_id,),
            ).fetchone()
            reason = ev_row["reason"] if ev_row else None
        return Room(
            id=row["id"],
            name=row["name"],
            building=row["building"],
            capacity=int(row["capacity"]),
            equipment=json.loads(row["equipment_json"] or "[]"),
            active=is_act,
            unavailable_reason=reason,
        )

    def set_room_availability(
        self,
        room_id: str,
        active: bool,
        reason: str,
        actor_id: str,
        reference_now: datetime | None = None,
    ) -> Room:
        """
        Змінює експлуатаційну доступність аудиторії.
        Якщо active=False, але для аудиторії існують майбутні активні заявки (PENDING/CONFIRMED),
        повертає 409 Conflict зі списком конфліктних бронювань — диспетчер спершу має їх відхилити або скасувати.
        """
        clean_reason = (reason or "").strip()
        if len(clean_reason) < 5:
            raise DomainRuleError(
                code="REASON_REQUIRED",
                message="Причина зміни доступності аудиторії є обов'язковою (щонайменше 5 символів).",
                status_code=400,
            )

        if reference_now is None:
            now_local = now_app_tz()
        elif reference_now.tzinfo is None:
            now_local = reference_now.replace(tzinfo=APP_TZ)
        else:
            now_local = reference_now.astimezone(APP_TZ)
        now_utc_iso = datetime.now(timezone.utc).isoformat()
        event_id = f"rev-{uuid.uuid4().hex[:12]}"

        with transaction(self.conn):
            room_row = self.conn.execute(
                "SELECT id, name, building, capacity, equipment_json, active FROM rooms WHERE id = ?",
                (room_id,),
            ).fetchone()
            if not room_row:
                raise DomainRuleError(
                    code="ROOM_NOT_FOUND",
                    message="Навчальну аудиторію не знайдено.",
                    status_code=404,
                )

            current_active = bool(room_row["active"])
            if current_active == active:
                unchanged = self.find_room_by_id(room_id)
                assert unchanged is not None
                return unchanged

            if not active:
                candidate_rows = self.conn.execute(
                    """
                    SELECT b.id, b.status, b.purpose, s.id AS slot_id, s.room_id, s.slot_date,
                           s.pair_number, s.starts_at, s.ends_at, u.display_name AS applicant_name
                    FROM bookings b
                    JOIN slots s ON s.id = b.slot_id
                    JOIN users u ON u.id = b.user_id
                    WHERE s.room_id = ?
                      AND b.status IN ('PENDING', 'CONFIRMED')
                    ORDER BY s.slot_date ASC, s.pair_number ASC
                    """,
                    (room_id,),
                ).fetchall()
                active_rows = [
                    r
                    for r in candidate_rows
                    if Slot(
                        id=r["slot_id"],
                        room_id=r["room_id"],
                        slot_date=r["slot_date"],
                        pair_number=int(r["pair_number"]),
                        starts_at=r["starts_at"],
                        ends_at=r["ends_at"],
                    ).is_time_allowed(now_local)
                ]
                if active_rows:
                    conflicts = [
                        {
                            "bookingId": r["id"],
                            "status": r["status"],
                            "slotDate": r["slot_date"],
                            "pairNumber": int(r["pair_number"]),
                            "applicantName": r["applicant_name"],
                            "purpose": r["purpose"],
                        }
                        for r in active_rows
                    ]
                    raise DomainRuleError(
                        code="ROOM_HAS_ACTIVE_BOOKINGS",
                        message=(
                            f"Неможливо закрити аудиторію «{room_row['name']}»: наявні майбутні "
                            f"активні бронювання ({len(conflicts)} шт.). Спершу відхиліть або скасуйте їх."
                        ),
                        status_code=409,
                        details=conflicts,
                    )

            self.conn.execute(
                "UPDATE rooms SET active = ? WHERE id = ?",
                (1 if active else 0, room_id),
            )
            self.conn.execute(
                """
                INSERT INTO room_events (id, room_id, actor_id, active, reason, occurred_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (event_id, room_id, actor_id, 1 if active else 0, clean_reason, now_utc_iso),
            )

        updated = self.find_room_by_id(room_id)
        assert updated is not None
        return updated

    def find_slot_by_id(self, slot_id: str) -> Slot | None:
        """Повертає часовий слот за його ідентифікатором."""
        row = self.conn.execute(
            "SELECT id, room_id, slot_date, pair_number, starts_at, ends_at FROM slots WHERE id = ?",
            (slot_id,),
        ).fetchone()
        if not row:
            return None
        return Slot(
            id=row["id"],
            room_id=row["room_id"],
            slot_date=row["slot_date"],
            pair_number=int(row["pair_number"]),
            starts_at=row["starts_at"],
            ends_at=row["ends_at"],
        )

    def list_slots_with_occupancy(
        self,
        slot_date: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        room_id: str | None = None,
        min_capacity: int | None = None,
        equipment_tag: str | None = None,
        only_available: bool = False,
    ) -> list[dict]:
        """Повертає перелік слотів із поточним станом зайнятості для денного або тижневого календаря."""
        query = """
            SELECT
                s.id AS slot_id,
                s.room_id,
                r.name AS room_name,
                r.building,
                r.capacity AS room_capacity,
                r.equipment_json,
                r.active AS room_active,
                s.slot_date,
                s.pair_number,
                s.starts_at,
                s.ends_at,
                b.id AS active_booking_id,
                b.status AS active_booking_status
            FROM slots s
            JOIN rooms r ON r.id = s.room_id
            LEFT JOIN bookings b
                ON b.slot_id = s.id AND b.status IN ('PENDING', 'CONFIRMED')
            WHERE 1=1
        """
        params: list[object] = []
        if slot_date:
            query += " AND s.slot_date = ?"
            params.append(slot_date)
        if date_from:
            query += " AND s.slot_date >= ?"
            params.append(date_from)
        if date_to:
            query += " AND s.slot_date <= ?"
            params.append(date_to)
        if room_id:
            query += " AND s.room_id = ?"
            params.append(room_id)
        if min_capacity is not None:
            query += " AND r.capacity >= ?"
            params.append(min_capacity)
        if only_available:
            query += " AND r.active = 1 AND b.id IS NULL"

        query += " ORDER BY s.slot_date ASC, s.pair_number ASC, r.name ASC"
        rows = self.conn.execute(query, params).fetchall()

        eq_filter = equipment_tag.strip().lower() if equipment_tag else None
        results: list[dict] = []
        for row in rows:
            equipment_list: list[str] = json.loads(row["equipment_json"] or "[]")
            if eq_filter and not any(eq_filter in item.lower() for item in equipment_list):
                continue
            results.append(
                {
                    "id": row["slot_id"],
                    "roomId": row["room_id"],
                    "roomName": row["room_name"],
                    "building": row["building"],
                    "roomCapacity": int(row["room_capacity"]),
                    "roomEquipment": equipment_list,
                    "roomActive": bool(row["room_active"]),
                    "slotDate": row["slot_date"],
                    "pairNumber": int(row["pair_number"]),
                    "startsAt": row["starts_at"],
                    "endsAt": row["ends_at"],
                    "isOccupied": row["active_booking_id"] is not None,
                    "activeBookingStatus": row["active_booking_status"],
                    "activeBookingId": row["active_booking_id"],
                }
            )
        return results
