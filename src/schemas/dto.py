"""Структури передавання даних (DTO) відповідно до рис. Д.11 та RFC 9457."""
from pydantic import BaseModel, Field


class FieldError(BaseModel):
    """Опис помилки окремого поля введення."""
    field: str
    message: str


class BookingError(BaseModel):
    """Передбачуваний формат помилки API за зразком рис. Д.11 та RFC 9457 Problem Details."""
    type: str = "about:blank"
    title: str
    status: int
    code: str
    message: str
    fieldErrors: list[FieldError] = Field(default_factory=list)
    details: list[dict] = Field(default_factory=list)


class LoginRequest(BaseModel):
    """Запит на автентифікацію користувача."""
    username: str = Field(..., min_length=2, max_length=64)
    password: str = Field(..., min_length=4, max_length=128)


class UserProfileResponse(BaseModel):
    """Публічні дані профілю без хешу пароля та з CSRF-токеном сесії."""
    id: str
    username: str
    displayName: str
    role: str
    department: str
    csrfToken: str = ""


class RoomResponse(BaseModel):
    """DTO навчальної аудиторії для публічного каталогу."""
    id: str
    name: str
    building: str
    capacity: int
    equipment: list[str]
    active: bool
    unavailableReason: str | None = None


class RoomAvailabilityRequest(BaseModel):
    """DTO зміни експлуатаційної доступності аудиторії диспетчером."""
    active: bool
    reason: str = Field(..., min_length=5, max_length=300)


class AuditEventResponse(BaseModel):
    """DTO запису журналу аудиту рішень диспетчера та подій життєвого циклу."""
    id: str
    entityType: str
    entityId: str
    targetLabel: str
    actorName: str
    oldState: str | None = None
    newState: str
    comment: str
    occurredAt: str


class SlotViewResponse(BaseModel):
    """DTO часового слота разом зі станом зайнятості для календаря."""
    id: str
    roomId: str
    roomName: str
    building: str
    roomCapacity: int
    roomEquipment: list[str]
    roomActive: bool
    slotDate: str
    pairNumber: int
    startsAt: str
    endsAt: str
    isOccupied: bool
    activeBookingStatus: str | None = None
    activeBookingId: str | None = None


class CreateBookingRequest(BaseModel):
    """
    DTO створення заявки на бронювання (рис. Д.11).
    Зверніть увагу: userId не приймається із тіла запиту, а визначається на сервері із сесії!
    """
    slotId: str = Field(..., min_length=1, max_length=64)
    purpose: str = Field(..., min_length=5, max_length=500)
    expectedAttendees: int = Field(default=15, ge=1, le=1000)


class DispatcherDecisionRequest(BaseModel):
    """DTO рішення диспетчера щодо заявки (погодження або відхилення)."""
    action: str = Field(..., pattern="^(CONFIRM|REJECT)$")
    comment: str = Field(default="", max_length=500)


class CancelBookingRequest(BaseModel):
    """DTO скасування заявки."""
    reason: str = Field(default="Скасовано за ініціативою користувача", max_length=500)


class BookingEventResponse(BaseModel):
    """DTO події в історії життєвого циклу заявки."""
    id: str
    oldStatus: str | None
    newStatus: str
    actorName: str
    comment: str
    occurredAt: str


class BookingResponse(BaseModel):
    """DTO відповіді з даними бронювання (рис. Д.11 з розширеними відомостями для інтерфейсу)."""
    id: str
    slotId: str
    roomId: str
    roomName: str
    building: str
    slotDate: str
    pairNumber: int
    startsAt: str
    endsAt: str
    userId: str
    applicantName: str
    status: str
    purpose: str
    expectedAttendees: int
    decisionComment: str | None = None
    createdAt: str
    updatedAt: str
    version: int = 1
    history: list[BookingEventResponse] = Field(default_factory=list)


class AlternativeSlotResponse(BaseModel):
    """DTO результату роботи алгоритму пошуку найближчої вільної альтернативи."""
    slotId: str
    roomId: str
    roomName: str
    building: str
    capacity: int
    equipment: list[str]
    slotDate: str
    pairNumber: int
    startsAt: str
    endsAt: str
    similarityScore: float
    explanation: str
