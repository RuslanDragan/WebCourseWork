-- Міграція 001: Початкова схема бази даних вебсистеми бронювання навчальних аудиторій
-- Підтримка зовнішніх ключів вмикається на рівні підключення (PRAGMA foreign_keys = ON)

CREATE TABLE IF NOT EXISTS rooms (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    building TEXT NOT NULL,
    capacity INTEGER NOT NULL CHECK (capacity > 0 AND capacity <= 1000),
    equipment_json TEXT NOT NULL DEFAULT '[]',
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
);

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('APPLICANT', 'DISPATCHER')),
    password_hash TEXT NOT NULL,
    department TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS slots (
    id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL,
    slot_date TEXT NOT NULL,
    pair_number INTEGER NOT NULL CHECK (pair_number BETWEEN 1 AND 8),
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    FOREIGN KEY (room_id) REFERENCES rooms(id) ON DELETE CASCADE,
    UNIQUE (room_id, slot_date, pair_number)
);

CREATE TABLE IF NOT EXISTS bookings (
    id TEXT PRIMARY KEY,
    slot_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('PENDING', 'CONFIRMED', 'CANCELLED', 'REJECTED')),
    purpose TEXT NOT NULL CHECK (length(trim(purpose)) >= 5 AND length(purpose) <= 500),
    expected_attendees INTEGER NOT NULL DEFAULT 1 CHECK (expected_attendees > 0),
    decision_comment TEXT,
    decided_by TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    FOREIGN KEY (slot_id) REFERENCES slots(id) ON DELETE RESTRICT,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE RESTRICT,
    FOREIGN KEY (decided_by) REFERENCES users(id) ON DELETE SET NULL
);

-- Ключовий інваріант предметної області (Додаток Д, рис. Д.3, Д.9):
-- Для одного часового слота дозволено не більше одного активного бронювання (PENDING або CONFIRMED).
-- Скасовані (CANCELLED) та відхилені (REJECTED) записи зберігаються в історії і не блокують слот.
CREATE UNIQUE INDEX IF NOT EXISTS idx_bookings_active_slot
ON bookings (slot_id)
WHERE status IN ('PENDING', 'CONFIRMED');

CREATE INDEX IF NOT EXISTS idx_slots_date_room ON slots (slot_date, room_id);
CREATE INDEX IF NOT EXISTS idx_bookings_user_created ON bookings (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_bookings_status ON bookings (status);

CREATE TABLE IF NOT EXISTS booking_events (
    id TEXT PRIMARY KEY,
    booking_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    old_status TEXT,
    new_status TEXT NOT NULL CHECK (new_status IN ('PENDING', 'CONFIRMED', 'CANCELLED', 'REJECTED')),
    comment TEXT NOT NULL DEFAULT '',
    occurred_at TEXT NOT NULL,
    FOREIGN KEY (booking_id) REFERENCES bookings(id) ON DELETE CASCADE,
    FOREIGN KEY (actor_id) REFERENCES users(id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_booking_events_booking ON booking_events (booking_id, occurred_at ASC);

CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
