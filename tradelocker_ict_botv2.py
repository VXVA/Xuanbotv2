import csv
import os
import threading
import time
import requests
from datetime import datetime
import pytz
from http.server import HTTPServer, BaseHTTPRequestHandler

# ============================================================
# CẤU HÌNH HỆ THỐNG & BIẾN MÔI TRƯỜNG
# ============================================================
ACCESS_TOKEN_TTL = 25 * 60  # Làm mới Access Token sau mỗi 25 phút

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID")

LOG_FILE = os.path.join(os.path.dirname(__file__), "trade_log.csv")
LOG_HEADERS = ["datetime_ny", "symbol", "side", "lot", "entry_price", "stop_loss", "take_profit", "risk_usd"]
DAILY_SUMMARY_HOUR = 23  # Gửi tóm tắt ngày lúc 11 giờ đêm giờ New York

REFRESH_TOKEN = "eyJhbGciOiJIUzUxMiIsInR5cCIgOiAiSldUIiwia2lkIiA6ICJlNGVjYzhkNy01NjcxLTQxZWQtOTUwYy1hZDVhNTIxMzgxMDIifQ.eyJleHAiOjE3ODI0NDIwMTQsImlhdCI6MTc3OTg1MDAxNCwianRpIjoiNDJjYmM0YjYtZGRiNC0yNjhlLWQ1OTktNDdiYTJjNmEwMDllIiwiaXNzIjoiaHR0cHM6Ly9hdXRoLnRyYWRlbG9ja2VyLmNvbS9yZWFsbXMvdHJhZeriOiJodHRwczovL2F1dGgudHJhZGVsb2NrZXIuY29tL3JlYWxtcy90cmFkZWxvY2tlciIsInN1YiI6ImY0MDA0NTJiLTMzZGQtNDA2OC04NTY4LWY2YTA4NDExZWNkOCIsInR5cCI6IlJlZnJlc2giLCJhenAiOiJmcm9udGVuZC13ZWItZGVtbyIsInNpZCI6IjBiNjdjZDViLTA2MjYtNmU5My1mZjE4LTg5ZGI0MDQzMzJlMCIsInNjb3BlIjoib3BlbmlkIGJhc2ljIGZyb250ZW5kLWF1ZGllbmNlIHJvbGVzIHdlYi1vcmlnaW5zIHByb2ZpbGUgRnJvbnRlbmRfQ2xpZW50X1Njb3BlIn0.-DlMAagYSm2DJPe9UmirnFkChXxXdrWA5XiUQj9t67KX2ia9m4aq6dQpin30WCSnuFDdUxAS8ekpUUxg15mbBQ"

ACCOUNT_ID = "vietanh9a2k5@gmail.com"  # Email đăng nhập của bạn
SERVER = "BLUEG"                       # Mã server quỹ Blue Guardian
RISK_USD = 25.0                        # Rủi ro mặc định $25
SYMBOL = "NAS100"                      # Mã giao dịch chính
SYMBOL_ES = "SPX500"                   # Mã đối chứng SMT

BASE_URL = "https://demo.tradelocker.com/api/v1"


def send_telegram(message: str):
    """Gửi thông báo lệnh tức thì lên Telegram"""
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


def send_daily_summary():
    """Đọc trade_log.csv và gửi tóm tắt kết quả ngày hôm nay lên Telegram"""
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

    total       = len(trades_today)
    buys        = sum(1 for t in trades_today if t["side"] == "BUY")
    sells       = total - buys
    total_risk  = sum(t["risk_usd"] for t in trades_today)
    max_profit  = total_risk * 2.0   
    max_loss    = total_risk          
    avg_lot     = sum(t["lot"] for t in trades_today) / total

    lines = [f"📋 <b>TÓM TẮT NGÀY {today_label}</b>\n"]
    lines.append(f"📊 Tổng lệnh hôm nay : <b>{total}</b>  (🟢 BUY {buys}  |  🔴 SELL {sells})")
    lines.append(f"📦 Lot trung bình     : <b>{avg_lot:.2f}</b>")
    lines.append(f"💰 Tổng rủi ro triển khai : <b>${total_risk:,.2f}</b>")
    lines.append(f"")
    lines.append(f"<b>Kịch bản kết quả (ước tính):</b>")
    lines.append(f"  ✅ Nếu tất cả chạm TP : <b>+${max_profit:,.2f}</b>")
    lines.append(f"  ❌ Nếu tất cả chạm SL : <b>-${max_loss:,.2f}</b>")
    lines.append(f"  ⚖️ Điểm hòa vốn       : <b>33.3% win rate</b>")
    lines.append(f"")
    lines.append(f"🤖 Bot tiếp tục chạy và sẵn sàng cho phiên tiếp theo.")

    send_telegram("\n".join(lines))
    print(f"📨 Đã gửi tóm tắt ngày {today_label} lên Telegram.")


def count_trades_today():
    """Đếm số lệnh đã vào hôm nay từ trade_log.csv"""
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


def poll_telegram_commands(bot_ref):
    """Lắng nghe lệnh điều khiển từ Telegram trong luồng riêng"""
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
                        "🔵 Ngoài Kill Zone — Bot đang túc trực chờ phiên Mỹ (21:30–22:30 NY)."
                    )
                    trade_status = (
                        "⏸️ <b>ĐÃ TẠM DỪNG</b> — Gõ /resume để tiếp tục."
                        if bot_ref.paused else
                        "▶️ <b>ĐANG CHẠY</b> — Gõ /pause để tạm dừng."
                    )

                    send_telegram(
                        f"📡 <b>TRẠNG THÁI BOT ICT PRO MAX</b>\n"
                        f"🕐 {now_ny.strftime('%d/%m/%Y %H:%M:%S')} (NY)\n\n"
                        f"{trade_status}\n"
                        f"{zone_status}\n\n"
                        f"{token_line}\n"
                        f"📊 Lệnh hôm nay: <b>{trades_today}</b>\n"
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
                        "📡 /status — Xem trạng thái bot, kill zone, token, risk hiện tại\n"
                        "📋 /today — Xem toàn bộ lệnh đã vào hôm nay\n"
                        "⏸️ /pause — Tạm dừng vào lệnh mới\n"
                        "▶️ /resume — Tiếp tục giao dịch bình thường\n"
                        "⚙️ /risk &lt;số&gt; — Đổi risk mỗi lệnh (vd: /risk 15)\n"
                        "❓ /help — Hiển thị danh sách lệnh này"
                    )

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
                        for i, t in enumerate(trades_today, 1):
                            side = t["side"]
                            emoji = "🟢" if side == "BUY" else "🔴"
                            time_str = t["datetime_ny"][11:16]
                            risk = float(t["risk_usd"])
                            total_risk += risk
                            lines.append(
                                f"{emoji} <b># {i} {side}</b>  {time_str} NY\n"
                                f"   Entry <b>{t['entry_price']}</b>  SL <b>{t['stop_loss']}</b>  TP <b>{t['take_profit']}</b>  Lot <b>{t['lot']}</b>"
                            )
                        lines.append(f"\n💰 Tổng rủi ro hôm nay: <b>${total_risk:,.2f}</b>")
                        send_telegram("\n".join(lines))

        except Exception as e:
            print(f"⚠️ Telegram polling lỗi: {e}")
            time.sleep(10)


class _Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
        
    def log_message(self, format, *args):
        """Ghi đè chuẩn để tắt hoàn toàn log nhật ký mạng HTTP, tránh lỗi type hint"""
        pass


class TradeLockerTokenBot:
    def __init__(self):
        self.access_token = None
        self.acc_id = None
        self.last_auth_time = None
        self.last_summary_date = None  
        self.paused = False            
        self.risk_usd = RISK_USD       
        self.authenticate_with_token()

    def authenticate_with_token(self):
        url = "https://auth.tradelocker.com/realms/tradelocker/protocol/openid-connect/token"
        payload = {
            "grant_type": "refresh_token",
            "client_id": "frontend-web-demo",
            "refresh_token": REFRESH_TOKEN,
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
                print(f"❌ Mã Refresh Token hết hạn hoặc sai cấu hình. Mã lỗi: {response.status_code}")
        except Exception as e:
            print(f"❌ Không thể kết nối đến tổng đài xác thực: {e}")

    def log_trade(self, side, entry_price, sl_price, tp_price, lot):
        tz_ny = pytz.timezone("America/New_York")
        timestamp = datetime.now(tz_ny).strftime("%Y-%m-%d %H:%M:%S")
        file_exists = os.path.isfile(LOG_FILE)
        with open(LOG_FILE, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=LOG_HEADERS)
            if not file_exists:
                writer.writeheader()
            writer.writerow({
                "datetime_ny": timestamp,
                "symbol": SYMBOL,
                "side": side.upper(),
                "lot": lot,
                "entry_price": round(entry_price, 2),
                "stop_loss": round(sl_price, 2),
                "take_profit": round(tp_price, 2),
                "risk_usd": self.risk_usd,
            })
        print(f"   ➔ Đã lưu lệnh vào nhật ký: trade_log.csv")

    def needs_token_refresh(self):
        if self.last_auth_time is None:
            return True
        return (time.time() - self.last_auth_time) >= ACCESS_TOKEN_TTL

    def get_account_details(self):
        url = f"{BASE_URL}/auth/accounts"
        headers = {"Authorization": f"Bearer {self.access_token}"}
        try:
            res = requests.get(url, headers=headers)
            if res.status_code == 200:
                accounts = res.json().get("accounts", [])
                for acc in accounts:
                    if acc.get("accNum") == ACCOUNT_ID or SERVER in acc.get("name", ""):
                        self.acc_id = acc.get("id")
                        print(f"🔒 Kết nối thành công tài khoản Quỹ Demo {SERVER}! ID hệ thống: {self.acc_id}")
                        return
                if accounts:
                    self.acc_id = accounts[0].get("id")
                    print(f"🔒 Kết nối thành công tài khoản mặc định có sẵn đầu tiên! ID: {self.acc_id}")
        except Exception as e:
            print(f"❌ Lỗi xử lý dữ liệu tài khoản sau đăng nhập: {e}")

    def get_candles(self, symbol):
        # MỞ RỘNG TẦM NHÌN: Tăng count từ 20 lên 50 nến để bao quát đủ quá khứ Kill Zone
        url = f"{BASE_URL}/market/candles?symbol={symbol}&resolution=1m&count=50"
        headers = {"Authorization": f"Bearer {self.access_token}"}
        try:
            res = requests.get(url, headers=headers)
            if res.status_code == 200:
                return res.json().get("candles", [])
        except:
            return []
        return []

    def check_killzone(self):
        tz_ny = pytz.timezone("America/New_York")
        current_time = datetime.now(tz_ny).strftime("%H:%M")
        return "09:30" <= current_time <= "10:30"

    def execute_trade(self, side, entry_price, sl_price):
        if side == "buy":
            distance = entry_price - sl_price
            tp_price = entry_price + (distance * 2.0)  
        else:  
            distance = sl_price - entry_price
            tp_price = entry_price - (distance * 2.0)  

        if distance <= 0:
            return

        calculated_lot = self.risk_usd / distance
        if calculated_lot < 0.05:
            print(f"⚠️ Bỏ qua tín hiệu: Khoảng cách vùng nến quá rộng (Lot {calculated_lot:.2f} < 0.05).")
            return

        final_lot = max(0.05, round(calculated_lot, 2))

        url = f"{BASE_URL}/trade/order"
        headers = {"Authorization": f"Bearer {self.access_token}"}
        payload = {
            "accId": self.acc_id,
            "symbol": SYMBOL,
            "side": side,
            "type": "market",
            "quantity": final_lot,
            "stopLoss": round(sl_price, 2),
            "takeProfit": round(tp_price, 2),
        }

        try:
            order_res = requests.post(url, json=payload, headers=headers)
            if order_res.status_code == 200:
                print(f"🚀 [VÀO LỆNH THÀNH CÔNG] {side.upper()} {SYMBOL}! Lot: {final_lot}")
                self.log_trade(side, entry_price, sl_price, tp_price, final_lot)
                tz_ny = pytz.timezone("America/New_York")
                now_str = datetime.now(tz_ny).strftime("%d/%m/%Y %H:%M:%S")
                emoji = "🟢" if side == "buy" else "🔴"
                send_telegram(
                    f"{emoji} <b>[VÀO LỆNH] {side.upper()} {SYMBOL}</b>\n"
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
        """THUẬT TOÁN ICT: LIQUIDITY SWEPT + SMT DIVERGENCE XÁC NHẬN (BẢN UPDATE TẦM NHÌN 30 PHÚT)"""
        # Đảm bảo nhận đủ dữ liệu nến cho thấu kính rộng hơn
        if not nas_candles or not es_candles or len(nas_candles) < 35 or len(es_candles) < 35:
            return None, None

        nas_curr = nas_candles[0]
        es_curr  = es_candles[0]

        # NÂNG CẤP XỊN: Quét tìm đỉnh/đáy trong vòng 30 cây nến trước (30 phút) thay vì chỉ nhìn 5 nến
        nas_prev_low  = min(c["low"]  for c in nas_candles[1:31])
        nas_prev_high = max(c["high"] for c in nas_candles[1:31])
        es_prev_low   = min(c["low"]  for c in es_candles[1:31])
        es_prev_high  = max(c["high"] for c in es_candles[1:31])

        # ── BUY SETUP (NAS quét đáy sâu trong 30p qua + ES giữ đáy + Nến đóng cửa MSS)
        if (nas_curr["low"] < nas_prev_low 
                and es_curr["low"] >= es_prev_low 
                and nas_curr["close"] > nas_candles[1]["high"]):
            return "buy", nas_curr["low"]

        # ── SELL SETUP (NAS quét đỉnh cao trong 30p qua + ES giữ đỉnh + Nến đóng cửa MSS)
        if (nas_curr["high"] > nas_prev_high 
                and es_curr["high"] <= es_prev_high 
                and nas_curr["close"] < nas_candles[1]["low"]):
            return "sell", nas_curr["high"]

        return None, None

    def run_strategy(self):
        if self.paused:
            print("⏸️  Bot đang tạm dừng. Gõ /resume trên Telegram để tiếp tục.", end="\r")
            return

        if not self.check_killzone():
            print("⏳ Ngoài Kill Zone (9:30–10:30 PM NY). Bot đứng chờ phiên Mỹ để quét SMT...", end="\r")
            return

        nas_candles = self.get_candles(SYMBOL)
        es_candles  = self.get_candles(SYMBOL_ES)

        signal, sl_price = self.check_smt_divergence(nas_candles, es_candles)

        if signal:
            entry_price = nas_candles[0]["close"]
            print(f"\n🔍 [SMT] Kích hoạt tín hiệu {signal.upper()} | Entry: {entry_price:.2f} SL: {sl_price:.2f}")
            self.execute_trade(signal, entry_price, sl_price)
            time.sleep(60)  


if __name__ == "__main__":
    bot = TradeLockerTokenBot()
    if bot.access_token:
        print("\n🤖 Hệ thống Bot ICT Pro Max xác thực Token thành công!")
        print("📡 Bot đang quét sàn NAS100 ngầm và túc trực kèo cho Việt Anh...")

        listener = threading.Thread(target=poll_telegram_commands, args=(bot,), daemon=True)
        listener.start()

        port = int(os.environ.get("PORT", 8080))
        try:
            health_srv = HTTPServer(("0.0.0.0", port), _Health)
            threading.Thread(target=health_srv.serve_forever, daemon=True).start()
            print(f"🌐 Health server đang chạy trên cổng {port}")
        except OSError:
            print(f"⚠️ Cổng {port} đang bận — Đã chuyển sang chế độ Dev.")

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

                bot.run_strategy()
            except Exception as e:
                print(f"\n⚠️ Lỗi vòng lặp, tự động kết nối lại sau 10s: {e}")
                time.sleep(10)
                bot.authenticate_with_token()
            time.sleep(2)
