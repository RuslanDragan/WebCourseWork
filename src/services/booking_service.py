"""Прикладний сервіс керування бронюваннями та підбору альтернатив (рис. Д.12)."""
from datetime import datetime, timezone
import uuid

from src.config import now_app_tz
from src.domain.models import Booking, BookingStatus, DomainRuleError, Room, Slot, User, UserRole
from src.repositories.booking_repository import SqlBookingRepository
from src.repositories.room_repository import SqlRoomRepository


class BookingService:
    """Координація сценаріїв бронювання, перевірки предметних правил і пошуку альтернатив."""

    def __init__(self, booking_repo: SqlBookingRepository, room_repo: SqlRoomRepository):
        self.booking_repo = booking_repo
        self.room_repo = room_repo

    def create(
        self,
        user: User,
        slot_id: str,
        purpose: str,
        expected_attendees: int = 15,
        reference_now: datetime | None = None,
    ) -> dict:
        """
        Створює нову заявку на бронювання фіксованого слота (алгоритм Прикладу Д.1, рис. Д.2–Д.3).
        """
        clean_purpose = (purpose or "").strip()
        if len(clean_purpose) < 5:
            raise DomainRuleError(
                code="INVALID_PURPOSE",
                message="Мета використання аудиторії має містити щонайменше 5 символів.",
                status_code=400,
            )

        slot: Slot | None = self.room_repo.find_slot_by_id(slot_id)
        if not slot:
            raise DomainRuleError(
                code="SLOT_NOT_FOUND",
                message="Указаний часовий слот не знайдено в розкладі.",
                status_code=404,
            )

        room: Room | None = self.room_repo.find_room_by_id(slot.room_id)
        if not room or not room.active:
            raise DomainRuleError(
                code="ROOM_UNAVAILABLE",
                message="Обрана аудиторія наразі виведена з експлуатації (неактивна) і недоступна для бронювання.",
                status_code=409,
            )

        if expected_attendees > room.capacity:
            raise DomainRuleError(
                code="CAPACITY_EXCEEDED",
                message=(
                    f"Очікувана кількість учасників ({expected_attendees}) перевищує місткість "
                    f"аудиторії «{room.name}» ({room.capacity} місць)."
                ),
                status_code=400,
            )

        now_dt = reference_now or now_app_tz()
        if not slot.is_time_allowed(now_dt):
            raise DomainRuleError(
                code="PAST_SLOT_NOT_ALLOWED",
                message="Неможливо створити бронювання на минулу дату або часовий слот, що вже розпочався.",
                status_code=400,
            )

        now_iso = datetime.now(timezone.utc).isoformat()
        booking_id = f"bk-{uuid.uuid4().hex[:10]}"
        booking = Booking(
            id=booking_id,
            slot_id=slot.id,
            user_id=user.id,
            status=BookingStatus.PENDING,
            purpose=clean_purpose,
            expected_attendees=expected_attendees,
            created_at=now_iso,
            updated_at=now_iso,
            version=1,
        )

        self.booking_repo.create_active(
            booking=booking,
            actor_id=user.id,
            comment=f"Створено заявку на бронювання ({clean_purpose})",
            reference_now=now_dt,
        )
        details = self.booking_repo.get_booking_details_dict(booking.id)
        assert details is not None
        return details

    def get_booking_for_user(self, user: User, booking_id: str) -> dict:
        """
        Перегляд бронювання з обов'язковою перевіркою об'єктного доступу (Таблиця Д.2, TC-ACCESS-02).
        Заявник може читати лише власні заявки; диспетчер бачить усі.
        """
        details = self.booking_repo.get_booking_details_dict(booking_id)
        if not details:
            raise DomainRuleError(
                code="BOOKING_NOT_FOUND",
                message="Заявку на бронювання не знайдено.",
                status_code=404,
            )
        if user.role != UserRole.DISPATCHER and details["userId"] != user.id:
            raise DomainRuleError(
                code="FORBIDDEN_PRIVATE_BOOKING",
                message="Доступ заборонено: перегляд чужої заявки не дозволений політикою безпеки.",
                status_code=403,
            )
        return details

    def list_bookings_for_user(self, user: User, status: str | None = None, all_requests: bool = False) -> list[dict]:
        """Повертає список заявок: власні для заявника або всі для диспетчера."""
        if all_requests:
            if user.role != UserRole.DISPATCHER:
                raise DomainRuleError(
                    code="FORBIDDEN_ROLE",
                    message="Перегляд усіх заявок системи дозволений лише диспетчеру.",
                    status_code=403,
                )
            return self.booking_repo.list_bookings(user_id=None, status=status)
        return self.booking_repo.list_bookings(user_id=user.id, status=status)

    def _ensure_slot_mutable(self, slot_id: str, reference_now: datetime | None = None) -> None:
        """Забороняє змінювати статус заявки, часовий слот якої вже розпочався або минув."""
        slot = self.room_repo.find_slot_by_id(slot_id)
        check_now = reference_now or now_app_tz()
        if slot and not slot.is_time_allowed(check_now):
            raise DomainRuleError(
                code="PAST_BOOKING_IMMUTABLE",
                message="Неможливо змінити статус заявки, часовий слот якої вже розпочався або минув.",
                status_code=409,
            )

    def cancel_booking(
        self,
        user: User,
        booking_id: str,
        reason: str = "Скасовано користувачем",
        reference_now: datetime | None = None,
    ) -> dict:
        """Скасовує активну майбутню заявку з перевіркою власника та допустимості переходу."""
        booking = self.booking_repo.find_by_id(booking_id)
        if not booking:
            raise DomainRuleError(
                code="BOOKING_NOT_FOUND",
                message="Заявку на бронювання не знайдено.",
                status_code=404,
            )
        self._ensure_slot_mutable(booking.slot_id, reference_now=reference_now)
        old_status = booking.status
        booking.cancel(actor=user, reason=reason)
        self.booking_repo.update_status(
            booking=booking,
            old_status=old_status,
            actor_id=user.id,
            comment=booking.decision_comment or "Скасовано користувачем",
        )
        details = self.booking_repo.get_booking_details_dict(booking.id)
        assert details is not None
        return details

    def decide_booking(
        self,
        dispatcher: User,
        booking_id: str,
        action: str,
        comment: str,
        reference_now: datetime | None = None,
    ) -> dict:
        """Погоджує (CONFIRM) або відхиляє (REJECT) активну майбутню заявку диспетчером."""
        if dispatcher.role != UserRole.DISPATCHER:
            raise DomainRuleError(
                code="FORBIDDEN_ROLE",
                message="Погодження або відхилення заявок дозволено виключно диспетчеру.",
                status_code=403,
            )
        booking = self.booking_repo.find_by_id(booking_id)
        if not booking:
            raise DomainRuleError(
                code="BOOKING_NOT_FOUND",
                message="Заявку на бронювання не знайдено.",
                status_code=404,
            )
        self._ensure_slot_mutable(booking.slot_id, reference_now=reference_now)
        old_status = booking.status
        if action == "CONFIRM":
            booking.confirm(dispatcher_id=dispatcher.id, comment=comment)
        elif action == "REJECT":
            booking.reject(dispatcher_id=dispatcher.id, reason=comment)
        else:
            raise DomainRuleError(
                code="INVALID_DECISION_ACTION",
                message="Невідома дія диспетчера. Допустимі значення: CONFIRM або REJECT.",
                status_code=400,
            )

        self.booking_repo.update_status(
            booking=booking,
            old_status=old_status,
            actor_id=dispatcher.id,
            comment=booking.decision_comment or "",
        )
        details = self.booking_repo.get_booking_details_dict(booking.id)
        assert details is not None
        return details

    def find_nearest_alternatives(
        self,
        slot_id: str,
        min_capacity: int | None = None,
        required_equipment: list[str] | None = None,
        limit: int = 5,
        reference_now: datetime | None = None,
    ) -> list[dict]:
        """
        Погоджене ускладнення Варіанта 17: Алгоритм пошуку найближчої вільної альтернативи.
        Для заданого (зайнятого або недоступного) слота знаходить вільні майбутні слоти в активних аудиторіях
        за ту саму дату, які задовольняють вимоги місткості й оснащення, та ранжує їх за оцінкою близькості S:
            S = clamp(100 - 25 * |p_cand - p_target| - 10 * I(building_cand != building_target)
                      - 0.15 * (capacity_cand - capacity_req), 10, 100)
        """
        target_slot = self.room_repo.find_slot_by_id(slot_id)
        if not target_slot:
            raise DomainRuleError(
                code="SLOT_NOT_FOUND",
                message="Цільовий слот для підбору альтернативи не знайдено.",
                status_code=404,
            )
        target_room = self.room_repo.find_room_by_id(target_slot.room_id)
        if not target_room:
            raise DomainRuleError(
                code="ROOM_NOT_FOUND",
                message="Цільову аудиторію не знайдено.",
                status_code=404,
            )

        effective_capacity = min_capacity if min_capacity is not None else target_room.capacity
        source_eq = target_room.equipment if required_equipment is None else required_equipment
        req_eq = [e.strip().lower() for e in source_eq if e.strip()]
        check_now = reference_now or now_app_tz()

        candidates = self.room_repo.list_slots_with_occupancy(
            slot_date=target_slot.slot_date,
            min_capacity=effective_capacity,
            only_available=True,
        )

        scored: list[dict] = []
        for cand in candidates:
            if cand["id"] == target_slot.id:
                continue
            cand_slot = Slot(
                id=cand["id"],
                room_id=cand["roomId"],
                slot_date=cand["slotDate"],
                pair_number=int(cand["pairNumber"]),
                starts_at=cand["startsAt"],
                ends_at=cand["endsAt"],
            )
            if not cand_slot.is_time_allowed(check_now):
                continue
            cand_eq_lower = [item.lower() for item in cand["roomEquipment"]]
            if req_eq and not all(any(r in eq_item for eq_item in cand_eq_lower) for r in req_eq):
                continue

            pair_diff = abs(int(cand["pairNumber"]) - target_slot.pair_number)
            same_building = cand["building"] == target_room.building
            capacity_surplus = max(0, int(cand["roomCapacity"]) - effective_capacity)

            score = (
                100.0
                - 25.0 * pair_diff
                - (0.0 if same_building else 10.0)
                - 0.15 * capacity_surplus
            )
            score = round(max(10.0, min(100.0, score)), 1)

            reasons: list[str] = []
            if pair_diff == 0:
                reasons.append(f"та сама {cand['pairNumber']}-та пара")
            else:
                reasons.append(f"сусідня {cand['pairNumber']}-та пара (відхилення {pair_diff} пар)")
            if same_building:
                reasons.append(f"той самий корпус ({cand['building']})")
            else:
                reasons.append(f"інший корпус ({cand['building']})")
            reasons.append(f"місткість {cand['roomCapacity']} місць")

            scored.append(
                {
                    "slotId": cand["id"],
                    "roomId": cand["roomId"],
                    "roomName": cand["roomName"],
                    "building": cand["building"],
                    "capacity": cand["roomCapacity"],
                    "equipment": cand["roomEquipment"],
                    "slotDate": cand["slotDate"],
                    "pairNumber": cand["pairNumber"],
                    "startsAt": cand["startsAt"],
                    "endsAt": cand["endsAt"],
                    "similarityScore": score,
                    "explanation": "Вільна альтернатива: " + ", ".join(reasons),
                }
            )

        scored.sort(key=lambda item: (-item["similarityScore"], item["pairNumber"], item["roomName"]))
        return scored[:limit]

    def set_room_availability(
        self,
        dispatcher: User,
        room_id: str,
        active: bool,
        reason: str,
        reference_now: datetime | None = None,
    ) -> Room:
        """Закриття або відкриття аудиторії на ремонт/спецзахід (тільки диспетчер)."""
        if dispatcher.role != UserRole.DISPATCHER:
            raise DomainRuleError(
                code="FORBIDDEN_ROLE",
                message="Керувати доступністю аудиторій може лише диспетчер.",
                status_code=403,
            )
        return self.room_repo.set_room_availability(
            room_id=room_id,
            active=active,
            reason=reason,
            actor_id=dispatcher.id,
            reference_now=reference_now,
        )

    def list_audit_events(
        self,
        dispatcher: User,
        limit: int = 100,
        entity_type: str | None = None,
    ) -> list[dict]:
        """Повертає журнал аудиту рішень диспетчера та змін доступності (тільки диспетчер)."""
        if dispatcher.role != UserRole.DISPATCHER:
            raise DomainRuleError(
                code="FORBIDDEN_ROLE",
                message="Перегляд журналу аудиту дозволено лише диспетчеру.",
                status_code=403,
            )
        return self.booking_repo.list_audit_events(limit=limit, entity_type=entity_type)
