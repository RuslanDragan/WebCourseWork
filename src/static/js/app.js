/**
 * Клієнтська логіка вебсистеми бронювання навчальних аудиторій (Варіант 17).
 * Забезпечує денний розклад і тижневий календар (7x6), бронювання та бронювання альтернатив у 1 клік,
 * панель диспетчера (черга заявок, керування доступністю аудиторій, журнал аудиту) та CSRF-захист.
 */

const state = {
    currentUser: null,
    csrfToken: "",
    selectedSlot: null,
    pendingLoginSlot: null,
    slotsCache: [],
    alternativesCache: [],
    roomsCache: [],
    catalogMode: "day", // "day" | "week"
    dispTab: "requests", // "requests" | "rooms" | "audit"
};

const STATUS_LABELS = {
    PENDING: "Очікує погодження (PENDING)",
    CONFIRMED: "Підтверджено (CONFIRMED)",
    REJECTED: "Відхилено (REJECTED)",
    CANCELLED: "Скасовано (CANCELLED)",
};

const WEEKDAY_NAMES_UK = ["Нд", "Пн", "Вт", "Ср", "Чт", "Пт", "Сб"];

function getNextMondayIso() {
    const kyivDateStr = new Intl.DateTimeFormat("en-CA", {
        timeZone: "Europe/Kyiv",
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
    }).format(new Date());
    const [kyivY, kyivM, kyivD] = kyivDateStr.split("-").map(Number);
    const nowKyiv = new Date(kyivY, kyivM - 1, kyivD);
    const day = nowKyiv.getDay(); // 0=Нд .. 6=Сб
    const diff = day === 1 ? 7 : (8 - day) % 7 || 7;
    const nextMon = new Date(kyivY, kyivM - 1, kyivD + diff);
    const y = nextMon.getFullYear();
    const m = String(nextMon.getMonth() + 1).padStart(2, "0");
    const d = String(nextMon.getDate()).padStart(2, "0");
    return `${y}-${m}-${d}`;
}

function addDaysIso(isoDate, offsetDays) {
    const [y, m, d] = isoDate.split("-").map(Number);
    const dt = new Date(y, m - 1, d + offsetDays);
    const yy = dt.getFullYear();
    const mm = String(dt.getMonth() + 1).padStart(2, "0");
    const dd = String(dt.getDate()).padStart(2, "0");
    return `${yy}-${mm}-${dd}`;
}

function formatShortDayHeader(isoDate) {
    const [y, m, d] = isoDate.split("-").map(Number);
    const dt = new Date(y, m - 1, d);
    const wd = WEEKDAY_NAMES_UK[dt.getDay()];
    return `${wd} (${String(d).padStart(2, "0")}.${String(m).padStart(2, "0")})`;
}

function escapeHtml(value) {
    const str = String(value ?? "");
    return str
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");
}

function showGlobalAlert(message, type = "success") {
    const alertEl = document.getElementById("global-alert");
    alertEl.className = `alert alert-${type}`;
    alertEl.textContent = message;
    alertEl.classList.remove("hidden");
}

function hideGlobalAlert() {
    const alertEl = document.getElementById("global-alert");
    alertEl.classList.add("hidden");
}

async function apiRequest(url, options = {}) {
    const method = (options.method || "GET").toUpperCase();
    const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
    if (["POST", "PATCH", "PUT", "DELETE"].includes(method) && state.csrfToken) {
        headers["X-CSRF-Token"] = state.csrfToken;
    }
    const response = await fetch(url, { ...options, method, headers });
    if (response.status === 204) {
        return null;
    }
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
        const error = new Error(data.message || `Помилка HTTP ${response.status}`);
        error.status = response.status;
        error.code = data.code || "HTTP_ERROR";
        error.fieldErrors = data.fieldErrors || [];
        error.details = data.details || [];
        throw error;
    }
    return data;
}

async function refreshAuthStatus() {
    const statusLabel = document.getElementById("user-status-label");
    const btnOpenLogin = document.getElementById("btn-open-login");
    const btnLogout = document.getElementById("btn-logout");
    const tabDispatcher = document.getElementById("tab-btn-dispatcher");

    try {
        const user = await apiRequest("/api/auth/me");
        state.currentUser = user;
        state.csrfToken = user.csrfToken || "";
        statusLabel.textContent = `${user.displayName} [${user.role}]`;
        btnOpenLogin.classList.add("hidden");
        btnLogout.classList.remove("hidden");
        if (user.role === "DISPATCHER") {
            tabDispatcher.classList.remove("hidden");
        } else {
            tabDispatcher.classList.add("hidden");
        }
    } catch {
        state.currentUser = null;
        state.csrfToken = "";
        statusLabel.textContent = "Гість (не автентифіковано)";
        btnOpenLogin.classList.remove("hidden");
        btnLogout.classList.add("hidden");
        tabDispatcher.classList.add("hidden");
    }
}

async function populateRoomsDropdown() {
    try {
        const rooms = await apiRequest("/api/rooms");
        state.roomsCache = rooms;
        const select = document.getElementById("filter-room");
        if (!select) return;
        const prev = select.value;
        select.innerHTML = rooms
            .map((r) => `<option value="${escapeHtml(r.id)}">${escapeHtml(r.name)} (${r.capacity} місць)</option>`)
            .join("");
        if (prev && rooms.some((r) => r.id === prev)) {
            select.value = prev;
        }
    } catch {
        // Ігноруємо при тимчасовій помилці завантаження довідника
    }
}

function setCatalogMode(mode) {
    state.catalogMode = mode;
    const isWeek = mode === "week";
    const btnDay = document.getElementById("btn-mode-day");
    const btnWeek = document.getElementById("btn-mode-week");

    btnDay.className = `btn btn-sm ${isWeek ? "btn-outline" : "btn-primary"}`;
    btnWeek.className = `btn btn-sm ${isWeek ? "btn-primary" : "btn-outline"}`;
    btnDay.setAttribute("aria-pressed", String(!isWeek));
    btnWeek.setAttribute("aria-pressed", String(isWeek));

    document.getElementById("filter-room-group").classList.toggle("hidden", !isWeek);
    document.getElementById("filter-capacity-group").classList.toggle("hidden", isWeek);
    document.getElementById("filter-equipment-group").classList.toggle("hidden", isWeek);
    document.getElementById("filter-date-label").textContent = isWeek ? "Початок тижня (7 днів)" : "Дата заняття";

    loadCatalogAndSchedule();
}

async function loadCatalogAndSchedule() {
    const loadingEl = document.getElementById("catalog-loading");
    const emptyEl = document.getElementById("catalog-empty");
    const gridEl = document.getElementById("rooms-schedule-grid");
    const weekEl = document.getElementById("weekly-calendar-container");

    loadingEl.classList.remove("hidden");
    emptyEl.classList.add("hidden");
    gridEl.innerHTML = "";
    weekEl.innerHTML = "";
    gridEl.classList.toggle("hidden", state.catalogMode === "week");
    weekEl.classList.toggle("hidden", state.catalogMode !== "week");

    const dateVal = document.getElementById("filter-date").value;
    const minCapVal = document.getElementById("filter-capacity").value;
    const eqVal = document.getElementById("filter-equipment").value;
    const onlyAvailVal = document.getElementById("filter-available").checked;
    const roomVal = document.getElementById("filter-room").value;

    const params = new URLSearchParams();
    if (state.catalogMode === "week") {
        const start = dateVal || getNextMondayIso();
        const end = addDaysIso(start, 6);
        params.set("dateFrom", start);
        params.set("dateTo", end);
        if (roomVal) params.set("roomId", roomVal);
        if (onlyAvailVal) params.set("onlyAvailable", "true");
    } else {
        if (dateVal) params.set("date", dateVal);
        if (minCapVal) params.set("minCapacity", minCapVal);
        if (eqVal) params.set("equipment", eqVal);
        if (onlyAvailVal) params.set("onlyAvailable", "true");
    }

    try {
        const slots = await apiRequest(`/api/slots?${params.toString()}`);
        state.slotsCache = slots;
        loadingEl.classList.add("hidden");

        if (!slots || slots.length === 0) {
            emptyEl.classList.remove("hidden");
            return;
        }

        if (state.catalogMode === "week") {
            renderWeeklyCalendar(slots, dateVal || getNextMondayIso(), weekEl);
            return;
        }

        renderDayGrid(slots, gridEl);
    } catch (err) {
        loadingEl.classList.add("hidden");
        showGlobalAlert(err.message, "error");
    }
}

function renderDayGrid(slots, gridEl) {
    const roomsMap = new Map();
    for (const s of slots) {
        if (!roomsMap.has(s.roomId)) {
            roomsMap.set(s.roomId, {
                roomId: s.roomId,
                roomName: s.roomName,
                building: s.building,
                roomCapacity: s.roomCapacity,
                roomEquipment: s.roomEquipment,
                roomActive: s.roomActive,
                slots: [],
            });
        }
        roomsMap.get(s.roomId).slots.push(s);
    }

    const cardsHtml = [];
    for (const room of roomsMap.values()) {
        const badgeClass = room.roomActive ? "badge-active" : "badge-inactive";
        const badgeText = room.roomActive ? `Активна • ${room.roomCapacity} місць` : "На ремонті (недоступна)";
        const eqHtml = room.roomEquipment.map((eq) => `<span class="eq-tag">${escapeHtml(eq)}</span>`).join("");

        const slotsHtml = room.slots
            .map((slot) => {
                const timeRange = `${slot.startsAt.slice(11, 16)}–${slot.endsAt.slice(11, 16)}`;
                let stateClass = "slot-free";
                let statusText = "Вільно — натисніть для заявки";
                if (!room.roomActive) {
                    stateClass = "slot-disabled";
                    statusText = "Аудиторія неактивна";
                } else if (slot.isOccupied) {
                    stateClass = "slot-occupied";
                    statusText = `Зайнято (${slot.activeBookingStatus})`;
                }
                return `
                    <button type="button" class="slot-btn ${stateClass}" data-slot-id="${escapeHtml(slot.id)}">
                        <span class="slot-pair">${slot.pairNumber} пара (${escapeHtml(timeRange)})</span>
                        <span class="slot-status-text">${escapeHtml(statusText)}</span>
                    </button>
                `;
            })
            .join("");

        cardsHtml.push(`
            <article class="room-card ${room.roomActive ? "" : "room-inactive"}">
                <div class="room-card-header">
                    <div>
                        <h3 class="room-title">${escapeHtml(room.roomName)}</h3>
                        <p class="room-meta">${escapeHtml(room.building)}</p>
                    </div>
                    <span class="badge ${badgeClass}">${escapeHtml(badgeText)}</span>
                </div>
                <div class="equipment-tags">${eqHtml}</div>
                <div class="slots-list">${slotsHtml}</div>
            </article>
        `);
    }
    gridEl.innerHTML = cardsHtml.join("");

    gridEl.querySelectorAll(".slot-btn").forEach((btn) => {
        btn.addEventListener("click", () => openBookingDialog(btn.getAttribute("data-slot-id")));
    });
}

function renderWeeklyCalendar(slots, startDate, weekEl) {
    const days = Array.from({ length: 7 }, (_, i) => addDaysIso(startDate, i));
    const slotByKey = new Map();
    const pairsSet = new Set();
    const pairTimes = new Map();

    for (const s of slots) {
        slotByKey.set(`${s.slotDate}|${s.pairNumber}`, s);
        pairsSet.add(s.pairNumber);
        pairTimes.set(s.pairNumber, `${s.startsAt.slice(11, 16)}–${s.endsAt.slice(11, 16)}`);
    }
    const pairs = Array.from(pairsSet).sort((a, b) => a - b);
    const roomMeta = slots[0];

    const headerCells = days.map((d) => `<th scope="col">${escapeHtml(formatShortDayHeader(d))}</th>`).join("");
    const bodyRows = pairs
        .map((pairNum) => {
            const cells = days
                .map((day) => {
                    const slot = slotByKey.get(`${day}|${pairNum}`);
                    if (!slot) {
                        return `<td class="week-slot-cell">—</td>`;
                    }
                    let cls = "slot-free";
                    let label = "Вільно";
                    if (!slot.roomActive) {
                        cls = "slot-disabled";
                        label = "Закрита";
                    } else if (slot.isOccupied) {
                        cls = "slot-occupied";
                        label = slot.activeBookingStatus;
                    }
                    return `
                        <td class="week-slot-cell">
                            <button type="button" class="slot-btn week-slot-btn ${cls}" data-slot-id="${escapeHtml(slot.id)}">
                                <span class="slot-status-text">${escapeHtml(label)}</span>
                            </button>
                        </td>
                    `;
                })
                .join("");
            return `
                <tr>
                    <th scope="row">${pairNum} пара<br><small>${escapeHtml(pairTimes.get(pairNum) || "")}</small></th>
                    ${cells}
                </tr>
            `;
        })
        .join("");

    weekEl.innerHTML = `
        <div class="room-card">
            <div class="room-card-header">
                <div>
                    <h3 class="room-title">${escapeHtml(roomMeta.roomName)} — календар на 7 днів</h3>
                    <p class="room-meta">${escapeHtml(roomMeta.building)} • ${roomMeta.roomCapacity} місць</p>
                </div>
                <span class="badge ${roomMeta.roomActive ? "badge-active" : "badge-inactive"}">
                    ${roomMeta.roomActive ? "Активна" : "На ремонті"}
                </span>
            </div>
            <div class="table-responsive">
                <table class="week-table">
                    <thead>
                        <tr>
                            <th scope="col">Пара / Час</th>
                            ${headerCells}
                        </tr>
                    </thead>
                    <tbody>${bodyRows}</tbody>
                </table>
            </div>
        </div>
    `;

    weekEl.querySelectorAll(".slot-btn").forEach((btn) => {
        btn.addEventListener("click", () => openBookingDialog(btn.getAttribute("data-slot-id")));
    });
}

function resolveSlotById(slotId) {
    const cached = state.slotsCache.find((s) => s.id === slotId);
    if (cached) return cached;
    const alt = state.alternativesCache.find((a) => a.slotId === slotId);
    if (!alt) return null;
    return {
        id: alt.slotId,
        roomId: alt.roomId,
        roomName: alt.roomName,
        building: alt.building,
        roomCapacity: alt.capacity,
        roomEquipment: alt.equipment || [],
        roomActive: true,
        slotDate: alt.slotDate,
        pairNumber: alt.pairNumber,
        startsAt: alt.startsAt,
        endsAt: alt.endsAt,
        isOccupied: false,
        activeBookingStatus: null,
    };
}

function openBookingDialog(slotOrId) {
    const slot = typeof slotOrId === "object" && slotOrId !== null ? slotOrId : resolveSlotById(slotOrId);
    if (!slot) return;
    state.selectedSlot = slot;

    document.getElementById("booking-slot-id").value = slot.id;
    const summaryEl = document.getElementById("selected-slot-summary");
    const timeRange = `${slot.startsAt.slice(11, 16)}–${slot.endsAt.slice(11, 16)}`;
    summaryEl.innerHTML = `
        <strong>Аудиторія:</strong> ${escapeHtml(slot.roomName)} (${escapeHtml(slot.building)}, місткість ${slot.roomCapacity} місць)<br>
        <strong>Дата і час:</strong> ${escapeHtml(slot.slotDate)}, ${slot.pairNumber}-та пара (${escapeHtml(timeRange)} Europe/Kyiv)<br>
        <strong>Поточний стан слота:</strong> ${
            !slot.roomActive
                ? "Неактивна аудиторія"
                : slot.isOccupied
                ? `Зайнятий (${escapeHtml(slot.activeBookingStatus)})`
                : "Вільний для бронювання"
        }
    `;

    const errBox = document.getElementById("booking-form-error");
    errBox.classList.add("hidden");
    errBox.textContent = "";

    const altContainer = document.getElementById("alternatives-container");
    altContainer.classList.add("hidden");

    const dialog = document.getElementById("booking-dialog");
    if (!dialog.open) {
        dialog.showModal();
    }

    if (slot.isOccupied || !slot.roomActive) {
        loadAlternativesForCurrentSlot();
    }
}

async function submitBookingForSlot(slotId) {
    const errBox = document.getElementById("booking-form-error");
    errBox.classList.add("hidden");

    const purpose = document.getElementById("booking-purpose").value;
    const rawAttendees = document.getElementById("booking-attendees").value.trim();
    const expectedAttendees = Number(rawAttendees);
    if (!rawAttendees || !Number.isInteger(expectedAttendees) || expectedAttendees < 1 || expectedAttendees > 1000) {
        errBox.textContent = "Вкажіть коректну цілу кількість учасників (від 1 до 1000).";
        errBox.classList.remove("hidden");
        return;
    }

    try {
        const created = await apiRequest("/api/bookings", {
            method: "POST",
            body: JSON.stringify({ slotId, purpose, expectedAttendees }),
        });
        state.selectedSlot = null;
        document.getElementById("booking-dialog").close();
        document.getElementById("booking-purpose").value = "";
        showGlobalAlert(`Бронювання #${created.id} успішно створено у стані ${created.status}!`, "success");
        await loadCatalogAndSchedule();
    } catch (err) {
        let msg = err.message;
        if (err.fieldErrors && err.fieldErrors.length > 0) {
            msg += " (" + err.fieldErrors.map((f) => `${f.field}: ${f.message}`).join("; ") + ")";
        }
        errBox.textContent = msg;
        errBox.classList.remove("hidden");

        if (err.status === 401) {
            state.pendingLoginSlot = resolveSlotById(slotId) || state.selectedSlot;
            document.getElementById("booking-dialog").close();
            document.getElementById("login-dialog").showModal();
        } else if (err.status === 409) {
            await loadAlternativesForCurrentSlot();
        }
    }
}

async function loadAlternativesForCurrentSlot() {
    if (!state.selectedSlot) return;
    const altContainer = document.getElementById("alternatives-container");
    const altList = document.getElementById("alternatives-list");
    const parsedAtt = parseInt(document.getElementById("booking-attendees").value, 10);
    const attendeesVal = Number.isInteger(parsedAtt) && parsedAtt >= 1 && parsedAtt <= 1000 ? parsedAtt : 15;

    altContainer.classList.remove("hidden");
    altList.innerHTML = "<p>Пошук вільних альтернатив...</p>";

    try {
        const alternatives = await apiRequest(
            `/api/slots/${encodeURIComponent(state.selectedSlot.id)}/alternatives?minCapacity=${attendeesVal}`
        );
        state.alternativesCache = alternatives || [];
        if (!alternatives || alternatives.length === 0) {
            altList.innerHTML = "<p>Вільних альтернатив на цю дату за заданою місткістю не знайдено.</p>";
            return;
        }
        altList.innerHTML = alternatives
            .map(
                (alt) => `
                <div class="alt-item">
                    <div>
                        <strong>${escapeHtml(alt.roomName)}</strong> • ${alt.pairNumber} пара (${escapeHtml(
                    alt.startsAt.slice(11, 16)
                )}–${escapeHtml(alt.endsAt.slice(11, 16))})<br>
                        <small>${escapeHtml(alt.explanation)} • Відповідність: <strong>${alt.similarityScore}%</strong></small>
                    </div>
                    <div class="alt-actions">
                        <button type="button" class="btn btn-sm btn-primary btn-book-alt-now" data-alt-slot="${escapeHtml(alt.slotId)}">
                            Забронювати зараз
                        </button>
                        <button type="button" class="btn btn-sm btn-outline btn-pick-alt" data-alt-slot="${escapeHtml(alt.slotId)}">
                            Обрати слот
                        </button>
                    </div>
                </div>
            `
            )
            .join("");

        altList.querySelectorAll(".btn-pick-alt").forEach((btn) => {
            btn.addEventListener("click", () => openBookingDialog(btn.getAttribute("data-alt-slot")));
        });

        altList.querySelectorAll(".btn-book-alt-now").forEach((btn) => {
            btn.addEventListener("click", async () => {
                const newSlotId = btn.getAttribute("data-alt-slot");
                const purposeVal = document.getElementById("booking-purpose").value.trim();
                if (purposeVal.length < 5) {
                    openBookingDialog(newSlotId);
                    const errBox = document.getElementById("booking-form-error");
                    errBox.textContent = "Спочатку вкажіть мету використання (мін. 5 символів) і натисніть «Надіслати заявку».";
                    errBox.classList.remove("hidden");
                    document.getElementById("booking-purpose").focus();
                    return;
                }
                btn.disabled = true;
                await submitBookingForSlot(newSlotId);
                btn.disabled = false;
            });
        });
    } catch (err) {
        altList.innerHTML = `<p class="alert-error">${escapeHtml(err.message)}</p>`;
    }
}

async function loadMyBookings() {
    const unauthEl = document.getElementById("my-bookings-unauth");
    const emptyEl = document.getElementById("my-bookings-empty");
    const listEl = document.getElementById("my-bookings-list");
    listEl.innerHTML = "";

    if (!state.currentUser) {
        unauthEl.classList.remove("hidden");
        emptyEl.classList.add("hidden");
        return;
    }
    unauthEl.classList.add("hidden");

    try {
        const bookings = await apiRequest("/api/bookings");
        if (!bookings || bookings.length === 0) {
            emptyEl.classList.remove("hidden");
            return;
        }
        emptyEl.classList.add("hidden");
        listEl.innerHTML = bookings.map((b) => renderBookingCard(b, false)).join("");
        attachBookingCardHandlers(listEl);
    } catch (err) {
        showGlobalAlert(err.message, "error");
    }
}

function switchDispatcherSubtab(subtab) {
    state.dispTab = subtab;
    document.querySelectorAll(".subnav-btn").forEach((b) => {
        const active = b.getAttribute("data-disp-tab") === subtab;
        b.classList.toggle("active", active);
        b.setAttribute("aria-selected", String(active));
    });
    document.getElementById("disp-panel-requests").classList.toggle("hidden", subtab !== "requests");
    document.getElementById("disp-panel-rooms").classList.toggle("hidden", subtab !== "rooms");
    document.getElementById("disp-panel-audit").classList.toggle("hidden", subtab !== "audit");

    if (subtab === "requests") loadDispatcherBookings();
    if (subtab === "rooms") loadDispatcherRooms();
    if (subtab === "audit") loadDispatcherAudit();
}

async function loadDispatcherBookings() {
    const listEl = document.getElementById("dispatcher-bookings-list");
    const statusFilter = document.getElementById("dispatcher-status-filter").value;
    listEl.innerHTML = "";

    const params = new URLSearchParams({ allRequests: "true" });
    if (statusFilter) params.set("status", statusFilter);

    try {
        const bookings = await apiRequest(`/api/bookings?${params.toString()}`);
        if (!bookings || bookings.length === 0) {
            listEl.innerHTML = `<div class="state-box">Заявок за обраним фільтром не знайдено.</div>`;
            return;
        }
        listEl.innerHTML = bookings.map((b) => renderBookingCard(b, true)).join("");
        attachBookingCardHandlers(listEl);
    } catch (err) {
        showGlobalAlert(err.message, "error");
    }
}

async function loadDispatcherRooms() {
    const errBox = document.getElementById("dispatcher-rooms-error");
    const listEl = document.getElementById("dispatcher-rooms-list");
    errBox.classList.add("hidden");
    listEl.innerHTML = "";

    try {
        const rooms = await apiRequest("/api/rooms");
        state.roomsCache = rooms;
        listEl.innerHTML = rooms
            .map((r) => {
                const targetActive = !r.active;
                const btnLabel = r.active ? "Закрити на ремонт" : "Відкрити для бронювання";
                const btnClass = r.active ? "btn-danger" : "btn-primary";
                const statusBadge = r.active
                    ? `<span class="badge badge-active">Активна • ${r.capacity} місць</span>`
                    : `<span class="badge badge-inactive">Недоступна (закрита)</span>`;
                return `
                    <article class="booking-card">
                        <div class="booking-card-top">
                            <div>
                                <strong>${escapeHtml(r.name)}</strong> • ${escapeHtml(r.building)}
                            </div>
                            ${statusBadge}
                        </div>
                        <div class="equipment-tags">
                            ${r.equipment.map((eq) => `<span class="eq-tag">${escapeHtml(eq)}</span>`).join("")}
                        </div>
                        ${
                            !r.active && r.unavailableReason
                                ? `<p class="room-reason-note">Причина закриття: ${escapeHtml(r.unavailableReason)}</p>`
                                : ""
                        }
                        <div class="booking-actions">
                            <label class="skip-link" for="room-reason-${escapeHtml(r.id)}">Причина зміни стану</label>
                            <input
                                type="text"
                                class="room-reason-input"
                                id="room-reason-${escapeHtml(r.id)}"
                                placeholder="${
                                    r.active
                                        ? "Причина закриття (напр.: Плановий ремонт проєктора)"
                                        : "Підстава відкриття (напр.: Ремонтні роботи завершено)"
                                }"
                            >
                            <button
                                type="button"
                                class="btn btn-sm ${btnClass} btn-toggle-room"
                                data-room-id="${escapeHtml(r.id)}"
                                data-target-active="${targetActive}"
                            >
                                ${btnLabel}
                            </button>
                        </div>
                    </article>
                `;
            })
            .join("");

        listEl.querySelectorAll(".btn-toggle-room").forEach((btn) => {
            btn.addEventListener("click", async () => {
                const roomId = btn.getAttribute("data-room-id");
                const targetActive = btn.getAttribute("data-target-active") === "true";
                const reasonInput = document.getElementById(`room-reason-${roomId}`);
                const reason = (reasonInput?.value || "").trim();
                errBox.classList.add("hidden");
                errBox.innerHTML = "";
                btn.disabled = true;

                try {
                    const updated = await apiRequest(`/api/rooms/${encodeURIComponent(roomId)}/availability`, {
                        method: "PATCH",
                        body: JSON.stringify({ active: targetActive, reason }),
                    });
                    showGlobalAlert(
                        `Стан аудиторії «${updated.name}» змінено на ${updated.active ? "Активна" : "Закрита"}.`,
                        "success"
                    );
                    await populateRoomsDropdown();
                    await loadDispatcherRooms();
                    await loadCatalogAndSchedule();
                } catch (err) {
                    let detailsHtml = `<p><strong>${escapeHtml(err.message)}</strong></p>`;
                    if (err.details && err.details.length > 0) {
                        const items = err.details
                            .map(
                                (c) =>
                                    `<li>Заявка #${escapeHtml(c.bookingId)} [${escapeHtml(c.status)}]: ${escapeHtml(
                                        c.slotDate
                                    )}, ${c.pairNumber} пара — ${escapeHtml(c.applicantName)} (${escapeHtml(c.purpose)})</li>`
                            )
                            .join("");
                        detailsHtml += `<ul class="conflict-list">${items}</ul>`;
                    }
                    errBox.innerHTML = detailsHtml;
                    errBox.classList.remove("hidden");
                } finally {
                    btn.disabled = false;
                }
            });
        });
    } catch (err) {
        showGlobalAlert(err.message, "error");
    }
}

async function loadDispatcherAudit() {
    const wrap = document.getElementById("dispatcher-audit-table-wrap");
    const entityFilter = document.getElementById("audit-entity-filter").value;
    wrap.innerHTML = `<div class="state-box">Завантаження журналу аудиту...</div>`;

    const params = new URLSearchParams({ limit: "100" });
    if (entityFilter) params.set("entityType", entityFilter);

    try {
        const events = await apiRequest(`/api/audit?${params.toString()}`);
        if (!events || events.length === 0) {
            wrap.innerHTML = `<div class="state-box">Подій аудиту за обраним фільтром ще немає.</div>`;
            return;
        }
        const rowsHtml = events
            .map(
                (ev) => `
                <tr>
                    <td>${escapeHtml(ev.occurredAt.slice(0, 19).replace("T", " "))}</td>
                    <td>${escapeHtml(ev.entityType === "BOOKING" ? "Бронювання" : "Аудиторія")}</td>
                    <td>${escapeHtml(ev.targetLabel)}<br><small>#${escapeHtml(ev.entityId)}</small></td>
                    <td>${escapeHtml(ev.actorName)}</td>
                    <td>${escapeHtml(ev.oldState || "—")} &rarr; <strong>${escapeHtml(ev.newState)}</strong></td>
                    <td>${escapeHtml(ev.comment)}</td>
                </tr>
            `
            )
            .join("");

        wrap.innerHTML = `
            <table class="audit-table">
                <thead>
                    <tr>
                        <th scope="col">Час події (UTC/ISO)</th>
                        <th scope="col">Тип</th>
                        <th scope="col">Об'єкт</th>
                        <th scope="col">Ініціатор</th>
                        <th scope="col">Перехід стану</th>
                        <th scope="col">Коментар / причина</th>
                    </tr>
                </thead>
                <tbody>${rowsHtml}</tbody>
            </table>
        `;
    } catch (err) {
        wrap.innerHTML = `<div class="alert alert-error">${escapeHtml(err.message)}</div>`;
    }
}

function renderBookingCard(b, isDispatcherView) {
    const timeRange = `${b.startsAt.slice(11, 16)}–${b.endsAt.slice(11, 16)}`;
    const isFutureSlot = new Date(b.startsAt).getTime() > Date.now();
    const canCancel = isFutureSlot && (b.status === "PENDING" || b.status === "CONFIRMED");
    const canDecide = isFutureSlot && isDispatcherView && b.status === "PENDING";

    const historyHtml = (b.history || [])
        .map(
            (ev) =>
                `<div>• [${escapeHtml(ev.occurredAt.slice(0, 19).replace("T", " "))}] <strong>${escapeHtml(
                    ev.newStatus
                )}</strong> (${escapeHtml(ev.actorName)}): ${escapeHtml(ev.comment)}</div>`
        )
        .join("");

    return `
        <article class="booking-card">
            <div class="booking-card-top">
                <div>
                    <strong>Заявка #${escapeHtml(b.id)}</strong> • ${escapeHtml(b.roomName)} (${escapeHtml(b.building)})
                </div>
                <span class="status-pill status-${escapeHtml(b.status)}">${escapeHtml(
        STATUS_LABELS[b.status] || b.status
    )}</span>
            </div>
            <div>
                <strong>Дата і слот:</strong> ${escapeHtml(b.slotDate)}, ${b.pairNumber}-та пара (${escapeHtml(timeRange)})<br>
                <strong>Заявник:</strong> ${escapeHtml(b.applicantName)} • <strong>Учасників:</strong> ${b.expectedAttendees}<br>
                <strong>Мета:</strong> ${escapeHtml(b.purpose)}
                ${
                    b.decisionComment
                        ? `<br><strong>Коментар рішення:</strong> ${escapeHtml(b.decisionComment)}`
                        : ""
                }
            </div>
            <div class="booking-history">
                <strong>Історія переходів стану:</strong>
                ${historyHtml}
            </div>
            <div class="booking-actions">
                ${
                    canCancel
                        ? `<button type="button" class="btn btn-sm btn-outline btn-cancel-bk" data-id="${escapeHtml(
                              b.id
                          )}">Скасувати заявку</button>`
                        : ""
                }
                ${
                    canDecide
                        ? `
                        <input type="text" class="dispatcher-comment-input" id="comment-${escapeHtml(
                            b.id
                        )}" placeholder="Коментар або причина відхилення (мін. 5 симв.)">
                        <button type="button" class="btn btn-sm btn-primary btn-confirm-bk" data-id="${escapeHtml(
                            b.id
                        )}">Погодити</button>
                        <button type="button" class="btn btn-sm btn-danger btn-reject-bk" data-id="${escapeHtml(
                            b.id
                        )}">Відхилити</button>
                    `
                        : ""
                }
            </div>
        </article>
    `;
}

function attachBookingCardHandlers(container) {
    container.querySelectorAll(".btn-cancel-bk").forEach((btn) => {
        btn.addEventListener("click", async () => {
            const id = btn.getAttribute("data-id");
            btn.disabled = true;
            try {
                await apiRequest(`/api/bookings/${encodeURIComponent(id)}/cancel`, {
                    method: "POST",
                    body: JSON.stringify({ reason: "Скасовано користувачем через вебінтерфейс" }),
                });
                showGlobalAlert(`Заявку #${id} успішно скасовано. Слот знову вільний.`, "success");
                await loadMyBookings();
                if (state.currentUser?.role === "DISPATCHER") {
                    await loadDispatcherBookings();
                }
                await loadCatalogAndSchedule();
            } catch (err) {
                showGlobalAlert(err.message, "error");
            } finally {
                btn.disabled = false;
            }
        });
    });

    container.querySelectorAll(".btn-confirm-bk").forEach((btn) => {
        btn.addEventListener("click", async () => {
            const id = btn.getAttribute("data-id");
            const commentInput = document.getElementById(`comment-${id}`);
            const comment = commentInput ? commentInput.value : "Погоджено диспетчером";
            btn.disabled = true;
            try {
                await apiRequest(`/api/bookings/${encodeURIComponent(id)}/decision`, {
                    method: "POST",
                    body: JSON.stringify({ action: "CONFIRM", comment: comment || "Погоджено диспетчером" }),
                });
                showGlobalAlert(`Заявку #${id} підтверджено (CONFIRMED).`, "success");
                await loadDispatcherBookings();
                await loadCatalogAndSchedule();
            } catch (err) {
                showGlobalAlert(err.message, "error");
            } finally {
                btn.disabled = false;
            }
        });
    });

    container.querySelectorAll(".btn-reject-bk").forEach((btn) => {
        btn.addEventListener("click", async () => {
            const id = btn.getAttribute("data-id");
            const commentInput = document.getElementById(`comment-${id}`);
            const comment = commentInput ? commentInput.value : "";
            btn.disabled = true;
            try {
                await apiRequest(`/api/bookings/${encodeURIComponent(id)}/decision`, {
                    method: "POST",
                    body: JSON.stringify({ action: "REJECT", comment }),
                });
                showGlobalAlert(`Заявку #${id} відхилено (REJECTED). Слот звільнено.`, "warning");
                await loadDispatcherBookings();
                await loadCatalogAndSchedule();
            } catch (err) {
                showGlobalAlert(err.message, "error");
            } finally {
                btn.disabled = false;
            }
        });
    });
}

function switchTab(tabName) {
    hideGlobalAlert();
    document.querySelectorAll(".nav-btn").forEach((b) => {
        b.classList.toggle("active", b.getAttribute("data-tab") === tabName);
    });
    document.getElementById("view-catalog").classList.toggle("hidden", tabName !== "catalog");
    document.getElementById("view-my-bookings").classList.toggle("hidden", tabName !== "my-bookings");
    document.getElementById("view-dispatcher").classList.toggle("hidden", tabName !== "dispatcher");

    if (tabName === "catalog") loadCatalogAndSchedule();
    if (tabName === "my-bookings") loadMyBookings();
    if (tabName === "dispatcher") switchDispatcherSubtab(state.dispTab);
}

document.addEventListener("DOMContentLoaded", async () => {
    const defaultDate = getNextMondayIso();
    document.getElementById("filter-date").value = defaultDate;

    await populateRoomsDropdown();
    await refreshAuthStatus();
    await loadCatalogAndSchedule();

    // Навігація вкладками
    document.querySelectorAll(".nav-btn").forEach((btn) => {
        btn.addEventListener("click", () => switchTab(btn.getAttribute("data-tab")));
    });

    // Режим денного / тижневого календаря
    document.getElementById("btn-mode-day").addEventListener("click", () => setCatalogMode("day"));
    document.getElementById("btn-mode-week").addEventListener("click", () => setCatalogMode("week"));
    document.getElementById("filter-room").addEventListener("change", () => {
        if (state.catalogMode === "week") loadCatalogAndSchedule();
    });

    // Підвкладки диспетчера
    document.querySelectorAll(".subnav-btn").forEach((btn) => {
        btn.addEventListener("click", () => switchDispatcherSubtab(btn.getAttribute("data-disp-tab")));
    });

    // Фільтри каталогу
    document.getElementById("filter-form").addEventListener("submit", (e) => {
        e.preventDefault();
        loadCatalogAndSchedule();
    });

    document.getElementById("btn-reset-filters").addEventListener("click", () => {
        document.getElementById("filter-date").value = getNextMondayIso();
        document.getElementById("filter-capacity").value = "";
        document.getElementById("filter-equipment").value = "";
        document.getElementById("filter-available").checked = false;
        loadCatalogAndSchedule();
    });

    // Діалог створення бронювання
    const bookingDialog = document.getElementById("booking-dialog");
    bookingDialog.addEventListener("close", () => {
        state.selectedSlot = null;
    });
    document.getElementById("btn-close-booking-dialog").addEventListener("click", () => bookingDialog.close());
    document.getElementById("btn-cancel-booking-modal").addEventListener("click", () => bookingDialog.close());
    document.getElementById("btn-find-alternatives").addEventListener("click", () => loadAlternativesForCurrentSlot());

    document.getElementById("create-booking-form").addEventListener("submit", async (e) => {
        e.preventDefault();
        const submitBtn = document.getElementById("btn-submit-booking");
        const slotId = document.getElementById("booking-slot-id").value;
        submitBtn.disabled = true;
        await submitBookingForSlot(slotId);
        submitBtn.disabled = false;
    });

    // Вікно входу
    const loginDialog = document.getElementById("login-dialog");
    document.getElementById("btn-open-login").addEventListener("click", () => {
        state.pendingLoginSlot = null;
        loginDialog.showModal();
    });
    document.getElementById("btn-close-login-dialog").addEventListener("click", () => loginDialog.close());
    document.getElementById("btn-cancel-login").addEventListener("click", () => loginDialog.close());

    document.getElementById("login-form").addEventListener("submit", async (e) => {
        e.preventDefault();
        const loginError = document.getElementById("login-error");
        loginError.classList.add("hidden");
        const username = document.getElementById("login-username").value;
        const password = document.getElementById("login-password").value;

        try {
            const user = await apiRequest("/api/auth/login", {
                method: "POST",
                body: JSON.stringify({ username, password }),
            });
            loginDialog.close();
            await refreshAuthStatus();
            if (user.role !== "DISPATCHER" && !document.getElementById("view-dispatcher").classList.contains("hidden")) {
                switchTab("catalog");
            }
            showGlobalAlert(`Ви увійшли як ${user.displayName} (${user.role}).`, "success");
            if (state.pendingLoginSlot) {
                const slotToReopen = state.pendingLoginSlot;
                state.pendingLoginSlot = null;
                openBookingDialog(slotToReopen);
            }
        } catch (err) {
            loginError.textContent = err.message;
            loginError.classList.remove("hidden");
        }
    });

    document.getElementById("btn-logout").addEventListener("click", async () => {
        try {
            await apiRequest("/api/auth/logout", { method: "POST" });
        } finally {
            await refreshAuthStatus();
            switchTab("catalog");
            showGlobalAlert("Сеанс завершено.", "warning");
        }
    });

    // Швидке перемикання ролей на панелі демонстрації
    document.querySelectorAll("[data-quick-user]").forEach((btn) => {
        btn.addEventListener("click", async () => {
            const username = btn.getAttribute("data-quick-user");
            const password = btn.getAttribute("data-quick-pass");
            try {
                const user = await apiRequest("/api/auth/login", {
                    method: "POST",
                    body: JSON.stringify({ username, password }),
                });
                await refreshAuthStatus();
                showGlobalAlert(`Швидкий вхід виконано: ${user.displayName} [${user.role}]`, "success");
                if (user.role === "DISPATCHER") {
                    switchTab("dispatcher");
                } else {
                    if (!document.getElementById("view-dispatcher").classList.contains("hidden")) {
                        switchTab("catalog");
                    } else {
                        await loadCatalogAndSchedule();
                    }
                    await loadMyBookings();
                }
            } catch (err) {
                showGlobalAlert(err.message, "error");
            }
        });
    });

    // Демонстрація негативного тест-кейсу TC-ACCESS-02 (спроба читання чужої заявки)
    document.getElementById("btn-test-foreign-access").addEventListener("click", async () => {
        try {
            await apiRequest("/api/bookings/bk-demo-other-user-03");
            showGlobalAlert(
                "Заявку bk-demo-other-user-03 прочитано (дозволено для власника або диспетчера).",
                "success"
            );
        } catch (err) {
            showGlobalAlert(
                `Тест TC-ACCESS-02 пройдено (HTTP ${err.status}, код ${err.code}): ${err.message}`,
                "error"
            );
        }
    });

    document.getElementById("btn-refresh-dispatcher").addEventListener("click", () => loadDispatcherBookings());
    document.getElementById("dispatcher-status-filter").addEventListener("change", () => loadDispatcherBookings());
    document.getElementById("btn-refresh-audit").addEventListener("click", () => loadDispatcherAudit());
    document.getElementById("audit-entity-filter").addEventListener("change", () => loadDispatcherAudit());
});
