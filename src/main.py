"""Головний модуль вебзастосунку бронювання навчальних аудиторій."""
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from src.config import ALLOWED_ORIGINS
from src.controllers.api_router import api_router
from src.database import run_migrations
from src.domain.models import DomainRuleError
from src.schemas.dto import BookingError, FieldError

SRC_DIR = Path(__file__).resolve().parent
STATIC_DIR = SRC_DIR / "static"
TEMPLATES_DIR = SRC_DIR / "templates"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Автоматичне виконання міграцій при старті сервера."""
    run_migrations()
    yield


app = FastAPI(
    title="Вебсистема бронювання навчальних аудиторій",
    description=(
        "Навчальна вебсистема пошуку вільних часових слотів, створення заявок, "
        "погодження диспетчером і підбору найближчих альтернатив (Варіант 17)."
    ),
    version="1.0.0",
    lifespan=lifespan,
)


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    """
    Перевірка Origin для змінювальних запитів і додавання HTTP-заголовків безпеки.
    """
    if request.method in ("POST", "PATCH", "PUT", "DELETE"):
        origin = request.headers.get("origin")
        if origin is not None:
            parsed_origin = urlparse(origin)
            request_host = request.headers.get("host", "")
            origin_host = parsed_origin.netloc
            origin_ok = bool(origin_host) and (origin_host == request_host) or (origin in ALLOWED_ORIGINS)
            if not origin_ok:
                err = BookingError(
                    type="https://op.edu.ua/problems/invalid_origin",
                    title="Запит відхилено політикою безпеки",
                    status=403,
                    code="INVALID_ORIGIN",
                    message="Заборонене джерело запиту (Origin mismatch).",
                    fieldErrors=[],
                )
                return JSONResponse(status_code=403, content=err.model_dump())

    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    if not request.url.path.startswith(("/docs", "/redoc")):
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "object-src 'none'; frame-ancestors 'none'; base-uri 'self'",
        )
    return response


@app.exception_handler(DomainRuleError)
async def domain_rule_exception_handler(_request: Request, exc: DomainRuleError) -> JSONResponse:
    """Перетворює порушення бізнес-правил на структуровану відповідь за RFC 9457 / рис. Д.11."""
    titles = {
        400: "Помилка валідації запиту",
        401: "Необхідна автентифікація",
        403: "Доступ заборонено",
        404: "Об'єкт не знайдено",
        409: "Конфлікт стану або зайнятий слот",
        429: "Перевищено ліміт запитів",
    }
    err = BookingError(
        type=f"https://op.edu.ua/problems/{exc.code.lower()}",
        title=titles.get(exc.status_code, "Помилка виконання операції"),
        status=exc.status_code,
        code=exc.code,
        message=exc.message,
        fieldErrors=[],
        details=exc.details,
    )
    return JSONResponse(status_code=exc.status_code, content=err.model_dump())


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
    """Перетворює помилки полів вхідних DTO на передбачуваний формат BookingError (400 Bad Request)."""
    field_errors: list[FieldError] = []
    for err_item in exc.errors():
        loc = [str(part) for part in err_item.get("loc", []) if part != "body"]
        field_name = ".".join(loc) if loc else "request"
        field_errors.append(FieldError(field=field_name, message=err_item.get("msg", "Некоректне значення")))

    err = BookingError(
        type="https://op.edu.ua/problems/validation-error",
        title="Некоректні вхідні дані",
        status=400,
        code="VALIDATION_ERROR",
        message="Перевірте правильність заповнення полів форми.",
        fieldErrors=field_errors,
    )
    return JSONResponse(status_code=400, content=err.model_dump())


app.include_router(api_router)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", include_in_schema=False)
def serve_index() -> FileResponse:
    """Повертає головну сторінку вебінтерфейсу."""
    return FileResponse(str(TEMPLATES_DIR / "index.html"))
