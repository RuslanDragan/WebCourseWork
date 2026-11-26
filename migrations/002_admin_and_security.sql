-- Міграція 002: адміністративний процес диспетчера та захист входу

-- Журнал змін експлуатаційної доступності аудиторій (закриття/відкриття з причиною).
-- Поточна причина недоступності = остання подія для аудиторії з active = 0.
CREATE TABLE IF NOT EXISTS room_events (
    id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    active INTEGER NOT NULL CHECK (active IN (0, 1)),
    reason TEXT NOT NULL CHECK (length(trim(reason)) >= 5 AND length(reason) <= 300),
    occurred_at TEXT NOT NULL,
    FOREIGN KEY (room_id) REFERENCES rooms(id) ON DELETE CASCADE,
    FOREIGN KEY (actor_id) REFERENCES users(id) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS idx_room_events_room ON room_events (room_id, occurred_at DESC);

-- Невдалі спроби входу для обмеження підбору пароля (rate limiting за логіном та IP).
CREATE TABLE IF NOT EXISTS login_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL,
    client_ip TEXT NOT NULL DEFAULT '',
    attempted_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_login_attempts_user_time ON login_attempts (username, client_ip, attempted_at);

-- Пришвидшення журналу аудиту та вибірок за періодом
CREATE INDEX IF NOT EXISTS idx_booking_events_time ON booking_events (occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_slots_room_date ON slots (room_id, slot_date, pair_number);
