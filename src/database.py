"""Інфраструктурний модуль керування підключенням до СКБД SQLite та міграціями."""
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from src.config import DATABASE_PATH, MIGRATIONS_DIR


def get_connection(db_path: Path | None = None) -> sqlite3.Connection:
    """Створює підключення до SQLite з увімкненою перевіркою зовнішніх ключів та режимом WAL."""
    target_path = db_path or DATABASE_PATH
    target_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target_path), timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA busy_timeout = 5000;")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """
    Контекстний менеджер атомарної транзакції (BEGIN IMMEDIATE).
    Забезпечує захист від конкурентного стану гонитви (race condition) при одночасному бронюванні слота.
    """
    conn.execute("BEGIN IMMEDIATE;")
    try:
        yield conn
        conn.execute("COMMIT;")
    except Exception:
        conn.execute("ROLLBACK;")
        raise


def run_migrations(db_path: Path | None = None) -> list[str]:
    """
    Виконує нові SQL-міграції з каталогу migrations/ рівно один раз.
    Застосовані версії фіксуються в таблиці schema_migrations, тому повторний запуск
    не виконує вже застосовані скрипти (важливо для міграцій з ALTER TABLE).
    Повертає список щойно застосованих файлів.
    """
    conn = get_connection(db_path)
    applied_now: list[str] = []
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " version TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
        )
        done = {row["version"] for row in conn.execute("SELECT version FROM schema_migrations")}
        for migration_file in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if migration_file.name in done:
                continue
            sql_script = migration_file.read_text(encoding="utf-8")
            # Скрипт міграції та фіксація версії виконуються узгоджено.
            conn.executescript("BEGIN;\n" + sql_script + "\nCOMMIT;")
            with transaction(conn):
                conn.execute(
                    "INSERT INTO schema_migrations (version) VALUES (?)",
                    (migration_file.name,),
                )
            conn.execute("PRAGMA foreign_keys = ON;")
            applied_now.append(migration_file.name)
        cols = {
            r["name"]
            for r in conn.execute("PRAGMA table_info(login_attempts)").fetchall()
        }
        if cols and "client_ip" not in cols:
            conn.execute("ALTER TABLE login_attempts ADD COLUMN client_ip TEXT NOT NULL DEFAULT ''")
        return applied_now
    finally:
        conn.close()
