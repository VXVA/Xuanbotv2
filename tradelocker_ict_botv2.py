import base64
import csv
import json
import os
import threading
import time
import requests
from datetime import datetime, timezone
import pytz
from http.server import HTTPServer, BaseHTTPRequestHandler
from dotenv import load_dotenv

load_dotenv()

# ============================================================
# CẤU HÌNH HỆ THỐNG & BIẾN MÔI TRƯỜNG
# ============================================================
ACCESS_TOKEN_TTL = 25 * 60

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID")
REFRESH_TOKEN      = os.environ.get("REFRESH_TOKEN")

LOG_FILE = os.path.join(os.path.dirname(__file__), "trade_log.csv")
LOG_HEADERS = ["datetime_ny", "symbol", "side", "lot", "entry_price", "stop_loss", "take_profit", "risk_usd", "order_id", "outcome", "pnl_usd", "close_time"]
DAILY_SUMMARY_HOUR = 23

MAX_TRADES_PER_DAY = 3
DAILY_LOSS_LIMIT   = 75.0

ACCOUNT_ID = "vietanh9a2k5@gmail.com"
SERVER = "BLUEG"
RISK_USD = 25.0
SYMBOL = "NAS100"
SYMBOL_ES = "SPX500"

# ============================================================
# [FIX D] Point value cho NAS100 — 1 lot = $1/điểm tại hầu hết broker
# Kiểm tra lại với TradeLocker nếu cần điều chỉnh
# ============================================================
NAS100_POINT_VALUE = 1.0   # USD per point per 1 lot

# Buffer SL: 0.03% của giá để tránh bị sweep ngay khi vào lệnh
SL_BUFFER_PCT = 0.0003

# ============================================================
# BASE URL — tự động chọn demo/live theo biến môi trường
# Set TRADELOCKER_ENV=live trong Render để dùng live account
# Mặc định là demo (an toàn hơn)
# ============================================================
_TL_ENV = os.environ.get("TRADELOCKER_ENV", "demo").strip().lower()
if _TL_ENV == "live":
    BASE_URL = "https://live.tradelocker.com/backend-api"
else:
    BASE_URL = "https://demo.tradelocker.com/backend-api"

# Giới hạn page size cho ordersHistory — được cập nhật từ /trade/config khi khởi động
ORDERS_HISTORY_PAGE_SIZE = 50   # default an toàn trước khi fetch config

# ============================================================
# [NEW 1] SESSION FILTER — Ngày lễ Mỹ + thứ Sáu NFP
# NFP = thứ Sáu đầu tiên mỗi tháng
# ============================================================
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


def is_nfp_friday() -> bool:
    """Trả về True nếu hôm nay là thứ Sáu NFP (thứ Sáu đầu tiên của tháng, giờ NY)."""
    tz_ny = pytz.timezone("America/New_York")
    now_ny = datetime.now(tz_ny)
    if now_ny.weekday() != 4:   # 4 = Friday
        return False
    # Thứ Sáu đầu tiên của tháng khi ngày <= 7
    return now_ny.day <= 7


def is_trading_allowed() -> tuple[bool, str]:
    """
    Kiểm tra xem hôm nay có được phép trade không.
    Trả về (allowed: bool, reason: str).
    """
    tz_ny = pytz.timezone("America/New_York")
    now_ny = datetime.now(tz_ny)
    today_str = now_ny.strftime("%Y-%m-%d")

    # Cuối tuần
    if now_ny.weekday() >= 5:
        return False, "Cuối tuần — thị trường đóng cửa."

    # Ngày lễ Mỹ
    if today_str in US_HOLIDAYS:
        return False, f"Ngày lễ Mỹ ({today_str}) — thị trường đóng cửa."

    # Thứ Sáu NFP
    if is_nfp_friday():
        return False, "Thứ Sáu NFP — biến động bất thường, bot tự bỏ qua phiên."

    return True, ""


def send_telegram(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
        }, timeout=10)
    except Exception as e:
        print(f"⚠️ Không gửi được Telegram: {e}")


def get_refresh_token_days_left() -> int | None:
    try:
        payload_b64 = REFRESH_TOKEN.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        exp_ts = payload.get("exp")
        if not exp_ts:
            return None
        days_left = (exp_ts - datetime.now(timezone.utc).timestamp()) / 86400
        return int(days_left)
    except Exception:
        return None


def send_daily_summary():
    tz_ny = pytz.timezone("America/New_York")
    today_str = datetime.now(tz_ny).strftime("%Y-%m-%d")
    today_label = datetime.now(tz_ny).strftime("%d/%m/%Y")

    trades_today = []
    if os.path.isfile(LOG_FILE):
        with open(LOG_FILE, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("datetime_ny", "").startswith(today_str):
                    try:
                        trades_today.append({
                            "side":        row["side"],
                            "lot":         float(row["lot"]),
                            "entry_price": float(row["entry_price"]),
                            "stop_loss":   float(row["stop_loss"]),
                            "take_profit": float(row["take_profit"]),
                            "risk_usd":    float(row["risk_usd"]),
                            "outcome":     row.get("outcome", ""),
                            "pnl_usd":     float(row["pnl_usd"]) if row.get("pnl_usd") else None,
                        })
                    except (ValueError, KeyError):
                        continue

    if not trades_today:
        send_telegram(
            f"📋 <b>TÓM TẮT NGÀY {today_label}</b>\n\n"
            f"😴 Hôm nay không có lệnh nào được thực thi.\n"
            f"Bot đã trực chiến suốt phiên Mỹ nhưng không tìm thấy kèo đủ điều kiện."
        )
        return

    total      = len(trades_today)
    buys       = sum(1 for t in trades_today if t["side"] == "BUY")
    sells      = total - buys
    total_risk = sum(t["risk_usd"] for t in trades_today)
    avg_lot    = sum(t["lot"] for t in trades_today) / total

    closed     = [t for t in trades_today if t["outcome"] in ("TP", "SL")]
    wins       = [t for t in closed if t["outcome"] == "TP"]
    losses     = [t for t in closed if t["outcome"] == "SL"]
    real_pnl   = sum(t["pnl_usd"] for t in closed if t["pnl_usd"] is not None)
    win_rate   = (len(wins) / len(closed) * 100) if closed else None

    lines = [f"📋 <b>TÓM TẮT NGÀY {today_label}</b>\n"]
    lines.append(f"📊 Tổng lệnh: <b>{total}</b>  (🟢 BUY {buys}  |  🔴 SELL {sells})")
    lines.append(f"📦 Lot trung bình: <b>{avg_lot:.2f}</b>")
    lines.append(f"💰 Tổng rủi ro triển khai: <b>${total_risk:,.2f}</b>")
    lines.append("")

    if closed:
        pnl_emoji = "✅" if real_pnl >= 0 else "❌"
        lines.append(f"<b>Kết quả thực tế ({len(closed)}/{total} lệnh đã đóng):</b>")
        lines.append(f"  🏆 Thắng: <b>{len(wins)}</b>  |  💀 Thua: <b>{len(losses)}</b>")
        lines.append(f"  📈 Win rate: <b>{win_rate:.0f}%</b>")
        lines.append(f"  {pnl_emoji} P&L thực tế: <b>${real_pnl:+,.2f}</b>")
    else:
        max_profit = total_risk * 2.0
        lines.append(f"<b>Kịch bản (chưa có lệnh đóng):</b>")
        lines.append(f"  ✅ Nếu tất cả chạm TP: <b>+${max_profit:,.2f}</b>")
        lines.append(f"  ❌ Nếu tất cả chạm SL: <b>-${total_risk:,.2f}</b>")

    lines.append("")
    lines.append(f"🤖 Bot tiếp tục chạy và sẵn sàng cho phiên tiếp theo.")

    send_telegram("\n".join(lines))
    print(f"📨 Đã gửi tóm tắt ngày {today_label} lên Telegram.")


def send_weekly_summary():
    tz_ny = pytz.timezone("America/New_York")
    now_ny = datetime.now(tz_ny)

    trades = []
    if os.path.isfile(LOG_FILE):
        cutoff = time.time() - 7 * 86400
        with open(LOG_FILE, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    dt = datetime.strptime(row["datetime_ny"], "%Y-%m-%d %H:%M:%S")
                    dt = pytz.timezone("America/New_York").localize(dt)
                    if dt.timestamp() >= cutoff:
                        trades.append({
                            "side":     row["side"],
                            "risk_usd": float(row["risk_usd"]),
                            "outcome":  row.get("outcome", ""),
                            "pnl_usd":  float(row["pnl_usd"]) if row.get("pnl_usd") else None,
                        })
                except (ValueError, KeyError):
                    continue

    if not trades:
        send_telegram(f"📅 <b>TÓM TẮT TUẦN</b>\n\n😴 Không có lệnh nào trong 7 ngày qua.")
        return

    total      = len(trades)
    closed     = [t for t in trades if t["outcome"] in ("TP", "SL")]
    wins       = [t for t in closed if t["outcome"] == "TP"]
    losses     = [t for t in closed if t["outcome"] == "SL"]
    real_pnl   = sum(t["pnl_usd"] for t in closed if t["pnl_usd"] is not None)
    total_risk = sum(t["risk_usd"] for t in trades)
    win_rate   = (len(wins) / len(closed) * 100) if closed else 0
    pnl_emoji  = "✅" if real_pnl >= 0 else "❌"

    send_telegram(
        f"📅 <b>TÓM TẮT TUẦN (7 ngày qua)</b>\n\n"
        f"📊 Tổng lệnh: <b>{total}</b>\n"
        f"💰 Tổng rủi ro: <b>${total_risk:,.2f}</b>\n\n"
        f"<b>Kết quả ({len(closed)} lệnh đã đóng):</b>\n"
        f"  🏆 Thắng: <b>{len(wins)}</b>  |  💀 Thua: <b>{len(losses)}</b>\n"
        f"  📈 Win rate: <b>{win_rate:.0f}%</b>\n"
        f"  {pnl_emoji} P&L thực tế: <b>${real_pnl:+,.2f}</b>"
    )


def count_trades_today():
    if not os.path.isfile(LOG_FILE):
        return 0
    tz_ny = pytz.timezone("America/New_York")
    today_str = datetime.now(tz_ny).strftime("%Y-%m-%d")
    count = 0
    try:
        with open(LOG_FILE, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("datetime_ny", "").startswith(today_str):
                    count += 1
    except:
        pass
    return count


def risk_deployed_today():
    if not os.path.isfile(LOG_FILE):
        return 0.0
    tz_ny = pytz.timezone("America/New_York")
    today_str = datetime.now(tz_ny).strftime("%Y-%m-%d")
    total = 0.0
    try:
        with open(LOG_FILE, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("datetime_ny", "").startswith(today_str):
                    try:
                        total += float(row["risk_usd"])
                    except (ValueError, KeyError):
                        pass
    except:
        pass
    return total


def poll_telegram_commands(bot_ref):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    last_update_id = None
    base_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
    print("📲 Telegram command listener đã khởi động (gõ /status để kiểm tra).")

    while True:
        try:
            params = {"timeout": 30, "allowed_updates": ["message"]}
            if last_update_id is not None:
                params["offset"] = last_update_id + 1

            resp = requests.get(f"{base_url}/getUpdates", params=params, timeout=40)
            if resp.status_code != 200:
                time.sleep(5)
                continue

            for update in resp.json().get("result", []):
                last_update_id = update["update_id"]
                msg = update.get("message", {})
                text = msg.get("text", "").strip().lower()
                chat_id = str(msg.get("chat", {}).get("id", ""))

                if chat_id != str(TELEGRAM_CHAT_ID):
                    continue

                if text in ("/status", "/status@" + bot_ref.acc_id if bot_ref.acc_id else "/status"):
                    tz_ny = pytz.timezone("America/New_York")
                    now_ny = datetime.now(tz_ny)
                    in_killzone = bot_ref.check_killzone()
                    trades_today = count_trades_today()

                    if bot_ref.last_auth_time:
                        elapsed = int(time.time() - bot_ref.last_auth_time)
                        mins, secs = divmod(elapsed, 60)
                        next_refresh = ACCESS_TOKEN_TTL - elapsed
                        nr_mins, nr_secs = divmod(max(next_refresh, 0), 60)
                        token_line = (
                            f"🔑 Token làm mới cách đây: <b>{mins}p {secs}s</b>\n"
                            f"⏳ Làm mới tiếp theo sau: <b>{nr_mins}p {nr_secs}s</b>"
                        )
                    else:
                        token_line = "🔑 Token: <b>chưa xác thực</b>"

                    zone_status = (
                        "🟢 <b>ĐANG TRONG KILL ZONE</b> — Bot đang rình kèo!"
                        if in_killzone else
                        "🔵 Ngoài Kill Zone — Bot đang túc trực chờ phiên Mỹ (09:30–10:30 NY)."
                    )
                    trade_status = (
                        "⏸️ <b>ĐÃ TẠM DỪNG</b> — Gõ /resume để tiếp tục."
                        if bot_ref.paused else
                        "▶️ <b>ĐANG CHẠY</b> — Gõ /pause để tạm dừng."
                    )

                    # [FIX B] Hiển thị cooldown status theo kill zone
                    kz_traded = ", ".join(bot_ref.traded_killzones_today) if bot_ref.traded_killzones_today else "Chưa có"
                    cooldown_line = f"🕐 Kill zone đã vào lệnh hôm nay: <b>{kz_traded}</b>"

                    send_telegram(
                        f"📡 <b>TRẠNG THÁI BOT ICT PRO MAX</b>\n"
                        f"🕐 {now_ny.strftime('%d/%m/%Y %H:%M:%S')} (NY)\n\n"
                        f"{trade_status}\n"
                        f"{zone_status}\n\n"
                        f"{token_line}\n"
                        f"📊 Lệnh hôm nay: <b>{trades_today}/{bot_ref.max_trades_per_day}</b>\n"
                        f"{cooldown_line}\n"
                        f"🛡️ Rủi ro hôm nay: <b>${risk_deployed_today():,.2f}</b> / giới hạn <b>${bot_ref.daily_loss_limit:,.2f}</b>\n"
                        f"💎 Symbol: <b>{SYMBOL}</b>  |  Risk/lệnh: <b>${bot_ref.risk_usd}</b>"
                    )

                elif text == "/pause":
                    if bot_ref.paused:
                        send_telegram("⏸️ Bot đã được tạm dừng trước đó rồi.\nGõ /resume để tiếp tục giao dịch.")
                    else:
                        bot_ref.paused = True
                        trades_today = count_trades_today()
                        print("⏸️  Bot đã được TẠM DỪNG qua Telegram.")
                        send_telegram(
                            f"⏸️ <b>ĐÃ TẠM DỪNG BOT</b>\n\n"
                            f"Bot sẽ không vào lệnh mới cho đến khi bạn gõ /resume.\n"
                            f"📊 Lệnh hôm nay trước khi dừng: <b>{trades_today}</b>\n\n"
                            f"Gõ /resume để tiếp tục giao dịch."
                        )

                elif text == "/resume":
                    if not bot_ref.paused:
                        send_telegram("▶️ Bot đang chạy bình thường rồi.\nGõ /pause để tạm dừng.")
                    else:
                        bot_ref.paused = False
                        print("▶️  Bot đã được TIẾP TỤC qua Telegram.")
                        send_telegram(
                            f"▶️ <b>BOT ĐÃ TIẾP TỤC HOẠT ĐỘNG</b>\n\n"
                            f"Bot sẽ tiếp tục quét kèo và vào lệnh bình thường.\n"
                            f"💎 Symbol: <b>{SYMBOL}</b>  |  Risk/lệnh: <b>${bot_ref.risk_usd}</b>"
                        )

                elif text.startswith("/risk"):
                    parts = text.split()
                    if len(parts) != 2:
                        send_telegram(
                            "⚙️ Cú pháp: <b>/risk &lt;số tiền&gt;</b>\n"
                            "Ví dụ: /risk 15  hoặc  /risk 50\n"
                            f"Risk hiện tại: <b>${bot_ref.risk_usd}</b>"
                        )
                    else:
                        try:
                            new_risk = float(parts[1])
                            if new_risk <= 0:
                                raise ValueError
                            old_risk = bot_ref.risk_usd
                            bot_ref.risk_usd = new_risk
                            print(f"⚙️  Risk thay đổi từ ${old_risk} → ${new_risk} qua Telegram.")
                            send_telegram(
                                f"⚙️ <b>ĐÃ CẬP NHẬT RISK</b>\n\n"
                                f"Risk cũ  : <b>${old_risk}</b>\n"
                                f"Risk mới : <b>${new_risk}</b>\n\n"
                                f"Lệnh tiếp theo sẽ tự động tính lại Lot theo mức risk mới.\n"
                                f"💎 Symbol: <b>{SYMBOL}</b>"
                            )
                        except (ValueError, IndexError):
                            send_telegram("❌ Giá trị không hợp lệ. Vui lòng nhập số dương.\nVí dụ: /risk 25")

                elif text == "/help":
                    send_telegram(
                        "🤖 <b>DANH SÁCH LỆNH BOT ICT PRO MAX</b>\n\n"
                        "📡 /status — Xem trạng thái bot, kill zone, token, risk\n"
                        "📋 /today — Xem lệnh + P&L thực tế hôm nay\n"
                        "📅 /week — Xem tóm tắt kết quả 7 ngày qua\n"
                        "⏸️ /pause — Tạm dừng vào lệnh mới\n"
                        "▶️ /resume — Tiếp tục giao dịch bình thường\n"
                        "⚙️ /risk &lt;số&gt; — Đổi risk mỗi lệnh (vd: /risk 15)\n"
                        "🛡️ /limit &lt;số&gt; — Đặt giới hạn rủi ro ngày (vd: /limit 75)\n"
                        "🔢 /maxtr &lt;số&gt; — Đặt max lệnh/ngày (vd: /maxtr 3)\n"
                        "🔑 /token — Kiểm tra hạn Refresh Token\n"
                        "❓ /help — Hiển thị danh sách lệnh này"
                    )

                elif text == "/week":
                    send_weekly_summary()

                elif text.startswith("/limit"):
                    parts = text.split()
                    if len(parts) != 2:
                        send_telegram(
                            f"🛡️ Cú pháp: <b>/limit &lt;số tiền&gt;</b>\n"
                            f"Ví dụ: /limit 75\n"
                            f"Giới hạn hiện tại: <b>${bot_ref.daily_loss_limit:,.2f}</b>"
                        )
                    else:
                        try:
                            new_limit = float(parts[1])
                            if new_limit <= 0:
                                raise ValueError
                            old_limit = bot_ref.daily_loss_limit
                            bot_ref.daily_loss_limit = new_limit
                            send_telegram(
                                f"🛡️ <b>ĐÃ CẬP NHẬT GIỚI HẠN RỦI RO NGÀY</b>\n\n"
                                f"Cũ: <b>${old_limit:,.2f}</b>  →  Mới: <b>${new_limit:,.2f}</b>\n"
                                f"Bot sẽ tự tạm dừng khi tổng rủi ro ngày vượt mức này."
                            )
                        except (ValueError, IndexError):
                            send_telegram("❌ Giá trị không hợp lệ. Ví dụ: /limit 75")

                elif text.startswith("/maxtr"):
                    parts = text.split()
                    if len(parts) != 2:
                        send_telegram(
                            f"🔢 Cú pháp: <b>/maxtr &lt;số lệnh&gt;</b>\n"
                            f"Ví dụ: /maxtr 3\n"
                            f"Giới hạn hiện tại: <b>{bot_ref.max_trades_per_day} lệnh/ngày</b>"
                        )
                    else:
                        try:
                            new_max = int(parts[1])
                            if new_max <= 0:
                                raise ValueError
                            old_max = bot_ref.max_trades_per_day
                            bot_ref.max_trades_per_day = new_max
                            send_telegram(
                                f"🔢 <b>ĐÃ CẬP NHẬT MAX LỆNH/NGÀY</b>\n\n"
                                f"Cũ: <b>{old_max}</b>  →  Mới: <b>{new_max}</b>\n"
                                f"Bot sẽ dừng vào lệnh sau khi đạt {new_max} lệnh hôm nay."
                            )
                        except (ValueError, IndexError):
                            send_telegram("❌ Giá trị không hợp lệ. Ví dụ: /maxtr 3")

                elif text == "/token":
                    days_left = get_refresh_token_days_left()
                    if days_left is None:
                        send_telegram("❌ Không thể đọc thông tin Refresh Token.")
                    elif days_left <= 0:
                        send_telegram(
                            "🚨 <b>REFRESH TOKEN ĐÃ HẾT HẠN!</b>\n\n"
                            "Bot sẽ không thể xác thực được nữa.\n\n"
                            "<b>Cách lấy token mới:</b>\n"
                            "1. Mở Chrome → vào <b>tradelocker.com</b> và đăng nhập\n"
                            "2. Nhấn <b>F12</b> → chọn tab <b>Application</b>\n"
                            "3. Bên trái chọn <b>Local Storage</b> → click vào tradelocker.com\n"
                            "4. Tìm key <b>refresh_token</b> → copy giá trị\n"
                            "5. Vào Render → <b>Environment</b> → cập nhật <b>REFRESH_TOKEN</b> → Save"
                        )
                    elif days_left <= 3:
                        send_telegram(
                            f"⚠️ <b>REFRESH TOKEN SẮP HẾT HẠN!</b>\n\n"
                            f"Còn <b>{days_left} ngày</b> — cần cập nhật sớm!\n\n"
                            "<b>Cách lấy token mới:</b>\n"
                            "1. Mở Chrome → vào <b>tradelocker.com</b> và đăng nhập\n"
                            "2. Nhấn <b>F12</b> → chọn tab <b>Application</b>\n"
                            "3. Bên trái chọn <b>Local Storage</b> → click vào tradelocker.com\n"
                            "4. Tìm key <b>refresh_token</b> → copy giá trị\n"
                            "5. Vào Render → <b>Environment</b> → cập nhật <b>REFRESH_TOKEN</b> → Save"
                        )
                    else:
                        send_telegram(f"🔑 Refresh Token còn hiệu lực: <b>{days_left} ngày</b> ✅")

                elif text == "/today":
                    tz_ny = pytz.timezone("America/New_York")
                    today_str = datetime.now(tz_ny).strftime("%Y-%m-%d")
                    today_label = datetime.now(tz_ny).strftime("%d/%m/%Y")
                    trades_today = []
                    if os.path.isfile(LOG_FILE):
                        with open(LOG_FILE, newline="") as f:
                            reader = csv.DictReader(f)
                            for row in reader:
                                if row.get("datetime_ny", "").startswith(today_str):
                                    trades_today.append(row)

                    if not trades_today:
                        send_telegram(f"📋 <b>LỆNH HÔM NAY — {today_label}</b>\n\n😴 Chưa có lệnh nào được thực thi hôm nay.")
                    else:
                        lines = [f"📋 <b>LỆNH HÔM NAY — {today_label}</b> ({len(trades_today)} lệnh)\n"]
                        total_risk = 0.0
                        real_pnl   = 0.0
                        for i, t in enumerate(trades_today, 1):
                            side    = t["side"]
                            emoji   = "🟢" if side == "BUY" else "🔴"
                            time_str = t["datetime_ny"][11:16]
                            risk    = float(t["risk_usd"])
                            total_risk += risk
                            outcome = t.get("outcome", "")
                            pnl_str = ""
                            if outcome == "TP":
                                pnl = float(t["pnl_usd"]) if t.get("pnl_usd") else 0
                                real_pnl += pnl
                                pnl_str = f"  ✅ TP  <b>+${pnl:,.2f}</b>"
                            elif outcome == "SL":
                                pnl = float(t["pnl_usd"]) if t.get("pnl_usd") else 0
                                real_pnl += pnl
                                pnl_str = f"  ❌ SL  <b>${pnl:,.2f}</b>"
                            else:
                                pnl_str = "  ⏳ Đang mở"
                            lines.append(
                                f"{emoji} <b>#{i} {side}</b>  {time_str} NY\n"
                                f"   Entry <b>{t['entry_price']}</b>  SL <b>{t['stop_loss']}</b>  TP <b>{t['take_profit']}</b>  Lot <b>{t['lot']}</b>\n"
                                f"  {pnl_str}"
                            )
                        lines.append(f"\n💰 Tổng rủi ro: <b>${total_risk:,.2f}</b>")
                        closed_today = [t for t in trades_today if t.get("outcome") in ("TP", "SL")]
                        if closed_today:
                            pnl_emoji = "✅" if real_pnl >= 0 else "❌"
                            lines.append(f"{pnl_emoji} P&L thực tế: <b>${real_pnl:+,.2f}</b>")
                        send_telegram("\n".join(lines))

        except Exception as e:
            print(f"⚠️ Telegram polling lỗi: {e}")
            time.sleep(10)


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
        # UptimeRobot và nhiều monitor khác dùng HEAD thay vì GET
        self._respond_ok()

    def log_message(self, format, *args):
        pass


class TradeLockerTokenBot:
    def __init__(self):
        self.access_token           = None
        self.acc_id                 = None
        self.last_auth_time         = None
        self.last_summary_date      = None
        self.paused                 = False
        self.risk_usd               = RISK_USD
        self.daily_loss_limit       = DAILY_LOSS_LIMIT
        self.max_trades_per_day     = MAX_TRADES_PER_DAY
        self.open_positions         = {}
        self.consecutive_wins       = 0
        self.consecutive_losses     = 0

        # [FIX B] Cooldown per kill zone: lưu tập hợp kill zone đã vào lệnh hôm nay
        # Reset mỗi ngày NY trong run loop chính
        self.traded_killzones_today = set()   # {"London", "NewYork"}
        self._last_kz_reset_date    = None

        self.authenticate_with_token()

    def _reset_killzone_cooldown_if_new_day(self):
        """Reset danh sách kill zone đã vào lệnh khi sang ngày mới (giờ NY)"""
        tz_ny = pytz.timezone("America/New_York")
        today_str = datetime.now(tz_ny).strftime("%Y-%m-%d")
        if self._last_kz_reset_date != today_str:
            self.traded_killzones_today = set()
            self._last_kz_reset_date = today_str

    def authenticate_with_token(self):
        url = "https://auth.tradelocker.com/realms/tradelocker/protocol/openid-connect/token"

        if not REFRESH_TOKEN:
            print("❌ Không tìm thấy mã REFRESH_TOKEN trong hệ thống.")
            return

        payload = {
            "grant_type": "refresh_token",
            "client_id": "frontend-web-live",
            "refresh_token": REFRESH_TOKEN.strip(),
        }
        headers = {"Content-Type": "application/x-www-form-urlencoded"}

        try:
            response = requests.post(url, data=payload, headers=headers)
            if response.status_code == 200:
                data = response.json()
                self.access_token = data.get("access_token")
                self.last_auth_time = time.time()
                print(f"🔑 Access Token được làm mới lúc {datetime.now().strftime('%H:%M:%S')}")
                self.get_account_details()
            else:
                msg = f"❌ Mã Refresh Token hết hạn hoặc sai cấu hình. Mã lỗi: {response.status_code}"
                print(msg)
                send_telegram(
                    f"🚨 <b>LỖI XÁC THỰC BOT!</b>\n\n"
                    f"Không thể làm mới Access Token.\n"
                    f"Mã lỗi: <b>{response.status_code}</b>\n\n"
                    f"Nếu lỗi 400: Refresh Token đã hết hạn — cần cập nhật token mới trong Render."
                )
        except Exception as e:
            print(f"❌ Không thể kết nối đến tổng đài xác thực: {e}")
            send_telegram(
                f"🚨 <b>LỖI KẾT NỐI XÁC THỰC!</b>\n\n"
                f"Bot không thể kết nối đến TradeLocker để làm mới token.\n"
                f"Lỗi: <code>{e}</code>\n\n"
                f"Bot sẽ tự thử lại sau 10 giây."
            )

    def log_trade(self, side, entry_price, sl_price, tp_price, lot, order_id=""):
        tz_ny = pytz.timezone("America/New_York")
        timestamp = datetime.now(tz_ny).strftime("%Y-%m-%d %H:%M:%S")
        file_exists = os.path.isfile(LOG_FILE)
        with open(LOG_FILE, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=LOG_HEADERS)
            if not file_exists:
                writer.writeheader()
            writer.writerow({
                "datetime_ny": timestamp,
                "symbol":      SYMBOL,
                "side":        side.upper(),
                "lot":         lot,
                "entry_price": round(entry_price, 2),
                "stop_loss":   round(sl_price, 2),
                "take_profit": round(tp_price, 2),
                "risk_usd":    self.risk_usd,
                "order_id":    order_id,
                "outcome":     "",
                "pnl_usd":     "",
                "close_time":  "",
            })
        print(f"   ➔ Đã lưu lệnh vào nhật ký: trade_log.csv")

    def get_open_positions(self):
        """GET /trade/positions — lấy vị thế đang mở"""
        if not self.acc_id or not self.access_token:
            return []
        url = f"{BASE_URL}/trade/positions?accId={self.acc_id}"
        headers = {"Authorization": f"Bearer {self.access_token}"}
        try:
            res = requests.get(url, headers=headers, timeout=10)
            if res.status_code == 200:
                return res.json().get("positions", [])
            print(f"⚠️ /trade/positions HTTP {res.status_code}")
        except Exception as e:
            print(f"⚠️ Lỗi lấy vị thế mở: {e}")
        return []

    def update_trade_outcome(self, order_id: str, outcome: str, pnl_usd: float):
        if not os.path.isfile(LOG_FILE):
            return
        tz_ny = pytz.timezone("America/New_York")
        close_time = datetime.now(tz_ny).strftime("%Y-%m-%d %H:%M:%S")
        rows = []
        updated = False
        with open(LOG_FILE, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("order_id") == str(order_id) and row.get("outcome") == "":
                    row["outcome"]    = outcome
                    row["pnl_usd"]    = round(pnl_usd, 2)
                    row["close_time"] = close_time
                    updated = True
                rows.append(row)
        if updated:
            with open(LOG_FILE, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=LOG_HEADERS)
                writer.writeheader()
                writer.writerows(rows)
            print(f"   ➔ Cập nhật kết quả lệnh {order_id}: {outcome}  P&L=${pnl_usd:+.2f}")

    def get_closed_positions(self) -> list:
        """
        GET /trade/ordersHistory — lấy lịch sử lệnh đã đóng để tracking P&L thực tế.
        Dùng page size từ /trade/config (ORDERS_HISTORY_PAGE_SIZE).
        Response: {"ordersHistory": [...]} hoặc {"d": {"ordersHistory": [...]}}
        """
        if not self.acc_id or not self.access_token:
            return []
        url = f"{BASE_URL}/trade/ordersHistory?accId={self.acc_id}&limit={ORDERS_HISTORY_PAGE_SIZE}"
        headers = {"Authorization": f"Bearer {self.access_token}"}
        try:
            res = requests.get(url, headers=headers, timeout=10)
            if res.status_code == 200:
                data = res.json()
                # TradeLocker có thể wrap trong "d" hoặc trả trực tiếp
                if "d" in data and "ordersHistory" in data["d"]:
                    return data["d"]["ordersHistory"]
                if "ordersHistory" in data:
                    return data["ordersHistory"]
                # Fallback: thử filledOrders nếu ordersHistory trống
                if "filledOrders" in data:
                    return data["filledOrders"]
            else:
                print(f"⚠️ /trade/ordersHistory HTTP {res.status_code}: {res.text[:150]}")
        except Exception as e:
            print(f"⚠️ Lỗi fetch ordersHistory: {e}")
        return []

    def _resolve_closed_pnl(self, order_id: str, pos: dict) -> tuple[str, float]:
        """
        Xác định outcome + P&L thực tế từ /trade/ordersHistory.

        TradeLocker ordersHistory fields (theo tài liệu public):
          id / orderId   — ID lệnh
          profit         — P&L thực tế (số âm = lỗ)
          side           — "buy" / "sell"
          filledPrice    — giá khớp thực
          status         — "Filled" / "Cancelled" v.v.

        Fallback về ước tính nếu không match được record.
        """
        closed = self.get_closed_positions()
        for record in closed:
            rec_id = str(
                record.get("id",
                record.get("orderId",
                record.get("positionId", "")))
            )
            if rec_id != str(order_id):
                continue

            # Bỏ qua lệnh bị huỷ
            status = str(record.get("status", "")).lower()
            if status in ("cancelled", "rejected", "expired"):
                break

            # P&L thực từ sàn
            real_pnl = record.get("profit")
            if real_pnl is not None:
                pnl_val = float(real_pnl)
                outcome = "TP" if pnl_val >= 0 else "SL"
                print(f"   ✅ P&L thực từ ordersHistory: {outcome}  ${pnl_val:+.2f}")
                return outcome, round(pnl_val, 2)

            # Nếu không có profit field, tính từ filledPrice
            filled_price = record.get("filledPrice", record.get("closePrice"))
            if filled_price is not None:
                entry = pos["entry"]
                lot   = pos["lot"]
                cp    = float(filled_price)
                if pos["side"] == "buy":
                    pnl_val = (cp - entry) * lot * NAS100_POINT_VALUE
                else:
                    pnl_val = (entry - cp) * lot * NAS100_POINT_VALUE
                outcome = "TP" if pnl_val >= 0 else "SL"
                print(f"   ✅ P&L tính từ filledPrice: {outcome}  ${pnl_val:+.2f}")
                return outcome, round(pnl_val, 2)

        # Fallback ước tính — giữ nguyên hành vi cũ
        entry = pos["entry"]
        side  = pos["side"]
        risk  = pos["risk_usd"]
        tp    = pos["tp"]
        outcome = ("TP" if tp > entry else "SL") if side == "buy" else ("TP" if tp < entry else "SL")
        pnl = risk * 2.0 if outcome == "TP" else -risk
        print(f"   ⚠️ Không tìm thấy record lệnh {order_id} trong ordersHistory — dùng ước tính.")
        return outcome, round(pnl, 2)

    def poll_open_positions(self):
        if not self.open_positions:
            return
        live_positions = self.get_open_positions()
        live_ids = {str(p.get("id")) for p in live_positions}

        for order_id, pos in list(self.open_positions.items()):
            if str(order_id) not in live_ids:
                side = pos["side"]
                risk = pos["risk_usd"]

                # [NEW 2] Lấy P&L thực từ sàn thay vì ước tính cố định
                outcome, pnl = self._resolve_closed_pnl(order_id, pos)

                self.update_trade_outcome(order_id, outcome, pnl)
                del self.open_positions[order_id]

                tz_ny   = pytz.timezone("America/New_York")
                now_str = datetime.now(tz_ny).strftime("%d/%m/%Y %H:%M:%S")

                if outcome == "TP":
                    self.consecutive_wins   += 1
                    self.consecutive_losses  = 0
                    send_telegram(
                        f"✅ <b>[CHẠM TP] {side.upper()} {SYMBOL}</b>\n"
                        f"🕐 {now_str} (NY)\n"
                        f"💰 P&L thực tế: <b>+${pnl:,.2f}</b>"
                    )
                else:
                    self.consecutive_losses += 1
                    self.consecutive_wins    = 0
                    send_telegram(
                        f"❌ <b>[CHẠM SL] {side.upper()} {SYMBOL}</b>\n"
                        f"🕐 {now_str} (NY)\n"
                        f"💸 P&L thực tế: <b>${pnl:,.2f}</b>"
                    )

                if self.consecutive_wins == 2:
                    send_telegram(f"🔥 <b>2 THẮNG LIÊN TIẾP!</b> Bot đang vào form — tiếp tục theo dõi.")
                elif self.consecutive_wins >= 3:
                    send_telegram(f"🔥🔥 <b>{self.consecutive_wins} THẮNG LIÊN TIẾP!</b> Tuyệt vời!")
                elif self.consecutive_losses == 2:
                    send_telegram(f"⚠️ <b>2 THUA LIÊN TIẾP!</b> Cân nhắc gõ /pause để xem lại điều kiện thị trường.")
                elif self.consecutive_losses >= 3:
                    send_telegram(
                        f"🚨 <b>{self.consecutive_losses} THUA LIÊN TIẾP!</b>\n"
                        f"Bot đã tự tạm dừng để bảo vệ tài khoản.\nGõ /resume để tiếp tục."
                    )
                    self.paused = True

    def needs_token_refresh(self):
        if self.last_auth_time is None:
            return True
        return (time.time() - self.last_auth_time) >= ACCESS_TOKEN_TTL

    def get_account_details(self):
        """
        Lấy danh sách tài khoản và fetch /trade/config để cập nhật page size.
        Endpoint: GET /auth/accounts (TradeLocker backend-api)
        """
        url = f"{BASE_URL}/auth/accounts"
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }
        try:
            res = requests.get(url, headers=headers, timeout=10)
            if res.status_code == 200:
                accounts = res.json().get("accounts", [])
                for acc in accounts:
                    if acc.get("accNum") == ACCOUNT_ID or SERVER in acc.get("name", ""):
                        self.acc_id = acc.get("id")
                        print(f"🔒 Kết nối thành công tài khoản Quỹ Demo {SERVER}! ID: {self.acc_id}")
                        self._fetch_trade_config()
                        return
                if accounts:
                    self.acc_id = accounts[0].get("id")
                    print(f"🔒 Kết nối tài khoản mặc định đầu tiên! ID: {self.acc_id}")
                    self._fetch_trade_config()
            else:
                print(f"⚠️ get_account_details HTTP {res.status_code}: {res.text[:200]}")
        except Exception as e:
            print(f"❌ Lỗi xử lý dữ liệu tài khoản: {e}")

    def _fetch_trade_config(self):
        """
        Fetch /trade/config để lấy giới hạn page size thực tế cho ordersHistory.
        Cập nhật biến module ORDERS_HISTORY_PAGE_SIZE.
        """
        global ORDERS_HISTORY_PAGE_SIZE
        if not self.acc_id or not self.access_token:
            return
        url = f"{BASE_URL}/trade/config?accId={self.acc_id}"
        headers = {"Authorization": f"Bearer {self.access_token}"}
        try:
            res = requests.get(url, headers=headers, timeout=10)
            if res.status_code == 200:
                cfg = res.json()
                # TradeLocker thường trả {"config": {"ordersHistory": {"maxCount": N}, ...}}
                # hoặc {"ordersHistoryMaxCount": N} — thử cả hai cấu trúc
                max_count = (
                    cfg.get("config", {}).get("ordersHistory", {}).get("maxCount")
                    or cfg.get("ordersHistoryMaxCount")
                    or cfg.get("maxOrdersHistory")
                    or cfg.get("historyLimit")
                )
                if max_count:
                    ORDERS_HISTORY_PAGE_SIZE = int(max_count)
                    print(f"📋 /trade/config: ordersHistory page size = {ORDERS_HISTORY_PAGE_SIZE}")
                else:
                    print(f"📋 /trade/config: không tìm thấy maxCount — giữ default {ORDERS_HISTORY_PAGE_SIZE}")
            else:
                print(f"⚠️ /trade/config HTTP {res.status_code} — giữ default page size {ORDERS_HISTORY_PAGE_SIZE}")
        except Exception as e:
            print(f"⚠️ Lỗi fetch /trade/config: {e}")

    def get_candles(self, symbol, resolution="1", count=50):
        """
        GET /trade/instruments/{instrument}/candles
        TradeLocker dùng resolution là số (1=1m, 5=5m, 15=15m, 60=1h, D=daily).
        Thử endpoint v2 trước, fallback về v1-style nếu cần.
        """
        headers = {"Authorization": f"Bearer {self.access_token}"}
        # Endpoint chính của TradeLocker backend-api
        url = f"{BASE_URL}/trade/candles?accId={self.acc_id}&symbol={symbol}&resolution={resolution}&count={count}"
        try:
            res = requests.get(url, headers=headers, timeout=10)
            if res.status_code == 200:
                data = res.json()
                # TradeLocker trả {"candles": [...]} hoặc {"d": {"candles": [...]}}
                if "d" in data and "candles" in data["d"]:
                    return data["d"]["candles"]
                return data.get("candles", [])
            print(f"⚠️ /trade/candles HTTP {res.status_code} — {symbol} {resolution}m")
        except Exception as e:
            print(f"⚠️ Lỗi get_candles: {e}")
        return []

    def check_killzone(self):
        tz_ny = pytz.timezone("America/New_York")
        current_time = datetime.now(tz_ny).strftime("%H:%M")
        # London Open: 3:00–4:30 AM NY (nới thêm 30 phút)
        in_london = "03:00" <= current_time <= "04:30"
        # NY Open: 8:30–11:00 AM NY (bắt đầu từ pre-market, thêm 30 phút sau)
        in_ny     = "08:30" <= current_time <= "11:00"
        return in_london or in_ny

    def current_killzone_name(self):
        tz_ny = pytz.timezone("America/New_York")
        current_time = datetime.now(tz_ny).strftime("%H:%M")
        if "03:00" <= current_time <= "04:30":
            return "London"
        if "08:30" <= current_time <= "11:00":
            return "NewYork"
        return None

    # ──────────────────────────────────────────────────────────
    # [NEW 3] HTF BIAS — ICT Swing Structure (thay thế EMA)
    # Dùng Higher High / Higher Low / Lower High / Lower Low
    # trên khung 15m để xác định bias thực sự theo ICT
    # ──────────────────────────────────────────────────────────
    def _ema(self, values: list[float], period: int) -> float:
        """Tính EMA đơn giản (giữ lại cho trường hợp cần dùng sau)"""
        if len(values) < period:
            return sum(values) / len(values)
        k = 2 / (period + 1)
        ema = sum(values[:period]) / period
        for v in values[period:]:
            ema = v * k + ema * (1 - k)
        return ema

    def _find_swing_points(self, candles: list, lookback: int = 3) -> list[dict]:
        """
        Tìm swing high/low từ danh sách nến (cũ → mới).
        Một điểm là swing high nếu high của nó cao hơn `lookback` nến hai bên.
        Tương tự swing low.
        Trả về list dict: {"type": "high"/"low", "price": float, "idx": int}
        """
        swings = []
        n = len(candles)
        for i in range(lookback, n - lookback):
            c = candles[i]
            # Swing High
            if all(c["high"] > candles[i - j]["high"] for j in range(1, lookback + 1)) and \
               all(c["high"] > candles[i + j]["high"] for j in range(1, lookback + 1)):
                swings.append({"type": "high", "price": c["high"], "idx": i})
            # Swing Low
            if all(c["low"] < candles[i - j]["low"] for j in range(1, lookback + 1)) and \
               all(c["low"] < candles[i + j]["low"] for j in range(1, lookback + 1)):
                swings.append({"type": "low", "price": c["low"], "idx": i})
        return swings

    def get_htf_bias(self) -> str | None:
        """
        [NEW 3] HTF Bias theo ICT Swing Structure trên 15m.

        Logic:
        - Bullish bias: chuỗi Higher High + Higher Low (uptrend structure)
        - Bearish bias: chuỗi Lower High + Lower Low (downtrend structure)
        - Cần ít nhất 2 swing high + 2 swing low gần nhất để xác định.
        - Fallback về EMA nếu không đủ swing points.
        """
        candles_15m = self.get_candles(SYMBOL, resolution="15", count=80)
        if not candles_15m or len(candles_15m) < 20:
            print("⚠️ Không đủ nến 15m để tính HTF bias — bỏ qua filter.")
            return None

        # API trả mới → cũ, đảo ngược để index tăng dần theo thời gian
        candles_chron = list(reversed(candles_15m))

        swings = self._find_swing_points(candles_chron, lookback=3)

        highs = [s for s in swings if s["type"] == "high"]
        lows  = [s for s in swings if s["type"] == "low"]

        if len(highs) >= 2 and len(lows) >= 2:
            last_two_highs = highs[-2:]
            last_two_lows  = lows[-2:]

            hh = last_two_highs[1]["price"] > last_two_highs[0]["price"]  # Higher High
            hl = last_two_lows[1]["price"]  > last_two_lows[0]["price"]   # Higher Low
            lh = last_two_highs[1]["price"] < last_two_highs[0]["price"]  # Lower High
            ll = last_two_lows[1]["price"]  < last_two_lows[0]["price"]   # Lower Low

            if hh and hl:
                print(f"   📈 HTF Bias: BULLISH (HH + HL structure trên 15m)")
                return "bull"
            if lh and ll:
                print(f"   📉 HTF Bias: BEARISH (LH + LL structure trên 15m)")
                return "bear"

            # Partial structure — chỉ một điều kiện đúng, dùng EMA làm tiebreaker
            print("   ↔️ HTF Swing structure không rõ ràng — dùng EMA làm tiebreaker.")

        # Fallback EMA khi không đủ swing points hoặc structure không rõ
        closes = [c["close"] for c in candles_chron]
        ema20  = self._ema(closes, 20)
        ema50  = self._ema(closes, 50)
        if ema20 > ema50:
            return "bull"
        elif ema20 < ema50:
            return "bear"
        return None

    def execute_trade(self, side, entry_price, sl_price):
        if side == "buy":
            distance = entry_price - sl_price
            tp_price = entry_price + (distance * 2.0)
        else:
            distance = sl_price - entry_price
            tp_price = entry_price - (distance * 2.0)

        if distance <= 0:
            return

        # ──────────────────────────────────────────────────────
        # [FIX D] Công thức lot đúng cho NAS100
        # risk_usd = lot × distance × point_value
        # → lot = risk_usd / (distance × point_value)
        # ──────────────────────────────────────────────────────
        calculated_lot = self.risk_usd / (distance * NAS100_POINT_VALUE)

        if calculated_lot < 0.05:
            print(f"⚠️ Bỏ qua tín hiệu: Khoảng cách vùng nến quá rộng (Lot {calculated_lot:.2f} < 0.05).")
            return

        final_lot = max(0.05, round(calculated_lot, 2))

        url = f"{BASE_URL}/trade/orders"
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }
        payload = {
            "accId":      self.acc_id,
            "instrument": SYMBOL,          # TradeLocker dùng "instrument" không phải "symbol"
            "side":       side,            # "buy" / "sell"
            "type":       "market",
            "qty":        final_lot,       # TradeLocker dùng "qty" không phải "quantity"
            "stopLoss":   round(sl_price, 2),
            "takeProfit": round(tp_price, 2),
        }

        try:
            order_res = requests.post(url, json=payload, headers=headers)
            if order_res.status_code in (200, 201):
                order_data = order_res.json()
                # TradeLocker thường wrap response trong "d": {"orderId": ...}
                d = order_data.get("d", order_data)
                order_id = str(d.get("orderId", d.get("id", order_data.get("orderId", ""))))
                print(f"🚀 [VÀO LỆNH THÀNH CÔNG] {side.upper()} {SYMBOL}! Lot: {final_lot}  ID: {order_id}")
                self.log_trade(side, entry_price, sl_price, tp_price, final_lot, order_id)
                if order_id:
                    self.open_positions[order_id] = {
                        "side": side, "entry": entry_price,
                        "sl": sl_price, "tp": tp_price,
                        "lot": final_lot, "risk_usd": self.risk_usd,
                    }
                tz_ny = pytz.timezone("America/New_York")
                now_str = datetime.now(tz_ny).strftime("%d/%m/%Y %H:%M:%S")
                emoji = "🟢" if side == "buy" else "🔴"
                kz = self.current_killzone_name() or ""
                send_telegram(
                    f"{emoji} <b>[VÀO LỆNH] {side.upper()} {SYMBOL}</b>  {kz}\n"
                    f"🕐 {now_str} (NY)\n"
                    f"📌 Entry : <b>{round(entry_price, 2)}</b>\n"
                    f"🛑 Stop Loss : <b>{round(sl_price, 2)}</b>\n"
                    f"🎯 Take Profit : <b>{round(tp_price, 2)}</b>\n"
                    f"📦 Khối lượng : <b>{final_lot} Lot</b>\n"
                    f"💰 Rủi ro : <b>${self.risk_usd}</b>"
                )
            else:
                print(f"❌ Sàn từ chối khớp lệnh: {order_res.text}")
        except Exception as e:
            print(f"❌ Lỗi phát lệnh lên sàn: {e}")

    def check_smt_divergence(self, nas_candles, es_candles):
        """
        THUẬT TOÁN ICT: LIQUIDITY SWEPT + SMT DIVERGENCE

        Thay đổi v4:
        - MSS confirmation: 3 nến (giảm từ 5 — bắt kèo nhanh hơn)
        - ES tolerance: 0.1% thay vì bằng nhau tuyệt đối
          (thực tế ES không bao giờ giữ đúng tuyệt đối)
        - Debug log chi tiết cho từng điều kiện
        """
        if not nas_candles or not es_candles:
            print(f"   ⚠️ [SMT] Không lấy được nến — NAS: {len(nas_candles) if nas_candles else 0}  ES: {len(es_candles) if es_candles else 0}")
            return None, None

        if len(nas_candles) < 35 or len(es_candles) < 35:
            print(f"   ⚠️ [SMT] Không đủ nến — NAS: {len(nas_candles)}  ES: {len(es_candles)} (cần 35)")
            return None, None

        nas_curr = nas_candles[0]
        es_curr  = es_candles[0]

        # Swing trong 30 nến trước (index 1..30)
        nas_prev_low  = min(c["low"]  for c in nas_candles[1:31])
        nas_prev_high = max(c["high"] for c in nas_candles[1:31])
        es_prev_low   = min(c["low"]  for c in es_candles[1:31])
        es_prev_high  = max(c["high"] for c in es_candles[1:31])

        # MSS confirmation: 3 nến (giảm từ 5)
        mss_swing_high = max(c["high"] for c in nas_candles[1:4])
        mss_swing_low  = min(c["low"]  for c in nas_candles[1:4])

        # ES tolerance 0.1% — ES không cần giữ đáy/đỉnh tuyệt đối
        es_low_tolerance  = es_prev_low  * 1.001   # cho phép ES thấp hơn 0.1%
        es_high_tolerance = es_prev_high * 0.999   # cho phép ES cao hơn 0.1%

        # ── DEBUG: in trạng thái từng điều kiện mỗi lần scan ──
        nas_swept_low  = nas_curr["low"]  < nas_prev_low
        nas_swept_high = nas_curr["high"] > nas_prev_high
        es_held_low    = es_curr["low"]   >= es_prev_low - (es_prev_low * 0.001)
        es_held_high   = es_curr["high"]  <= es_prev_high + (es_prev_high * 0.001)
        mss_bull       = nas_curr["close"] > mss_swing_high
        mss_bear       = nas_curr["close"] < mss_swing_low

        print(
            f"   📊 NAS low={nas_curr['low']:.1f} prev_low={nas_prev_low:.1f} swept={'✅' if nas_swept_low else '❌'} | "
            f"ES held_low={'✅' if es_held_low else '❌'} | MSS_bull={'✅' if mss_bull else '❌'}"
        )
        print(
            f"   📊 NAS high={nas_curr['high']:.1f} prev_high={nas_prev_high:.1f} swept={'✅' if nas_swept_high else '❌'} | "
            f"ES held_high={'✅' if es_held_high else '❌'} | MSS_bear={'✅' if mss_bear else '❌'}"
        )

        # ── BUY SETUP ──
        if nas_swept_low and es_held_low and mss_bull:
            buffer   = nas_curr["low"] * SL_BUFFER_PCT
            sl_price = nas_curr["low"] - buffer
            print(f"   🟢 [BUY SIGNAL] SL={sl_price:.2f}")
            return "buy", round(sl_price, 2)

        # ── SELL SETUP ──
        if nas_swept_high and es_held_high and mss_bear:
            buffer   = nas_curr["high"] * SL_BUFFER_PCT
            sl_price = nas_curr["high"] + buffer
            print(f"   🔴 [SELL SIGNAL] SL={sl_price:.2f}")
            return "sell", round(sl_price, 2)

        return None, None

    def run_strategy(self):
        # Reset cooldown kill zone nếu sang ngày mới
        self._reset_killzone_cooldown_if_new_day()

        tz_ny    = pytz.timezone("America/New_York")
        now_str  = datetime.now(tz_ny).strftime("%H:%M:%S")

        if self.paused:
            print(f"\r⏸️  [{now_str}] Bot tạm dừng — gõ /resume để tiếp tục.          ", end="")
            return

        # Session filter
        allowed, reason = is_trading_allowed()
        if not allowed:
            print(f"\r🚫 [{now_str}] {reason}          ", end="")
            return

        # Giới hạn rủi ro ngày
        today_risk = risk_deployed_today()
        if today_risk >= self.daily_loss_limit:
            if not self.paused:
                self.paused = True
                send_telegram(
                    f"🛡️ <b>ĐÃ ĐẠT GIỚI HẠN RỦI RO NGÀY!</b>\n\n"
                    f"Tổng rủi ro hôm nay: <b>${today_risk:,.2f}</b> / giới hạn <b>${self.daily_loss_limit:,.2f}</b>\n"
                    f"Bot đã tự tạm dừng để bảo vệ tài khoản.\nGõ /resume để tiếp tục."
                )
            return

        # Giới hạn số lệnh ngày
        trades_today = count_trades_today()
        if trades_today >= self.max_trades_per_day:
            print(f"\r🔢 [{now_str}] Đã đạt {trades_today}/{self.max_trades_per_day} lệnh/ngày.          ", end="")
            return

        # Kill zone check
        if not self.check_killzone():
            print(f"\r⏳ [{now_str}] Ngoài Kill Zone — chờ London(3–4:30AM) / NY(8:30–11AM).          ", end="")
            return

        kz = self.current_killzone_name()

        # Cooldown per kill zone
        if kz and kz in self.traded_killzones_today:
            print(f"\r🔒 [{now_str}] Kill zone {kz} đã có lệnh hôm nay — chờ phiên tiếp theo.          ", end="")
            return

        # Lấy nến
        nas_candles = self.get_candles(SYMBOL,    resolution="1", count=50)
        es_candles  = self.get_candles(SYMBOL_ES, resolution="1", count=50)

        print(f"\n🔎 [{now_str}] Đang scan SMT [{kz}] — NAS={len(nas_candles)} nến  ES={len(es_candles)} nến")

        signal, sl_price = self.check_smt_divergence(nas_candles, es_candles)

        if not signal:
            print(f"   ⏩ Không có tín hiệu — tiếp tục chờ...")
            return

        # HTF Bias Filter
        htf_bias = self.get_htf_bias()
        if htf_bias is not None:
            if signal == "buy" and htf_bias != "bull":
                print(f"   🚫 [HTF FILTER] BUY bị chặn — 15m bias: {htf_bias}")
                return
            if signal == "sell" and htf_bias != "bear":
                print(f"   🚫 [HTF FILTER] SELL bị chặn — 15m bias: {htf_bias}")
                return
        else:
            print(f"   ℹ️ HTF bias không xác định — bỏ qua filter, tiếp tục vào lệnh.")

        entry_price = nas_candles[0]["close"]
        print(f"   ✅ [{kz}] {signal.upper()} Entry={entry_price:.2f}  SL={sl_price:.2f}  HTF={htf_bias or 'N/A'}")
        self.execute_trade(signal, entry_price, sl_price)

        if kz:
            self.traded_killzones_today.add(kz)

        time.sleep(60)


if __name__ == "__main__":
    bot = TradeLockerTokenBot()
    if bot.access_token:
        print("\n🤖 Hệ thống Bot ICT Pro Max xác thực Token thành công!")
        print("📡 Bot đang quét sàn NAS100 ngầm và túc trực kèo cho Việt Anh...")

        days_left = get_refresh_token_days_left()
        if days_left is not None:
            print(f"🗓️ Refresh Token còn hiệu lực: {days_left} ngày")
            if days_left <= 0:
                send_telegram(
                    "🚨 <b>REFRESH TOKEN ĐÃ HẾT HẠN!</b>\n\n"
                    "Bot sẽ không thể xác thực được nữa.\n\n"
                    "<b>Cách lấy token mới:</b>\n"
                    "1. Mở Chrome → vào <b>tradelocker.com</b> và đăng nhập\n"
                    "2. Nhấn <b>F12</b> → chọn tab <b>Application</b>\n"
                    "3. Bên trái chọn <b>Local Storage</b> → click vào tradelocker.com\n"
                    "4. Tìm key <b>refresh_token</b> → copy giá trị\n"
                    "5. Vào Render → <b>Environment</b> → cập nhật <b>REFRESH_TOKEN</b> → Save"
                )
            elif days_left <= 3:
                send_telegram(
                    f"⚠️ <b>REFRESH TOKEN SẮP HẾT HẠN!</b>\n\n"
                    f"Còn <b>{days_left} ngày</b> — cần cập nhật sớm!\n\n"
                    "<b>Cách lấy token mới:</b>\n"
                    "1. Mở Chrome → vào <b>tradelocker.com</b> và đăng nhập\n"
                    "2. Nhấn <b>F12</b> → chọn tab <b>Application</b>\n"
                    "3. Bên trái chọn <b>Local Storage</b> → click vào tradelocker.com\n"
                    "4. Tìm key <b>refresh_token</b> → copy giá trị\n"
                    "5. Vào Render → <b>Environment</b> → cập nhật <b>REFRESH_TOKEN</b> → Save"
                )

        listener = threading.Thread(target=poll_telegram_commands, args=(bot,), daemon=True)
        listener.start()

        port = int(os.environ.get("PORT", 8080))
        try:
            health_srv = HTTPServer(("0.0.0.0", port), _Health)
            threading.Thread(target=health_srv.serve_forever, daemon=True).start()
            print(f"🌐 Health server đang chạy trên cổng {port}")
        except OSError:
            print(f"⚠️ Cổng {port} đang bận — Đã chuyển sang chế độ Dev.")

        last_expiry_check_date  = None
        last_position_poll_time = 0
        while True:
            try:
                tz_ny = pytz.timezone("America/New_York")
                now_ny = datetime.now(tz_ny)

                if bot.needs_token_refresh():
                    print("\n🔄 Đang làm mới Access Token theo lịch tự động...")
                    bot.authenticate_with_token()

                today_date = now_ny.strftime("%Y-%m-%d")
                if now_ny.hour == DAILY_SUMMARY_HOUR and bot.last_summary_date != today_date:
                    bot.last_summary_date = today_date
                    send_daily_summary()

                # [NEW 1] Cảnh báo NFP Friday lúc 8AM NY
                if now_ny.hour == 8 and is_nfp_friday() and bot.last_summary_date != today_date + "_nfp":
                    bot.last_summary_date = today_date + "_nfp"
                    send_telegram(
                        "📰 <b>HÔM NAY LÀ THỨ SÁU NFP!</b>\n\n"
                        "Bot sẽ <b>không trade</b> hôm nay do nguy cơ biến động đột ngột.\n"
                        "Phiên tiếp theo: Thứ Hai tuần sau."
                    )

                if now_ny.hour == 9 and last_expiry_check_date != today_date:
                    last_expiry_check_date = today_date
                    days_left = get_refresh_token_days_left()
                    if days_left is not None and days_left <= 3:
                        send_telegram(
                            f"⚠️ <b>CẢNH BÁO HẠN TOKEN</b>\n\n"
                            f"Refresh Token còn <b>{days_left} ngày</b>!\n\n"
                            "<b>Cách lấy token mới:</b>\n"
                            "1. Mở Chrome → vào <b>tradelocker.com</b> và đăng nhập\n"
                            "2. Nhấn <b>F12</b> → chọn tab <b>Application</b>\n"
                            "3. Bên trái chọn <b>Local Storage</b> → click vào tradelocker.com\n"
                            "4. Tìm key <b>refresh_token</b> → copy giá trị\n"
                            "5. Vào Render → <b>Environment</b> → cập nhật <b>REFRESH_TOKEN</b> → Save"
                        )

                if time.time() - last_position_poll_time >= 30:
                    bot.poll_open_positions()
                    last_position_poll_time = time.time()

                bot.run_strategy()
            except Exception as e:
                print(f"\n⚠️ Lỗi vòng lặp, tự động kết nối lại sau 10s: {e}")
                time.sleep(10)
                bot.authenticate_with_token()
            time.sleep(2)
