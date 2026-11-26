# -*- coding: utf-8 -*-
"""
Скрипт автоматизованої перевірки якості вебінтерфейсу та продуктивності:
1) Перевірка доступності WCAG 2.2 AA (HTML семантика, підписи <label>, контрастність кольорів CSS).
2) Навантажувальне тестування реального ASGI-сервера Uvicorn (50 паралельних клієнтів, p50 / p95 / max).
3) Зняття скріншотів адаптивності інтерфейсу для ширин 360px, 768px і 1440px (Firefox BiDi та Edge CDP).
"""
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from seed.seed_data import demo_dates, seed_database, slot_id
from src.database import get_connection

EDGE_CANDIDATES = [
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
]
EDGE_PATH = next((p for p in EDGE_CANDIDATES if p.exists()), EDGE_CANDIDATES[0])
FIREFOX_PATH = Path(r"C:\Program Files\Mozilla Firefox\firefox.exe")
SCREENSHOT_DIR = ROOT_DIR / "docs" / "screenshots"
REPORT_PATH = ROOT_DIR / "docs" / "quality_report_stage9.json"


# --- 1. WCAG 2.2 AA: математика контрастності та перевірка HTML ---
def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _relative_luminance(hex_color: str) -> float:
    def chan(v: int) -> float:
        c = v / 255.0
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = _hex_to_rgb(hex_color)
    return 0.2126 * chan(r) + 0.7152 * chan(g) + 0.0722 * chan(b)


def contrast_ratio(fg_hex: str, bg_hex: str) -> float:
    l1 = _relative_luminance(fg_hex)
    l2 = _relative_luminance(bg_hex)
    hi, lo = max(l1, l2), min(l1, l2)
    return round((hi + 0.05) / (lo + 0.05), 2)


class AccessibilityHtmlChecker(HTMLParser):
    def __init__(self):
        super().__init__()
        self.lang = ""
        self.landmarks: set[str] = set()
        self.has_skip_link = False
        self.inline_style_count = 0
        self.labels_for: set[str] = set()
        self.controls: list[tuple[str, str, str]] = []  # (tag, id, type)
        self.buttons_without_text_missing_aria: list[str] = []
        self._in_button = False
        self._btn_has_aria = False
        self._btn_id = ""
        self._btn_text = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {k: (v or "") for k, v in attrs}
        if "style" in attr_map:
            self.inline_style_count += 1
        if tag == "html":
            self.lang = attr_map.get("lang", "")
        if tag in ("header", "nav", "main", "footer"):
            self.landmarks.add(tag)
        if tag == "a" and "skip-link" in attr_map.get("class", ""):
            self.has_skip_link = True
        if tag == "label" and attr_map.get("for"):
            self.labels_for.add(attr_map["for"])
        if tag in ("input", "select", "textarea"):
            if attr_map.get("type", "") != "hidden":
                self.controls.append((tag, attr_map.get("id", ""), attr_map.get("type", "")))
        if tag == "button":
            self._in_button = True
            self._btn_has_aria = bool(attr_map.get("aria-label"))
            self._btn_id = attr_map.get("id", "")
            self._btn_text = ""

    def handle_data(self, data: str) -> None:
        if self._in_button:
            self._btn_text += data.strip()

    def handle_endtag(self, tag: str) -> None:
        if tag == "button" and self._in_button:
            if not self._btn_text and not self._btn_has_aria:
                self.buttons_without_text_missing_aria.append(self._btn_id or "unknown")
            self._in_button = False


def check_wcag_compliance() -> dict:
    html_text = (ROOT_DIR / "src" / "templates" / "index.html").read_text(encoding="utf-8")
    checker = AccessibilityHtmlChecker()
    checker.feed(html_text)

    unlabeled = [f"{tag}#{cid}" for tag, cid, _ in checker.controls if not cid or cid not in checker.labels_for]

    # Кольорові пари з src/static/css/styles.css
    pairs = [
        ("Основний текст на фоні картки", "#0f172a", "#ffffff"),
        ("Вторинний текст на фоні сторінки", "#334155", "#f8fafc"),
        ("Кнопка Primary (білий на #1d4ed8)", "#ffffff", "#1d4ed8"),
        ("Кнопка Secondary (білий на #0f766e)", "#ffffff", "#0f766e"),
        ("Кнопка Warning (білий на #b45309)", "#ffffff", "#b45309"),
        ("Кнопка Danger (білий на #b91c1c)", "#ffffff", "#b91c1c"),
        ("Статус CONFIRMED (#14532d на #dcfce7)", "#14532d", "#dcfce7"),
        ("Статус PENDING (#78350f на #fef3c7)", "#78350f", "#fef3c7"),
        ("Статус REJECTED (#7f1d1d на #fee2e2)", "#7f1d1d", "#fee2e2"),
    ]
    contrast_results = []
    min_ratio = 99.0
    for name, fg, bg in pairs:
        ratio = contrast_ratio(fg, bg)
        min_ratio = min(min_ratio, ratio)
        contrast_results.append({"pair": name, "fg": fg, "bg": bg, "ratio": ratio, "passesAA": ratio >= 4.5})

    css_text = (ROOT_DIR / "src" / "static" / "css" / "styles.css").read_text(encoding="utf-8")
    has_focus_visible = ":focus-visible" in css_text

    return {
        "lang": checker.lang,
        "landmarks": sorted(checker.landmarks),
        "hasSkipLink": checker.has_skip_link,
        "hasFocusVisibleCss": has_focus_visible,
        "inlineStylesInHtml": checker.inline_style_count,
        "controlsCount": len(checker.controls),
        "unlabeledControls": unlabeled,
        "buttonsMissingAria": checker.buttons_without_text_missing_aria,
        "minContrastRatio": min_ratio,
        "allContrastPassesAA": min_ratio >= 4.5,
        "contrastDetails": contrast_results,
    }


# --- 2. Сервер і навантажувальний тест ---
def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_server(base_url: str, timeout: float = 15.0) -> None:
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        try:
            with urllib.request.urlopen(f"{base_url}/api/health", timeout=1.5) as resp:
                if resp.status == 200:
                    return
        except Exception:
            time.sleep(0.2)
    raise RuntimeError("Не вдалося дочекатися запуску сервера Uvicorn.")


def _http_get(url: str) -> tuple[int, float]:
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(url, timeout=10.0) as resp:
            resp.read()
            return resp.status, (time.perf_counter() - t0) * 1000.0
    except urllib.error.HTTPError as err:
        err.read()
        return err.code, (time.perf_counter() - t0) * 1000.0


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round((p / 100.0) * (len(ordered) - 1)))))
    return round(ordered[idx], 2)


def _login_opener(base_url: str, username: str, password: str) -> tuple[urllib.request.OpenerDirector, str]:
    cj = urllib.request.HTTPCookieProcessor()
    opener = urllib.request.build_opener(cj)
    req = urllib.request.Request(
        f"{base_url}/api/auth/login",
        data=json.dumps({"username": username, "password": password}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with opener.open(req, timeout=10.0) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
        return opener, payload["csrfToken"]


def run_load_benchmarks(base_url: str, db_path: Path) -> dict:
    d = demo_dates()
    # Сценарій 1: 50 паралельних клієнтів -> GET /api/slots (150 запитів)
    slots_url = f"{base_url}/api/slots?date={d['mon']}"
    t_start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=50) as pool:
        res_slots = list(pool.map(lambda _: _http_get(slots_url), range(150)))
    total_s = time.perf_counter() - t_start
    lat_slots = [r[1] for r in res_slots]
    err_slots = sum(1 for code, _ in res_slots if code != 200)

    # Сценарій 2: 50 паралельних клієнтів -> GET /api/slots/{id}/alternatives (100 запитів)
    busy_slot = slot_id("room-102", d["mon"], 2)
    alt_url = f"{base_url}/api/slots/{busy_slot}/alternatives?minCapacity=25"
    t_start2 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=50) as pool:
        res_alts = list(pool.map(lambda _: _http_get(alt_url), range(100)))
    total_a = time.perf_counter() - t_start2
    lat_alts = [r[1] for r in res_alts]
    err_alts = sum(1 for code, _ in res_alts if code != 200)

    # Сценарій 3: Гонка записів (50 паралельних POST /api/bookings на один вільний слот)
    race_slot = slot_id("room-104", d["fri"], 4)
    # Отримаємо токен заявника та будемо передавати однакові cookie+csrf (імітація 50 паралельних кліків)
    opener, csrf = _login_opener(base_url, "kovalenko", "ApplicantPass2026!")

    def post_race(_idx: int) -> tuple[int, float]:
        t0 = time.perf_counter()
        req = urllib.request.Request(
            f"{base_url}/api/bookings",
            data=json.dumps(
                {"slotId": race_slot, "purpose": "Навантажувальний тест гонки слота", "expectedAttendees": 20}
            ).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-CSRF-Token": csrf},
            method="POST",
        )
        try:
            with opener.open(req, timeout=10.0) as resp:
                resp.read()
                return resp.status, (time.perf_counter() - t0) * 1000.0
        except urllib.error.HTTPError as err:
            err.read()
            return err.code, (time.perf_counter() - t0) * 1000.0

    with ThreadPoolExecutor(max_workers=50) as pool:
        res_race = list(pool.map(post_race, range(50)))
    codes_race = [c for c, _ in res_race]
    lat_race = [l for _, l in res_race]

    conn = get_connection(db_path)
    try:
        db_active_cnt = conn.execute(
            "SELECT COUNT(*) AS c FROM bookings WHERE slot_id = ? AND status IN ('PENDING', 'CONFIRMED')",
            (race_slot,),
        ).fetchone()["c"]
    finally:
        conn.close()

    return {
        "getSlots50Concurrency": {
            "requests": len(res_slots),
            "concurrency": 50,
            "errors": err_slots,
            "p50Ms": round(statistics.median(lat_slots), 2),
            "p95Ms": _percentile(lat_slots, 95),
            "maxMs": round(max(lat_slots), 2),
            "rps": round(len(res_slots) / total_s, 1),
        },
        "getAlternatives50Concurrency": {
            "requests": len(res_alts),
            "concurrency": 50,
            "errors": err_alts,
            "p50Ms": round(statistics.median(lat_alts), 2),
            "p95Ms": _percentile(lat_alts, 95),
            "maxMs": round(max(lat_alts), 2),
            "rps": round(len(res_alts) / total_a, 1),
        },
        "postRace50Concurrency": {
            "requests": len(res_race),
            "concurrency": 50,
            "created201Count": codes_race.count(201),
            "conflict409Count": codes_race.count(409),
            "serverErrors5xx": sum(1 for c in codes_race if c >= 500),
            "dbActiveRowsForSlot": int(db_active_cnt),
            "p50Ms": round(statistics.median(lat_race), 2),
            "p95Ms": _percentile(lat_race, 95),
        },
    }


# --- 3. Скріншоти адаптивності й перевірка scrollWidth через headless Edge + CDP ---
import base64
import hashlib
import struct


class _SimpleCdpClient:
    """Мінімалістичний WebSocket-клієнт (RFC 6455) для Chrome DevTools Protocol без зовнішніх бібліотек."""

    def __init__(self, ws_url: str):
        # ws://127.0.0.1:PORT/devtools/page/ID
        without_scheme = ws_url.split("ws://", 1)[1]
        host_port, path = without_scheme.split("/", 1)
        host, port_str = host_port.split(":")
        self.sock = socket.create_connection((host, int(port_str)), timeout=15.0)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        handshake = (
            f"GET /{path} HTTP/1.1\r\n"
            f"Host: {host_port}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(handshake.encode("ascii"))
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += self.sock.recv(4096)
        self._extra = buf.split(b"\r\n\r\n", 1)[1]
        self._msg_id = 0

    def _recv_exact(self, n: int) -> bytes:
        chunks = []
        remaining = n
        if self._extra:
            take = self._extra[:remaining]
            chunks.append(take)
            self._extra = self._extra[remaining:]
            remaining -= len(take)
        while remaining > 0:
            data = self.sock.recv(remaining)
            if not data:
                raise ConnectionError("CDP socket closed")
            chunks.append(data)
            remaining -= len(data)
        return b"".join(chunks)

    def _send_frame(self, text: str) -> None:
        payload = text.encode("utf-8")
        header = bytearray([0x81])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        mask = os.urandom(4)
        header.extend(mask)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def _recv_frame(self) -> str:
        while True:
            b1, b2 = self._recv_exact(2)
            opcode = b1 & 0x0F
            length = b2 & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._recv_exact(8))[0]
            if b2 & 0x80:
                mask = self._recv_exact(4)
                raw = bytes(b ^ mask[i % 4] for i, b in enumerate(self._recv_exact(length)))
            else:
                raw = self._recv_exact(length)
            if opcode == 0x8:
                raise ConnectionError("WebSocket closed")
            if opcode == 0x9:  # ping -> pong
                continue
            return raw.decode("utf-8", errors="replace")

    def call(self, method: str, params: dict | None = None) -> dict:
        self._msg_id += 1
        req_id = self._msg_id
        self._send_frame(json.dumps({"id": req_id, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(self._recv_frame())
            if msg.get("id") == req_id:
                return msg.get("result", {})

    def close(self) -> None:
        self.sock.close()


def capture_responsive_screenshots(base_url: str) -> list[dict]:
    if not EDGE_PATH.exists():
        return []
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    cdp_port = _find_free_port()
    profile_dir = Path(tempfile.mkdtemp(prefix="edge_cdp_"))
    edge_proc = subprocess.Popen(
        [
            str(EDGE_PATH),
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            f"--remote-debugging-port={cdp_port}",
            f"--user-data-dir={profile_dir}",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    results = []
    try:
        # Очікуємо готовності CDP
        ws_url = ""
        for _ in range(40):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{cdp_port}/json/list", timeout=1.0) as resp:
                    targets = json.loads(resp.read().decode("utf-8"))
                    page_targets = [t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
                    if page_targets:
                        ws_url = page_targets[0]["webSocketDebuggerUrl"]
                        break
            except Exception:
                time.sleep(0.2)
        if not ws_url:
            return []

        cdp = _SimpleCdpClient(ws_url)
        try:
            cdp.call("Page.enable")
            cdp.call("Runtime.enable")
            viewports = [
                ("desktop_1440", 1440, 900, False),
                ("tablet_768", 768, 1024, False),
                ("mobile_360", 360, 800, True),
            ]
            for name, w, h, is_mobile in viewports:
                cdp.call(
                    "Emulation.setDeviceMetricsOverride",
                    {"width": w, "height": h, "deviceScaleFactor": 1, "mobile": is_mobile},
                )
                cdp.call("Page.navigate", {"url": base_url + "/"})
                time.sleep(1.2)
                eval_res = cdp.call(
                    "Runtime.evaluate",
                    {
                        "expression": "JSON.stringify({innerWidth: window.innerWidth, scrollWidth: document.documentElement.scrollWidth})",
                        "returnByValue": True,
                    },
                )
                metrics = json.loads(eval_res["result"]["value"])
                shot = cdp.call("Page.captureScreenshot", {"format": "png"})
                out_png = SCREENSHOT_DIR / f"ui_{name}.png"
                out_png.write_bytes(base64.b64decode(shot["data"]))
                results.append(
                    {
                        "name": name,
                        "width": w,
                        "height": h,
                        "innerWidth": metrics["innerWidth"],
                        "scrollWidth": metrics["scrollWidth"],
                        "horizontalOverflow": metrics["scrollWidth"] > metrics["innerWidth"],
                        "file": str(out_png),
                        "bytes": out_png.stat().st_size,
                    }
                )
        finally:
            cdp.close()
    finally:
        edge_proc.terminate()
        try:
            edge_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            edge_proc.kill()
        shutil.rmtree(profile_dir, ignore_errors=True)
    return results


def capture_firefox_screenshots(base_url: str) -> list[dict]:
    if not FIREFOX_PATH.exists():
        return []
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    bidi_port = _find_free_port()
    profile_dir = Path(tempfile.mkdtemp(prefix="firefox_bidi_"))
    ff_proc = subprocess.Popen(
        [
            str(FIREFOX_PATH),
            "--headless",
            "--no-remote",
            "-profile",
            str(profile_dir),
            f"--remote-debugging-port={bidi_port}",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    results = []
    try:
        bidi = None
        deadline = time.time() + 15.0
        while time.time() < deadline:
            try:
                bidi = _SimpleCdpClient(f"ws://127.0.0.1:{bidi_port}/session")
                break
            except Exception:
                time.sleep(0.25)
        if bidi is None:
            return []

        try:
            sess = bidi.call("session.new", {"capabilities": {}})
            browser_version = sess.get("capabilities", {}).get("browserVersion", "unknown")
            tree = bidi.call("browsingContext.getTree", {})
            ctx_id = tree["contexts"][0]["context"]

            viewports = [
                ("desktop_1440", 1440, 900),
                ("tablet_768", 768, 1024),
                ("mobile_360", 360, 800),
            ]
            for name, w, h in viewports:
                bidi.call(
                    "browsingContext.setViewport",
                    {"context": ctx_id, "viewport": {"width": w, "height": h}},
                )
                bidi.call(
                    "browsingContext.navigate",
                    {"context": ctx_id, "url": base_url + "/", "wait": "complete"},
                )
                time.sleep(1.2)
                eval_res = bidi.call(
                    "script.evaluate",
                    {
                        "expression": "JSON.stringify({innerWidth: window.innerWidth, scrollWidth: document.documentElement.scrollWidth})",
                        "target": {"context": ctx_id},
                        "awaitPromise": False,
                    },
                )
                metrics = json.loads(eval_res["result"]["value"])
                shot = bidi.call(
                    "browsingContext.captureScreenshot",
                    {"context": ctx_id, "origin": "viewport"},
                )
                out_png = SCREENSHOT_DIR / f"ui_firefox_{name}.png"
                out_png.write_bytes(base64.b64decode(shot["data"]))
                results.append(
                    {
                        "browser": f"Mozilla Firefox {browser_version}",
                        "name": name,
                        "width": w,
                        "height": h,
                        "innerWidth": metrics["innerWidth"],
                        "scrollWidth": metrics["scrollWidth"],
                        "horizontalOverflow": metrics["scrollWidth"] > metrics["innerWidth"],
                        "file": str(out_png),
                        "bytes": out_png.stat().st_size,
                    }
                )
        finally:
            bidi.close()
    finally:
        ff_proc.terminate()
        try:
            ff_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            ff_proc.kill()
        shutil.rmtree(profile_dir, ignore_errors=True)
    return results


def main() -> None:
    wcag = check_wcag_compliance()

    tmp_dir = Path(tempfile.mkdtemp(prefix="stage9_bench_"))
    db_path = tmp_dir / "bench.db"
    seed_database(db_path)

    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    env = os.environ.copy()
    env["DATABASE_PATH"] = str(db_path)

    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "src.main:app", "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=str(ROOT_DIR),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_server(base_url)
        benchmarks = run_load_benchmarks(base_url, db_path)
        screenshots = capture_responsive_screenshots(base_url)
        firefox_screenshots = capture_firefox_screenshots(base_url)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(tmp_dir, ignore_errors=True)

    report = {
        "wcag": wcag,
        "benchmarks": benchmarks,
        "screenshots": screenshots,
        "firefox_screenshots": firefox_screenshots,
    }
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
