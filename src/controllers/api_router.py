"""Маршрути та контролери HTTP API (відповідно до таблиці Д.12 та рис. Д.12 методичних вказівок)."""
from fastapi import APIRouter, Cookie, Header, Query, Request, Response, status

from src.config import SESSION_COOKIE_NAME, SESSION_MAX_AGE_SECONDS
from src.database import get_connection
from src.domain.models import DomainRuleError, User
from src.repositories.booking_repository import SqlBookingRepository
from src.repositories.room_repository import SqlRoomRepository
from src.repositories.user_repository import SqlUserRepository
from src.schemas.dto import (
    AlternativeSlotResponse,
    AuditEventResponse,
    BookingResponse,
    CancelBookingRequest,
    CreateBookingRequest,
    DispatcherDecisionRequest,
    LoginRequest,
    RoomAvailabilityRequest,
    RoomResponse,
    SlotViewResponse,
    UserProfileResponse,
)
from src.services.auth_service import AuthService, generate_csrf_token, verify_csrf_token
from src.services.booking_service import BookingService

api_router = APIRouter(prefix="/api")


def _require_csrf(session_token: str | None, csrf_token: str | None) -> None:
    """Перевіряє CSRF-токен у заголовку X-CSRF-Token для захищених cookie-сесією запитів."""
    if not verify_csrf_token(session_token or "", csrf_token):
        raise DomainRuleError(
            code="INVALID_CSRF_TOKEN",
            message="Відсутній або недійсний CSRF-токен. Оновіть сторінку та повторіть дію.",
            status_code=403,
        )


def _auth_and_csrf(conn, session_token: str | None, csrf_token: str | None) -> User:
    """Автентифікує користувача за cookie-сесією та перевіряє CSRF-токен для мутацій."""
    auth_service = AuthService(SqlUserRepository(conn))
    user = auth_service.require_authenticated(session_token)
    _require_csrf(session_token, csrf_token)
    return user


@api_router.get("/health", tags=["System"])
def health_check() -> dict:
    """Маршрут перевірки працездатності процесу та готовності бази даних (Operational View, табл. Д.14)."""
    conn = get_connection()
    try:
        room_count = conn.execute("SELECT COUNT(*) AS cnt FROM rooms").fetchone()["cnt"]
        return {"status": "UP", "database": "CONNECTED", "roomsCount": int(room_count)}
    finally:
        conn.close()


@api_router.post("/auth/login", response_model=UserProfileResponse, tags=["Auth"])
def login(payload: LoginRequest, request: Request, response: Response) -> dict:
    """Автентифікація користувача зі встановленням HttpOnly cookie та видачею CSRF-токена."""
    conn = get_connection()
    try:
        user_repo = SqlUserRepository(conn)
        auth_service = AuthService(user_repo)
        client_ip = request.client.host if request.client else ""
        user, token = auth_service.authenticate(
            username=payload.username,
            password=payload.password,
            max_age_seconds=SESSION_MAX_AGE_SECONDS,
            client_ip=client_ip,
        )
        response.set_cookie(
            key=SESSION_COOKIE_NAME,
            value=token,
            max_age=SESSION_MAX_AGE_SECONDS,
            httponly=True,
            samesite="lax",
            secure=False,
        )
        return {
            "id": user.id,
            "username": user.username,
            "displayName": user.display_name,
            "role": user.role.value,
            "department": user.department,
            "csrfToken": generate_csrf_token(token),
        }
    finally:
        conn.close()


@api_router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT, tags=["Auth"])
def logout(
    response: Response,
    session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME),
    x_csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
) -> Response:
    """Завершення сеансу користувача та очищення сесійного cookie (з перевіркою CSRF)."""
    conn = get_connection()
    try:
        if session_token:
            user_repo = SqlUserRepository(conn)
            if user_repo.get_user_by_session(session_token):
                _require_csrf(session_token, x_csrf_token)
                user_repo.delete_session(session_token)
        response.delete_cookie(key=SESSION_COOKIE_NAME)
        response.status_code = status.HTTP_204_NO_CONTENT
        return response
    finally:
        conn.close()


@api_router.get("/auth/me", response_model=UserProfileResponse, tags=["Auth"])
def current_user(
    session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME),
) -> dict:
    """Повертає профіль поточного автентифікованого користувача разом із CSRF-токеном."""
    conn = get_connection()
    try:
        auth_service = AuthService(SqlUserRepository(conn))
        user = auth_service.require_authenticated(session_token)
        return {
            "id": user.id,
            "username": user.username,
            "displayName": user.display_name,
            "role": user.role.value,
            "department": user.department,
            "csrfToken": generate_csrf_token(session_token or ""),
        }
    finally:
        conn.close()


@api_router.get("/rooms", response_model=list[RoomResponse], tags=["Catalog"])
def get_rooms(
    minCapacity: int | None = Query(default=None, ge=1, le=1000),
    equipment: str | None = Query(default=None, max_length=64),
    building: str | None = Query(default=None, max_length=64),
    onlyActive: bool = Query(default=False),
) -> list[dict]:
    """Каталог навчальних аудиторій із погодженими фільтрами (таблиця Д.12: GET /api/rooms)."""
    conn = get_connection()
    try:
        rooms = SqlRoomRepository(conn).list_rooms(
            min_capacity=minCapacity,
            equipment_tag=equipment,
            building=building,
            only_active=onlyActive,
        )
        return [
            {
                "id": r.id,
                "name": r.name,
                "building": r.building,
                "capacity": r.capacity,
                "equipment": r.equipment,
                "active": r.active,
                "unavailableReason": r.unavailable_reason,
            }
            for r in rooms
        ]
    finally:
        conn.close()


@api_router.patch("/rooms/{room_id}/availability", response_model=RoomResponse, tags=["Dispatcher"])
def update_room_availability(
    room_id: str,
    payload: RoomAvailabilityRequest,
    session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME),
    x_csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
) -> dict:
    """
    Керування експлуатаційною доступністю аудиторії диспетчером.
    Забороняє закриття, якщо існують майбутні активні бронювання (409 Conflict зі списком конфліктів).
    """
    conn = get_connection()
    try:
        user = _auth_and_csrf(conn, session_token, x_csrf_token)
        booking_service = BookingService(SqlBookingRepository(conn), SqlRoomRepository(conn))
        room = booking_service.set_room_availability(
            dispatcher=user,
            room_id=room_id,
            active=payload.active,
            reason=payload.reason,
        )
        return {
            "id": room.id,
            "name": room.name,
            "building": room.building,
            "capacity": room.capacity,
            "equipment": room.equipment,
            "active": room.active,
            "unavailableReason": room.unavailable_reason,
        }
    finally:
        conn.close()


@api_router.get("/slots", response_model=list[SlotViewResponse], tags=["Catalog"])
def get_slots(
    date: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    dateFrom: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    dateTo: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    roomId: str | None = Query(default=None, max_length=64),
    minCapacity: int | None = Query(default=None, ge=1, le=1000),
    equipment: str | None = Query(default=None, max_length=64),
    onlyAvailable: bool = Query(default=False),
) -> list[dict]:
    """Доступні часові слоти за дату або діапазон дат (денний розклад і тижневий календар 7×6)."""
    conn = get_connection()
    try:
        return SqlRoomRepository(conn).list_slots_with_occupancy(
            slot_date=date,
            date_from=dateFrom,
            date_to=dateTo,
            room_id=roomId,
            min_capacity=minCapacity,
            equipment_tag=equipment,
            only_available=onlyAvailable,
        )
    finally:
        conn.close()


@api_router.get("/slots/{slot_id}/alternatives", response_model=list[AlternativeSlotResponse], tags=["Catalog"])
def get_slot_alternatives(
    slot_id: str,
    minCapacity: int | None = Query(default=None, ge=1, le=1000),
    equipment: str | None = Query(default=None, max_length=128),
) -> list[dict]:
    """Погоджене ускладнення: пошук найближчих вільних альтернатив для зайнятого слота."""
    conn = get_connection()
    try:
        service = BookingService(SqlBookingRepository(conn), SqlRoomRepository(conn))
        req_eq = [item.strip() for item in equipment.split(",") if item.strip()] if equipment else None
        return service.find_nearest_alternatives(
            slot_id=slot_id,
            min_capacity=minCapacity,
            required_equipment=req_eq,
        )
    finally:
        conn.close()


@api_router.post("/bookings", response_model=BookingResponse, status_code=status.HTTP_201_CREATED, tags=["Bookings"])
def create_booking(
    payload: CreateBookingRequest,
    session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME),
    x_csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
) -> dict:
    """
    Створення заявки на бронювання (таблиця Д.12: POST /api/bookings, рис. Д.12 BookingController.create).
    Ідентифікатор заявника береться виключно з перевіреної серверної сесії.
    """
    conn = get_connection()
    try:
        user = _auth_and_csrf(conn, session_token, x_csrf_token)
        booking_service = BookingService(SqlBookingRepository(conn), SqlRoomRepository(conn))
        return booking_service.create(
            user=user,
            slot_id=payload.slotId,
            purpose=payload.purpose,
            expected_attendees=payload.expectedAttendees,
        )
    finally:
        conn.close()


@api_router.get("/bookings", response_model=list[BookingResponse], tags=["Bookings"])
def list_bookings(
    statusFilter: str | None = Query(default=None, alias="status"),
    allRequests: bool = Query(default=False),
    session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME),
) -> list[dict]:
    """Перегляд списку власних бронювань (або всіх заявок для диспетчера)."""
    conn = get_connection()
    try:
        auth_service = AuthService(SqlUserRepository(conn))
        user = auth_service.require_authenticated(session_token)
        if statusFilter and statusFilter not in ("PENDING", "CONFIRMED", "CANCELLED", "REJECTED"):
            raise DomainRuleError(
                code="INVALID_STATUS_FILTER",
                message="Недопустиме значення фільтра статусу.",
                status_code=400,
            )
        booking_service = BookingService(SqlBookingRepository(conn), SqlRoomRepository(conn))
        return booking_service.list_bookings_for_user(
            user=user,
            status=statusFilter,
            all_requests=allRequests,
        )
    finally:
        conn.close()


@api_router.get("/bookings/{booking_id}", response_model=BookingResponse, tags=["Bookings"])
def get_booking(
    booking_id: str,
    session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME),
) -> dict:
    """Перегляд дозволеного бронювання з перевіркою власника (таблиця Д.12: GET /api/bookings/{id})."""
    conn = get_connection()
    try:
        auth_service = AuthService(SqlUserRepository(conn))
        user = auth_service.require_authenticated(session_token)
        booking_service = BookingService(SqlBookingRepository(conn), SqlRoomRepository(conn))
        return booking_service.get_booking_for_user(user=user, booking_id=booking_id)
    finally:
        conn.close()


@api_router.post("/bookings/{booking_id}/cancel", response_model=BookingResponse, tags=["Bookings"])
def cancel_booking(
    booking_id: str,
    payload: CancelBookingRequest | None = None,
    session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME),
    x_csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
) -> dict:
    """Скасування бронювання згідно з правилами (таблиця Д.12: POST /api/bookings/{id}/cancel)."""
    conn = get_connection()
    try:
        user = _auth_and_csrf(conn, session_token, x_csrf_token)
        booking_service = BookingService(SqlBookingRepository(conn), SqlRoomRepository(conn))
        reason = payload.reason if payload else "Скасовано за ініціативою користувача"
        return booking_service.cancel_booking(user=user, booking_id=booking_id, reason=reason)
    finally:
        conn.close()


@api_router.post("/bookings/{booking_id}/decision", response_model=BookingResponse, tags=["Bookings"])
def decide_booking(
    booking_id: str,
    payload: DispatcherDecisionRequest,
    session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME),
    x_csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
) -> dict:
    """Погодження або відхилення заявки диспетчером із фіксацією історії."""
    conn = get_connection()
    try:
        user = _auth_and_csrf(conn, session_token, x_csrf_token)
        booking_service = BookingService(SqlBookingRepository(conn), SqlRoomRepository(conn))
        return booking_service.decide_booking(
            dispatcher=user,
            booking_id=booking_id,
            action=payload.action,
            comment=payload.comment,
        )
    finally:
        conn.close()


@api_router.get("/audit", response_model=list[AuditEventResponse], tags=["Dispatcher"])
def get_audit_log(
    entityType: str | None = Query(default=None, pattern="^(BOOKING|ROOM)$"),
    limit: int = Query(default=100, ge=1, le=500),
    session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME),
) -> list[dict]:
    """Журнал аудиту рішень диспетчера та змін доступності аудиторій."""
    conn = get_connection()
    try:
        auth_service = AuthService(SqlUserRepository(conn))
        user = auth_service.require_authenticated(session_token)
        booking_service = BookingService(SqlBookingRepository(conn), SqlRoomRepository(conn))
        return booking_service.list_audit_events(dispatcher=user, limit=limit, entity_type=entityType)
    finally:
        conn.close()
