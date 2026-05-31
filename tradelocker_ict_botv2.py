import base64
import csv
import json
import os
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytz
import requests
from dotenv import load_dotenv

load_dotenv()

ACCESS_TOKEN_TTL = 25 * 60
REQUEST_TIMEOUT = int(os.environ.get("REQUEST_TIMEOUT", "10"))

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

TRADELOCKER_EMAIL = os.environ.get("TRADELOCKER_EMAIL")
TRADELOCKER_PASSWORD = os.environ.get("TRADELOCKER_PASSWORD")
TRADELOCKER_SERVER = os.environ.get("TRADELOCKER_SERVER", "BLUEG")
REFRESH_TOKEN = os.environ.get("REFRESH_TOKEN")

TRADELOCKER_ACCOUNT_ID = os.environ.get("TRADELOCKER_ACCOUNT_ID", "").strip()
TRADELOCKER_ACC_NUM = os.environ.get("TRADELOCKER_ACC_NUM", "").strip()

RISK_USD = float(os.environ.get("RISK_USD", "25"))
DAILY_LOSS_LIMIT = float(os.environ.get("DAILY_LOSS_LIMIT", "75"))
MAX_TRADES_PER_DAY = int(os.environ.get("MAX_TRADES_PER_DAY", "3"))

SYMBOL = os.environ.get("SYMBOL", "NAS100")
SYMBOL_ES = os.environ.get("SYMBOL_ES", "SPX500")

NAS100_POINT_VALUE = float(os.environ.get("NAS100_POINT_VALUE", "1.0"))
SL_BUFFER_PCT = float(os.environ.get("SL_BUFFER_PCT", "0.0003"))
DAILY_SUMMARY_HOUR = int(os.environ.get("DAILY_SUMMARY_HOUR", "23"))

TL_ENV = os.environ.get("TRADELOCKER_ENV", "demo").strip().lower()
BASE_URL = (
    "https://live.tradelocker.com/backend-api"
    if TL_ENV == "live"
    else "https://demo.tradelocker.com/backend-api"
)

LOG_FILE = os.path.join(os.path.dirname(__file__), "trade_log.csv")
LOG_HEADERS = [
    "datetime_ny",
    "symbol",
    "side",
    "lot",
    "entry_price",
    "stop_loss",
    "take_profit",
    "risk_usd",
    "order_id",
    "outcome",
    "pnl_usd",
    "close_time",
]

ORDERS_HISTORY_PAGE_SIZE = 50

US_HOLIDAYS_2025 = {
    "2025-01-01", "2025-01-20", "2025-02-17", "2025-04-18",
    "2025-05-26", "2025-06-19", "2025-07-04", "2025-09-01",
    "2025-11-27", "2025-12-25",
}
US_HOLIDAYS_2026 = {
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03",
    "2026-05-25", "2026-06-19", "2026-07-03", "2026-09-07",
    "2026-11-26", "2026-12-25",
}
US_HOLIDAYS = US_HOLIDAYS_2025 | US_HOLIDAYS_2026


def ny_now():
    return datetime.now(pytz.timezone("America/New_York"))


def send_telegram(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"},
            timeout=REQUEST_TIMEOUT,
        )
    except Exception as e:
        print(f"⚠️ Không gửi được Telegram: {e}")


def get_refresh_token_days_left() -> int | None:
    try:
        if not REFRESH_TOKEN:
            return None
        payload_b64 = REFRESH_TOKEN.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        exp_ts = payload.get("exp")
        if not exp_ts:
            return None
        return int((exp_ts - datetime.now(timezone.utc).timestamp()) / 86400)
    except Exception:
        return None


def is_nfp_friday() -> bool:
    now = ny_now()
    return now.weekday() == 4 and now.day <= 7


def is_trading_allowed() -> tuple[bool, str]:
    now = ny_now()
    today = now.strftime("%Y-%m-%d")
    if now.weekday() >= 5:
        return False, "Cuối tuần — thị trường đóng cửa."
    if today in US_HOLIDAYS:
        return False, f"Ngày lễ Mỹ ({today}) — thị trường đóng cửa."
    if is_nfp_friday():
        return False, "Thứ Sáu NFP — bot tự bỏ qua phiên."
    return True, ""


def ensure_log_file():
    if os.path.isfile(LOG_FILE):
        return
    with open(LOG_FILE, "w", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=LOG_HEADERS).writeheader()


def read_log_rows():
    if not os.path.isfile(LOG_FILE):
        return []
    try:
        with open(LOG_FILE, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    except Exception:
        return []


def count_trades_today():
    today = ny_now().strftime("%Y-%m-%d")
    return sum(1 for row in read_log_rows() if row.get("datetime_ny", "").startswith(today))


def risk_deployed_today():
    today = ny_now().strftime("%Y-%m-%d")
    total = 0.0
    for row in read_log_rows():
        if row.get("datetime_ny", "").startswith(today):
            try:
                total += float(row.get("risk_usd") or 0)
            except ValueError:
                pass
    return total


def realized_pnl_today():
    today = ny_now().strftime("%Y-%m-%d")
    total = 0.0
    for row in read_log_rows():
        if row.get("datetime_ny", "").startswith(today) and row.get("outcome") in ("TP", "SL"):
            try:
                total += float(row.get("pnl_usd") or 0)
            except ValueError:
                pass
    return total


def send_daily_summary():
    now = ny_now()
    today = now.strftime("%Y-%m-%d")
    label = now.strftime("%d/%m/%Y")
    rows = [r for r in read_log_rows() if r.get("datetime_ny", "").startswith(today)]

    if not rows:
        send_telegram(f"📋 <b>TÓM TẮT NGÀY {label}</b>\n\n😴 Hôm nay không có lệnh nào.")
        return

    closed = [r for r in rows if r.get("outcome") in ("TP", "SL")]
    wins = [r for r in closed if r.get("outcome") == "TP"]
    losses = [r for r in closed if r.get("outcome") == "SL"]
    pnl = sum(float(r.get("pnl_usd") or 0) for r in closed)
    risk = sum(float(r.get("risk_usd") or 0) for r in rows)
    buys = sum(1 for r in rows if r.get("side") == "BUY")
    sells = len(rows) - buys
    avg_lot = sum(float(r.get("lot") or 0) for r in rows) / len(rows)
    win_rate = len(wins) / len(closed) * 100 if closed else 0

    lines = [
        f"📋 <b>TÓM TẮT NGÀY {label}</b>\n",
        f"📊 Tổng lệnh: <b>{len(rows)}</b> (🟢 BUY {buys} | 🔴 SELL {sells})",
        f"📦 Lot trung bình: <b>{avg_lot:.2f}</b>",
        f"💰 Tổng rủi ro triển khai: <b>${risk:,.2f}</b>",
        "",
    ]
    if closed:
        emoji = "✅" if pnl >= 0 else "❌"
        lines += [
            f"<b>Kết quả thực tế ({len(closed)}/{len(rows)} lệnh đã đóng):</b>",
            f"🏆 Thắng: <b>{len(wins)}</b> | 💀 Thua: <b>{len(losses)}</b>",
            f"📈 Win rate: <b>{win_rate:.0f}%</b>",
            f"{emoji} P&L thực tế: <b>${pnl:+,.2f}</b>",
        ]
    else:
        lines += [
            "<b>Kịch bản (chưa có lệnh đóng):</b>",
            f"✅ Nếu tất cả chạm TP: <b>+${risk * 2:,.2f}</b>",
            f"❌ Nếu tất cả chạm SL: <b>-${risk:,.2f}</b>",
        ]
    send_telegram("\n".join(lines))


def send_weekly_summary():
    cutoff = time.time() - 7 * 86400
    tz = pytz.timezone("America/New_York")
    trades = []
    for row in read_log_rows():
        try:
            dt = tz.localize(datetime.strptime(row["datetime_ny"], "%Y-%m-%d %H:%M:%S"))
            if dt.timestamp() >= cutoff:
                trades.append(row)
        except Exception:
            pass

    if not trades:
        send_telegram("📅 <b>TÓM TẮT TUẦN</b>\n\n😴 Không có lệnh nào trong 7 ngày qua.")
        return

    closed = [r for r in trades if r.get("outcome") in ("TP", "SL")]
    wins = [r for r in closed if r.get("outcome") == "TP"]
    losses = [r for r in closed if r.get("outcome") == "SL"]
    pnl = sum(float(r.get("pnl_usd") or 0) for r in closed)
    risk = sum(float(r.get("risk_usd") or 0) for r in trades)
    win_rate = len(wins) / len(closed) * 100 if closed else 0
    emoji = "✅" if pnl >= 0 else "❌"

    send_telegram(
        f"📅 <b>TÓM TẮT TUẦN (7 ngày qua)</b>\n\n"
        f"📊 Tổng lệnh: <b>{len(trades)}</b>\n"
        f"💰 Tổng rủi ro: <b>${risk:,.2f}</b>\n\n"
        f"<b>Kết quả ({len(closed)} lệnh đã đóng):</b>\n"
        f"🏆 Thắng: <b>{len(wins)}</b> | 💀 Thua: <b>{len(losses)}</b>\n"
        f"📈 Win rate: <b>{win_rate:.0f}%</b>\n"
        f"{emoji} P&L thực tế: <b>${pnl:+,.2f}</b>"
    )


class _Health(BaseHTTPRequestHandler):
    def _respond_ok(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", "2")
        self.end_headers()

    def do_GET(self):
        self._respond_ok()
        self.wfile.write(b"OK")

    def do_HEAD(self):
        self._respond_ok()

    def log_message(self, format, *args):
        pass


class TradeLockerTokenBot:
    def __init__(self):
        self.access_token = None
        self.refresh_token = REFRESH_TOKEN
        self.acc_id = TRADELOCKER_ACCOUNT_ID or None
        self.acc_num = TRADELOCKER_ACC_NUM or None
        self.last_auth_time = None
        self.last_summary_date = None
        self.last_nfp_alert_date = None
        self.paused = False

        self.risk_usd = RISK_USD
        self.daily_loss_limit = DAILY_LOSS_LIMIT
        self.max_trades_per_day = MAX_TRADES_PER_DAY

        self.open_positions = {}
        self.consecutive_wins = 0
        self.consecutive_losses = 0
        self.traded_killzones_today = set()
        self._last_kz_reset_date = None

        self.instruments = {}
        self.trade_route_ids = {}
        self.info_route_ids = {}

        ensure_log_file()
        self.authenticate_with_token()

    def auth_headers(self):
        headers = {"Authorization": f"Bearer {self.access_token}"}
        if self.acc_num:
            headers["accNum"] = str(self.acc_num)
        return headers

    def json_headers(self):
        headers = self.auth_headers()
        headers["Content-Type"] = "application/json"
        headers["accept"] = "application/json"
        return headers

    def authenticate_with_token(self):
        def apply_auth_data(data):
            access_token = data.get("accessToken") or data.get("access_token")
            refresh_token = data.get("refreshToken") or data.get("refresh_token")
            if not access_token:
                return False
            self.access_token = access_token
            if refresh_token:
                self.refresh_token = refresh_token
            self.last_auth_time = time.time()
            print(f"🔑 Access Token được làm mới lúc {datetime.now().strftime('%H:%M:%S')}")
            self.get_account_details()
            return True

        if self.refresh_token:
            try:
                res = requests.post(
                    f"{BASE_URL}/auth/jwt/refresh",
                    json={"refreshToken": self.refresh_token.strip()},
                    headers={"accept": "application/json", "Content-Type": "application/json"},
                    timeout=REQUEST_TIMEOUT,
                )
                if res.status_code in (200, 201) and apply_auth_data(res.json()):
                    return
                print(f"⚠️ Refresh token failed HTTP {res.status_code}: {res.text[:200]}")
            except Exception as e:
                print(f"⚠️ Lỗi refresh token: {e}")

        if not TRADELOCKER_EMAIL or not TRADELOCKER_PASSWORD or not TRADELOCKER_SERVER:
            print("❌ Thiếu TRADELOCKER_EMAIL / TRADELOCKER_PASSWORD / TRADELOCKER_SERVER.")
            send_telegram(
                "🚨 <b>LỖI XÁC THỰC BOT</b>\n\n"
                "Thiếu env TRADELOCKER_EMAIL / TRADELOCKER_PASSWORD / TRADELOCKER_SERVER."
            )
            return

        try:
            res = requests.post(
                f"{BASE_URL}/auth/jwt/token",
                json={
                    "email": TRADELOCKER_EMAIL,
                    "password": TRADELOCKER_PASSWORD,
                    "server": TRADELOCKER_SERVER,
                },
                headers={"accept": "application/json", "Content-Type": "application/json"},
                timeout=REQUEST_TIMEOUT,
            )
            if res.status_code in (200, 201) and apply_auth_data(res.json()):
                return

            print(f"❌ Auth failed HTTP {res.status_code}: {res.text[:300]}")
            send_telegram(
                f"🚨 <b>LỖI XÁC THỰC BOT</b>\n\n"
                f"Không thể login TradeLocker.\n"
                f"Mã lỗi: <b>{res.status_code}</b>\n"
                f"<code>{res.text[:300]}</code>"
            )
        except Exception as e:
            print(f"❌ Không thể kết nối xác thực: {e}")
            send_telegram(f"🚨 <b>LỖI KẾT NỐI XÁC THỰC</b>\n\n<code>{e}</code>")

    def needs_token_refresh(self):
        return self.last_auth_time is None or (time.time() - self.last_auth_time) >= ACCESS_TOKEN_TTL

    def get_account_details(self):
        accounts = self._fetch_accounts()
        if not accounts:
            print("⚠️ Không lấy được danh sách tài khoản.")
            return

        selected = None
        if self.acc_id:
            selected = next((a for a in accounts if str(self._account_id(a)) == str(self.acc_id)), None)
        if not selected and self.acc_num:
            selected = next((a for a in accounts if str(self._account_num(a)) == str(self.acc_num)), None)
        if not selected:
            selected = accounts[0]

        self.acc_id = str(self._account_id(selected) or self.acc_id or "")
        self.acc_num = str(self._account_num(selected) or self.acc_num or "")

        print(f"🔒 Kết nối tài khoản TradeLocker ID={self.acc_id}, accNum={self.acc_num}")
        self._fetch_trade_config()
        self._fetch_all_instruments()

    def _fetch_accounts(self):
        endpoints = [
            f"{BASE_URL}/auth/jwt/all-accounts",
            f"{BASE_URL}/auth/accounts",
        ]
        for url in endpoints:
            try:
                res = requests.get(url, headers={"Authorization": f"Bearer {self.access_token}"}, timeout=REQUEST_TIMEOUT)
                if res.status_code != 200:
                    continue
                data = res.json()
                accounts = data.get("accounts") or data.get("d", {}).get("accounts") or data
                if isinstance(accounts, list):
                    return accounts
            except Exception as e:
                print(f"⚠️ Lỗi lấy accounts từ {url}: {e}")
        return []

    def _account_id(self, acc):
        return acc.get("id") or acc.get("accountId") or acc.get("accId")

    def _account_num(self, acc):
        return acc.get("accNum") or acc.get("accountNumber") or acc.get("accountNum")

    def _fetch_trade_config(self):
        global ORDERS_HISTORY_PAGE_SIZE
        if not self.acc_id:
            return
        urls = [
            f"{BASE_URL}/trade/config?accId={self.acc_id}",
            f"{BASE_URL}/trade/config",
        ]
        for url in urls:
            try:
                res = requests.get(url, headers=self.auth_headers(), timeout=REQUEST_TIMEOUT)
                if res.status_code != 200:
                    continue
                cfg = res.json()
                max_count = (
                    cfg.get("config", {}).get("ordersHistory", {}).get("maxCount")
                    or cfg.get("ordersHistoryMaxCount")
                    or cfg.get("maxOrdersHistory")
                    or cfg.get("historyLimit")
                )
                if max_count:
                    ORDERS_HISTORY_PAGE_SIZE = int(max_count)
                print(f"📋 ordersHistory page size = {ORDERS_HISTORY_PAGE_SIZE}")
                return
            except Exception as e:
                print(f"⚠️ Lỗi fetch /trade/config: {e}")

    def _fetch_all_instruments(self):
        if not self.acc_id:
            return
        try:
            res = requests.get(
                f"{BASE_URL}/trade/accounts/{self.acc_id}/instruments",
                headers=self.auth_headers(),
                timeout=REQUEST_TIMEOUT,
            )
            if res.status_code != 200:
                print(f"⚠️ /instruments HTTP {res.status_code}: {res.text[:200]}")
                return

            data = res.json()
            instruments = data.get("instruments") or data.get("d", {}).get("instruments") or data
            if not isinstance(instruments, list):
                return

            for inst in instruments:
                names = {
                    str(inst.get("name", "")),
                    str(inst.get("symbol", "")),
                    str(inst.get("tradableInstrument", "")),
                }
                tradable_id = (
                    inst.get("tradableInstrumentId")
                    or inst.get("id")
                    or inst.get("instrumentId")
                )
                routes = inst.get("routes") or []
                trade_route = self._extract_route_id(routes, "TRADE")
                info_route = self._extract_route_id(routes, "INFO")

                for name in names:
                    if name:
                        self.instruments[name] = tradable_id
                        if trade_route:
                            self.trade_route_ids[name] = trade_route
                        if info_route:
                            self.info_route_ids[name] = info_route

            print(
                f"📋 Instrument IDs: {SYMBOL}={self.instruments.get(SYMBOL)}, "
                f"{SYMBOL_ES}={self.instruments.get(SYMBOL_ES)}"
            )
        except Exception as e:
            print(f"⚠️ Lỗi fetch instruments: {e}")

    def _extract_route_id(self, routes, route_type):
        if isinstance(routes, dict):
            routes = [routes]
        for route in routes:
            r_type = str(route.get("type") or route.get("routeType") or route.get("name") or "").upper()
            if route_type in r_type:
                return route.get("id") or route.get("routeId")
        return None

    def log_trade(self, side, entry_price, sl_price, tp_price, lot, order_id=""):
        ensure_log_file()
        with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=LOG_HEADERS)
            writer.writerow({
                "datetime_ny": ny_now().strftime("%Y-%m-%d %H:%M:%S"),
                "symbol": SYMBOL,
                "side": side.upper(),
                "lot": lot,
                "entry_price": round(entry_price, 2),
                "stop_loss": round(sl_price, 2),
                "take_profit": round(tp_price, 2),
                "risk_usd": self.risk_usd,
                "order_id": order_id,
                "outcome": "",
                "pnl_usd": "",
                "close_time": "",
            })
        print("   ➔ Đã lưu lệnh vào trade_log.csv")

    def update_trade_outcome(self, order_id: str, outcome: str, pnl_usd: float):
        rows = read_log_rows()
        updated = False
        close_time = ny_now().strftime("%Y-%m-%d %H:%M:%S")
        for row in rows:
            if row.get("order_id") == str(order_id) and row.get("outcome") == "":
                row["outcome"] = outcome
                row["pnl_usd"] = round(pnl_usd, 2)
                row["close_time"] = close_time
                updated = True
        if updated:
            with open(LOG_FILE, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=LOG_HEADERS)
                writer.writeheader()
                writer.writerows(rows)
            print(f"   ➔ Cập nhật lệnh {order_id}: {outcome} P&L=${pnl_usd:+.2f}")

    def get_open_positions(self):
        urls = [
            f"{BASE_URL}/trade/accounts/{self.acc_id}/positions",
            f"{BASE_URL}/trade/positions?accId={self.acc_id}",
        ]
        for url in urls:
            try:
                res = requests.get(url, headers=self.auth_headers(), timeout=REQUEST_TIMEOUT)
                if res.status_code != 200:
                    continue
                data = res.json()
                return data.get("positions") or data.get("d", {}).get("positions", []) or []
            except Exception as e:
                print(f"⚠️ Lỗi lấy vị thế mở: {e}")
        return []

    def get_closed_positions(self):
        urls = [
            f"{BASE_URL}/trade/accounts/{self.acc_id}/ordersHistory?limit={ORDERS_HISTORY_PAGE_SIZE}",
            f"{BASE_URL}/trade/ordersHistory?accId={self.acc_id}&limit={ORDERS_HISTORY_PAGE_SIZE}",
        ]
        for url in urls:
            try:
                res = requests.get(url, headers=self.auth_headers(), timeout=REQUEST_TIMEOUT)
                if res.status_code != 200:
                    continue
                data = res.json()
                return (
                    data.get("ordersHistory")
                    or data.get("d", {}).get("ordersHistory")
                    or data.get("filledOrders")
                    or []
                )
            except Exception as e:
                print(f"⚠️ Lỗi fetch ordersHistory: {e}")
        return []

    def _record_matches_order(self, record: dict, order_id: str):
        ids = [
            record.get("id"),
            record.get("orderId"),
            record.get("positionId"),
            record.get("originalOrderId"),
            record.get("closingOrderId"),
        ]
        return str(order_id) in {str(x) for x in ids if x is not None}

    def _live_position_matches_order(self, pos: dict, order_id: str):
        ids = [pos.get("id"), pos.get("orderId"), pos.get("positionId"), pos.get("openOrderId")]
        return str(order_id) in {str(x) for x in ids if x is not None}

    def _resolve_closed_pnl(self, order_id: str, pos: dict):
        for record in self.get_closed_positions():
            if not self._record_matches_order(record, order_id):
                continue
            status = str(record.get("status", "")).lower()
            if status in ("cancelled", "rejected", "expired"):
                break
            real_pnl = record.get("profit", record.get("pnl"))
            if real_pnl is not None:
                pnl_val = float(real_pnl)
                return ("TP" if pnl_val >= 0 else "SL"), round(pnl_val, 2)
            filled_price = record.get("filledPrice", record.get("closePrice"))
            if filled_price is not None:
                entry = pos["entry"]
                lot = pos["lot"]
                cp = float(filled_price)
                pnl_val = (cp - entry) * lot * NAS100_POINT_VALUE if pos["side"] == "buy" else (entry - cp) * lot * NAS100_POINT_VALUE
                return ("TP" if pnl_val >= 0 else "SL"), round(pnl_val, 2)

        risk = pos["risk_usd"]
        outcome = "TP"
        pnl = risk * 2.0
        print(f"   ⚠️ Không tìm thấy record lệnh {order_id} — dùng ước tính.")
        return outcome, round(pnl, 2)

    def poll_open_positions(self):
        if not self.open_positions:
            return
        live_positions = self.get_open_positions()
        for order_id, pos in list(self.open_positions.items()):
            if any(self._live_position_matches_order(p, order_id) for p in live_positions):
                continue

            outcome, pnl = self._resolve_closed_pnl(order_id, pos)
            self.update_trade_outcome(order_id, outcome, pnl)
            del self.open_positions[order_id]

            side = pos["side"]
            now_str = ny_now().strftime("%d/%m/%Y %H:%M:%S")
            if outcome == "TP":
                self.consecutive_wins += 1
                self.consecutive_losses = 0
                send_telegram(
                    f"✅ <b>[CHẠM TP] {side.upper()} {SYMBOL}</b>\n"
                    f"🕐 {now_str} (NY)\n"
                    f"💰 P&L thực tế: <b>${pnl:+,.2f}</b>"
                )
            else:
                self.consecutive_losses += 1
                self.consecutive_wins = 0
                send_telegram(
                    f"❌ <b>[CHẠM SL] {side.upper()} {SYMBOL}</b>\n"
                    f"🕐 {now_str} (NY)\n"
                    f"💸 P&L thực tế: <b>${pnl:+,.2f}</b>"
                )

            if self.consecutive_losses >= 3:
                self.paused = True
                send_telegram("🚨 <b>3 THUA LIÊN TIẾP!</b>\nBot đã tự tạm dừng. Gõ /resume để tiếp tục.")

    def _normalize_candles_newest_first(self, candles):
        if not candles:
            return []

        def ts(c):
            for key in ("time", "timestamp", "date", "t"):
                if key in c:
                    return c[key]
            return None

        if ts(candles[0]) is None or ts(candles[-1]) is None:
            return candles
        try:
            return sorted(candles, key=ts, reverse=True)
        except Exception:
            return candles

    def _candles_oldest_first(self, candles):
        if not candles:
            return []

        def ts(c):
            for key in ("time", "timestamp", "date", "t"):
                if key in c:
                    return c[key]
            return None

        if ts(candles[0]) is not None and ts(candles[-1]) is not None:
            try:
                return sorted(candles, key=ts)
            except Exception:
                pass
        return list(reversed(candles))

    def get_candles(self, symbol, resolution="1", count=50):
        if not self.acc_id or not self.access_token:
            return []

        urls = [
            f"{BASE_URL}/trade/candles?accId={self.acc_id}&symbol={symbol}&resolution={resolution}&count={count}",
        ]
        product_id = self.instruments.get(symbol)
        if product_id:
            urls.insert(
                0,
                f"{BASE_URL}/trade/candles?accId={self.acc_id}&productId={product_id}&resolution={resolution}&count={count}",
            )

        for url in urls:
            try:
                res = requests.get(url, headers=self.auth_headers(), timeout=REQUEST_TIMEOUT)
                if res.status_code != 200:
                    continue
                data = res.json()
                candles = data.get("candles") or data.get("d", {}).get("candles", [])
                return self._normalize_candles_newest_first(candles)
            except Exception as e:
                print(f"⚠️ Lỗi get_candles: {e}")
        print(f"⚠️ Không lấy được candles cho {symbol}")
        return []

    def check_killzone(self):
        current_time = ny_now().strftime("%H:%M")
        return "03:00" <= current_time <= "04:30" or "08:30" <= current_time <= "11:00"

    def current_killzone_name(self):
        current_time = ny_now().strftime("%H:%M")
        if "03:00" <= current_time <= "04:30":
            return "London"
        if "08:30" <= current_time <= "11:00":
            return "NewYork"
        return None

    def _reset_killzone_cooldown_if_new_day(self):
        today = ny_now().strftime("%Y-%m-%d")
        if self._last_kz_reset_date != today:
            self.traded_killzones_today = set()
            self._last_kz_reset_date = today

    def _ema(self, values, period):
        if not values:
            return 0
        if len(values) < period:
            return sum(values) / len(values)
        k = 2 / (period + 1)
        ema = sum(values[:period]) / period
        for v in values[period:]:
            ema = v * k + ema * (1 - k)
        return ema

    def _find_swing_points(self, candles, lookback=3):
        swings = []
        for i in range(lookback, len(candles) - lookback):
            c = candles[i]
            if all(c["high"] > candles[i - j]["high"] for j in range(1, lookback + 1)) and all(c["high"] > candles[i + j]["high"] for j in range(1, lookback + 1)):
                swings.append({"type": "high", "price": c["high"], "idx": i})
            if all(c["low"] < candles[i - j]["low"] for j in range(1, lookback + 1)) and all(c["low"] < candles[i + j]["low"] for j in range(1, lookback + 1)):
                swings.append({"type": "low", "price": c["low"], "idx": i})
        return swings

    def get_htf_bias(self):
        candles_15m = self.get_candles(SYMBOL, resolution="15", count=80)
        if not candles_15m or len(candles_15m) < 20:
            print("⚠️ Không đủ nến 15m để tính HTF bias.")
            return None

        candles_chron = self._candles_oldest_first(candles_15m)
        swings = self._find_swing_points(candles_chron, lookback=3)
        highs = [s for s in swings if s["type"] == "high"]
        lows = [s for s in swings if s["type"] == "low"]

        if len(highs) >= 2 and len(lows) >= 2:
            h1, h2 = highs[-2], highs[-1]
            l1, l2 = lows[-2], lows[-1]
            if h2["price"] > h1["price"] and l2["price"] > l1["price"]:
                print("   📈 HTF Bias: BULLISH")
                return "bull"
            if h2["price"] < h1["price"] and l2["price"] < l1["price"]:
                print("   📉 HTF Bias: BEARISH")
                return "bear"
            print("   ↔️ HTF Swing structure không rõ — dùng EMA.")

        closes = [c["close"] for c in candles_chron]
        ema20 = self._ema(closes, 20)
        ema50 = self._ema(closes, 50)
        if ema20 > ema50:
            return "bull"
        if ema20 < ema50:
            return "bear"
        return None

    def execute_trade(self, side, entry_price, sl_price):
        if side == "buy":
            distance = entry_price - sl_price
            tp_price = entry_price + distance * 2.0
        else:
            distance = sl_price - entry_price
            tp_price = entry_price - distance * 2.0

        if distance <= 0:
            print("⚠️ Bỏ qua tín hiệu: SL không hợp lệ.")
            return False

        calculated_lot = self.risk_usd / (distance * NAS100_POINT_VALUE)
        if calculated_lot < 0.05:
            print(f"⚠️ Bỏ qua tín hiệu: Lot {calculated_lot:.2f} < 0.05.")
            return False

        final_lot = max(0.05, round(calculated_lot, 2))
        instrument_id = self.instruments.get(SYMBOL)
        route_id = self.trade_route_ids.get(SYMBOL)

        if not instrument_id or not route_id:
            print(f"❌ Thiếu tradableInstrumentId/routeId cho {SYMBOL}. Huỷ phát lệnh.")
            send_telegram(f"❌ <b>KHÔNG TÌM THẤY INSTRUMENT/ROUTE</b>\n\nSymbol: <b>{SYMBOL}</b>")
            return False

        payload = {
            "qty": final_lot,
            "routeId": int(route_id),
            "side": side,
            "validity": "IOC",
            "type": "market",
            "price": 0,
            "tradableInstrumentId": int(instrument_id),
            "stopLoss": round(sl_price, 2),
            "stopLossType": "absolute",
            "takeProfit": round(tp_price, 2),
            "takeProfitType": "absolute",
        }

        urls = [
            f"{BASE_URL}/trade/accounts/{self.acc_id}/orders",
            f"{BASE_URL}/trade/orders",
        ]
        for url in urls:
            try:
                res = requests.post(url, json=payload, headers=self.json_headers(), timeout=REQUEST_TIMEOUT)
                if res.status_code not in (200, 201):
                    print(f"❌ Sàn từ chối lệnh tại {url}: HTTP {res.status_code}: {res.text[:300]}")
                    continue

                order_data = res.json()
                d = order_data.get("d", order_data)
                order_id = str(d.get("orderId", d.get("id", order_data.get("orderId", ""))))

                print(f"🚀 [VÀO LỆNH THÀNH CÔNG] {side.upper()} {SYMBOL}! Lot: {final_lot} ID: {order_id}")
                self.log_trade(side, entry_price, sl_price, tp_price, final_lot, order_id)

                if order_id:
                    self.open_positions[order_id] = {
                        "side": side,
                        "entry": entry_price,
                        "sl": sl_price,
                        "tp": tp_price,
                        "lot": final_lot,
                        "risk_usd": self.risk_usd,
                    }

                emoji = "🟢" if side == "buy" else "🔴"
                kz = self.current_killzone_name() or ""
                send_telegram(
                    f"{emoji} <b>[VÀO LỆNH] {side.upper()} {SYMBOL}</b> {kz}\n"
                    f"🕐 {ny_now().strftime('%d/%m/%Y %H:%M:%S')} (NY)\n"
                    f"📌 Entry : <b>{round(entry_price, 2)}</b>\n"
                    f"🛑 Stop Loss : <b>{round(sl_price, 2)}</b>\n"
                    f"🎯 Take Profit : <b>{round(tp_price, 2)}</b>\n"
                    f"📦 Khối lượng : <b>{final_lot} Lot</b>\n"
                    f"💰 Rủi ro : <b>${self.risk_usd}</b>"
                )
                return True
            except Exception as e:
                print(f"❌ Lỗi phát lệnh tại {url}: {e}")

        send_telegram(f"❌ <b>LỆNH BỊ TỪ CHỐI</b>\n\n{side.upper()} {SYMBOL}")
        return False

    def check_smt_divergence(self, nas_candles, es_candles):
        if not nas_candles or not es_candles:
            print(f"   ⚠️ [SMT] Không lấy được nến — NAS: {len(nas_candles) if nas_candles else 0} ES: {len(es_candles) if es_candles else 0}")
            return None, None
        if len(nas_candles) < 35 or len(es_candles) < 35:
            print(f"   ⚠️ [SMT] Không đủ nến — NAS: {len(nas_candles)} ES: {len(es_candles)}")
            return None, None

        nas_curr = nas_candles[0]
        es_curr = es_candles[0]
        nas_prev_low = min(c["low"] for c in nas_candles[1:31])
        nas_prev_high = max(c["high"] for c in nas_candles[1:31])
        es_prev_low = min(c["low"] for c in es_candles[1:31])
        es_prev_high = max(c["high"] for c in es_candles[1:31])
        mss_swing_high = max(c["high"] for c in nas_candles[1:4])
        mss_swing_low = min(c["low"] for c in nas_candles[1:4])

        nas_swept_low = nas_curr["low"] < nas_prev_low
        nas_swept_high = nas_curr["high"] > nas_prev_high
        es_held_low = es_curr["low"] >= es_prev_low - (es_prev_low * 0.001)
        es_held_high = es_curr["high"] <= es_prev_high + (es_prev_high * 0.001)
        mss_bull = nas_curr["close"] > mss_swing_high
        mss_bear = nas_curr["close"] < mss_swing_low

        print(
            f"   📊 NAS low={nas_curr['low']:.1f} prev_low={nas_prev_low:.1f} swept={'✅' if nas_swept_low else '❌'} | "
            f"ES held_low={'✅' if es_held_low else '❌'} | MSS_bull={'✅' if mss_bull else '❌'}"
        )
        print(
            f"   📊 NAS high={nas_curr['high']:.1f} prev_high={nas_prev_high:.1f} swept={'✅' if nas_swept_high else '❌'} | "
            f"ES held_high={'✅' if es_held_high else '❌'} | MSS_bear={'✅' if mss_bear else '❌'}"
        )

        if nas_swept_low and es_held_low and mss_bull:
            sl_price = nas_curr["low"] - (nas_curr["low"] * SL_BUFFER_PCT)
            print(f"   🟢 [BUY SIGNAL] SL={sl_price:.2f}")
            return "buy", round(sl_price, 2)

        if nas_swept_high and es_held_high and mss_bear:
            sl_price = nas_curr["high"] + (nas_curr["high"] * SL_BUFFER_PCT)
            print(f"   🔴 [SELL SIGNAL] SL={sl_price:.2f}")
            return "sell", round(sl_price, 2)

        return None, None

    def run_strategy(self):
        self._reset_killzone_cooldown_if_new_day()
        now_str = ny_now().strftime("%H:%M:%S")

        if self.paused:
            print(f"\r⏸️ [{now_str}] Bot tạm dừng.          ", end="")
            return

        allowed, reason = is_trading_allowed()
        if not allowed:
            print(f"\r🚫 [{now_str}] {reason}          ", end="")
            return

        today_risk = risk_deployed_today()
        if today_risk >= self.daily_loss_limit:
            self.paused = True
            send_telegram(
                f"🛡️ <b>ĐÃ ĐẠT GIỚI HẠN RỦI RO NGÀY</b>\n\n"
                f"Tổng rủi ro hôm nay: <b>${today_risk:,.2f}</b> / <b>${self.daily_loss_limit:,.2f}</b>\n"
                f"Bot đã tự tạm dừng. Gõ /resume để tiếp tục."
            )
            return

        trades_today = count_trades_today()
        if trades_today >= self.max_trades_per_day:
            print(f"\r🔢 [{now_str}] Đã đạt {trades_today}/{self.max_trades_per_day} lệnh/ngày.          ", end="")
            return

        if not self.check_killzone():
            print(f"\r⏳ [{now_str}] Ngoài Kill Zone — chờ London/NY.          ", end="")
            return

        kz = self.current_killzone_name()
        if kz and kz in self.traded_killzones_today:
            print(f"\r🔒 [{now_str}] Kill zone {kz} đã có lệnh hôm nay.          ", end="")
            return

        nas_candles = self.get_candles(SYMBOL, resolution="1", count=50)
        es_candles = self.get_candles(SYMBOL_ES, resolution="1", count=50)
        print(f"\n🔎 [{now_str}] Đang scan SMT [{kz}] — NAS={len(nas_candles)} ES={len(es_candles)}")

        signal, sl_price = self.check_smt_divergence(nas_candles, es_candles)
        if not signal:
            print("   ⏩ Không có tín hiệu.")
            return

        htf_bias = self.get_htf_bias()
        if htf_bias is not None:
            if signal == "buy" and htf_bias != "bull":
                print(f"   🚫 [HTF FILTER] BUY bị chặn — 15m bias: {htf_bias}")
                return
            if signal == "sell" and htf_bias != "bear":
                print(f"   🚫 [HTF FILTER] SELL bị chặn — 15m bias: {htf_bias}")
                return
        else:
            print("   ℹ️ HTF bias không xác định — tiếp tục.")

        entry_price = nas_candles[0]["close"]
        print(f"   ✅ [{kz}] {signal.upper()} Entry={entry_price:.2f} SL={sl_price:.2f} HTF={htf_bias or 'N/A'}")

        trade_ok = self.execute_trade(signal, entry_price, sl_price)
        if trade_ok and kz:
            self.traded_killzones_today.add(kz)
            time.sleep(60)


def poll_telegram_commands(bot_ref):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    last_update_id = None
    base_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
    print("📲 Telegram command listener đã khởi động.")

    while True:
        try:
            params = {"timeout": 30, "allowed_updates": ["message"]}
            if last_update_id is not None:
                params["offset"] = last_update_id + 1
            res = requests.get(f"{base_url}/getUpdates", params=params, timeout=40)
            if res.status_code != 200:
                time.sleep(5)
                continue

            for update in res.json().get("result", []):
                last_update_id = update["update_id"]
                msg = update.get("message", {})
                text = msg.get("text", "").strip().lower()
                chat_id = str(msg.get("chat", {}).get("id", ""))
                if chat_id != str(TELEGRAM_CHAT_ID):
                    continue

                if text.startswith("/status"):
                    elapsed = int(time.time() - bot_ref.last_auth_time) if bot_ref.last_auth_time else 0
                    mins, secs = divmod(elapsed, 60)
                    next_refresh = max(ACCESS_TOKEN_TTL - elapsed, 0)
                    nr_mins, nr_secs = divmod(next_refresh, 60)
                    kz_traded = ", ".join(bot_ref.traded_killzones_today) if bot_ref.traded_killzones_today else "Chưa có"
                    send_telegram(
                        f"📡 <b>TRẠNG THÁI BOT ICT PRO MAX</b>\n"
                        f"🕐 {ny_now().strftime('%d/%m/%Y %H:%M:%S')} (NY)\n\n"
                        f"{'⏸️ <b>ĐÃ TẠM DỪNG</b>' if bot_ref.paused else '▶️ <b>ĐANG CHẠY</b>'}\n"
                        f"{'🟢 <b>ĐANG TRONG KILL ZONE</b>' if bot_ref.check_killzone() else '🔵 Ngoài Kill Zone'}\n\n"
                        f"🔑 Token làm mới cách đây: <b>{mins}p {secs}s</b>\n"
                        f"⏳ Làm mới tiếp theo sau: <b>{nr_mins}p {nr_secs}s</b>\n"
                        f"📊 Lệnh hôm nay: <b>{count_trades_today()}/{bot_ref.max_trades_per_day}</b>\n"
                        f"🕐 Kill zone đã vào lệnh: <b>{kz_traded}</b>\n"
                        f"🛡️ Rủi ro hôm nay: <b>${risk_deployed_today():,.2f}</b> / <b>${bot_ref.daily_loss_limit:,.2f}</b>\n"
                        f"📈 P&L thực tế hôm nay: <b>${realized_pnl_today():+,.2f}</b>\n"
                        f"💎 Symbol: <b>{SYMBOL}</b> | Risk/lệnh: <b>${bot_ref.risk_usd}</b>"
                    )
                elif text == "/pause":
                    bot_ref.paused = True
                    send_telegram("⏸️ <b>ĐÃ TẠM DỪNG BOT</b>\n\nGõ /resume để tiếp tục.")
                elif text == "/resume":
                    bot_ref.paused = False
                    send_telegram("▶️ <b>BOT ĐÃ TIẾP TỤC HOẠT ĐỘNG</b>")
                elif text.startswith("/risk"):
                    try:
                        new_risk = float(text.split()[1])
                        if new_risk <= 0:
                            raise ValueError
                        old = bot_ref.risk_usd
                        bot_ref.risk_usd = new_risk
                        send_telegram(f"⚙️ <b>ĐÃ CẬP NHẬT RISK</b>\n\nCũ: <b>${old}</b> → Mới: <b>${new_risk}</b>")
                    except Exception:
                        send_telegram("❌ Cú pháp: <b>/risk &lt;số tiền&gt;</b>\nVí dụ: /risk 25")
                elif text.startswith("/limit"):
                    try:
                        new_limit = float(text.split()[1])
                        if new_limit <= 0:
                            raise ValueError
                        old = bot_ref.daily_loss_limit
                        bot_ref.daily_loss_limit = new_limit
                        send_telegram(f"🛡️ <b>ĐÃ CẬP NHẬT LIMIT</b>\n\nCũ: <b>${old:,.2f}</b> → Mới: <b>${new_limit:,.2f}</b>")
                    except Exception:
                        send_telegram("❌ Cú pháp: <b>/limit &lt;số tiền&gt;</b>\nVí dụ: /limit 75")
                elif text.startswith("/maxtr"):
                    try:
                        new_max = int(text.split()[1])
                        if new_max <= 0:
                            raise ValueError
                        old = bot_ref.max_trades_per_day
                        bot_ref.max_trades_per_day = new_max
                        send_telegram(f"🔢 <b>ĐÃ CẬP NHẬT MAX LỆNH</b>\n\nCũ: <b>{old}</b> → Mới: <b>{new_max}</b>")
                    except Exception:
                        send_telegram("❌ Cú pháp: <b>/maxtr &lt;số lệnh&gt;</b>\nVí dụ: /maxtr 3")
                elif text == "/token":
                    days = get_refresh_token_days_left()
                    if days is None:
                        send_telegram("ℹ️ Bot đang dùng login email/password hoặc không đọc được hạn refresh token.")
                    elif days <= 0:
                        send_telegram("🚨 <b>REFRESH TOKEN ĐÃ HẾT HẠN!</b>")
                    elif days <= 3:
                        send_telegram(f"⚠️ <b>REFRESH TOKEN SẮP HẾT HẠN!</b>\n\nCòn <b>{days} ngày</b>.")
                    else:
                        send_telegram(f"🔑 Refresh Token còn hiệu lực: <b>{days} ngày</b> ✅")
                elif text == "/today":
                    send_daily_summary()
                elif text == "/week":
                    send_weekly_summary()
                elif text == "/help":
                    send_telegram(
                        "🤖 <b>DANH SÁCH LỆNH BOT</b>\n\n"
                        "📡 /status — Xem trạng thái bot\n"
                        "📋 /today — Xem lệnh hôm nay\n"
                        "📅 /week — Xem tóm tắt 7 ngày\n"
                        "⏸️ /pause — Tạm dừng\n"
                        "▶️ /resume — Tiếp tục\n"
                        "⚙️ /risk &lt;số&gt; — Đổi risk/lệnh\n"
                        "🛡️ /limit &lt;số&gt; — Đổi giới hạn rủi ro ngày\n"
                        "🔢 /maxtr &lt;số&gt; — Đổi max lệnh/ngày\n"
                        "🔑 /token — Kiểm tra token\n"
                        "❓ /help — Danh sách lệnh"
                    )
        except Exception as e:
            print(f"⚠️ Telegram polling lỗi: {e}")
            time.sleep(10)


if __name__ == "__main__":
    bot = TradeLockerTokenBot()
    if bot.access_token:
        print("\n🤖 Bot ICT Pro Max xác thực Token thành công!")
        print(f"📡 Bot đang quét {SYMBOL} trên {TL_ENV.upper()}...")

        threading.Thread(target=poll_telegram_commands, args=(bot,), daemon=True).start()

        port = int(os.environ.get("PORT", "8080"))
        try:
            health_srv = HTTPServer(("0.0.0.0", port), _Health)
            threading.Thread(target=health_srv.serve_forever, daemon=True).start()
            print(f"🌐 Health server đang chạy trên cổng {port}")
        except OSError:
            print(f"⚠️ Cổng {port} đang bận — bỏ qua health server.")

        last_expiry_check_date = None
        last_position_poll_time = 0

        while True:
            try:
                now = ny_now()
                today = now.strftime("%Y-%m-%d")

                if bot.needs_token_refresh():
                    print("\n🔄 Đang làm mới Access Token...")
                    bot.authenticate_with_token()

                if now.hour == DAILY_SUMMARY_HOUR and bot.last_summary_date != today:
                    bot.last_summary_date = today
                    send_daily_summary()

                if now.hour == 8 and is_nfp_friday() and bot.last_nfp_alert_date != today:
                    bot.last_nfp_alert_date = today
                    send_telegram("📰 <b>HÔM NAY LÀ THỨ SÁU NFP!</b>\n\nBot sẽ <b>không trade</b> hôm nay.")

                if now.hour == 9 and last_expiry_check_date != today:
                    last_expiry_check_date = today
                    days = get_refresh_token_days_left()
                    if days is not None and days <= 3:
                        send_telegram(f"⚠️ <b>CẢNH BÁO HẠN TOKEN</b>\n\nRefresh Token còn <b>{days} ngày</b>.")

                if time.time() - last_position_poll_time >= 30:
                    bot.poll_open_positions()
                    last_position_poll_time = time.time()

                bot.run_strategy()
            except Exception as e:
                print(f"\n⚠️ Lỗi vòng lặp, thử lại sau 10s: {e}")
                time.sleep(10)
                bot.authenticate_with_token()

            time.sleep(2)
    else:
        print("❌ Bot chưa xác thực được token. Kiểm tra TRADELOCKER_EMAIL/PASSWORD/SERVER.")
