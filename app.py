import os
import sys
import re
from datetime import datetime, timedelta
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError, LineBotApiError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, SourceGroup, SourceRoom, SourceUser
import psycopg2

# --- 環境變數設定 ---
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
DATABASE_URL = os.environ.get('DATABASE_URL')
# NEW: 排除的群組ID列表 (用於測試功能時跳過某些群組)
EXCLUDE_GROUP_IDS_STR = os.environ.get('EXCLUDE_GROUP_IDS', '')
EXCLUDE_GROUP_IDS = set(EXCLUDE_GROUP_IDS_STR.split(',')) if EXCLUDE_GROUP_IDS_STR else set()

# --- 診斷與初始化 ---
if not LINE_CHANNEL_ACCESS_TOKEN:
    sys.exit("LINE_CHANNEL_ACCESS_TOKEN is missing!")
if not LINE_CHANNEL_SECRET:
    sys.exit("LINE_CHANNEL_SECRET is missing!")

app = Flask(__name__)
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# --- 姓名正規化工具 ---
def normalize_name(name):
    # 移除開頭被括號包裹的內容
    normalized = re.sub(r'^\s*[（(\[【][^()\[\]]{1,10}[)）\]】]\s*', '', name).strip()
    return normalized if normalized else name

# --- 資料庫連線函式 ---
def get_db_connection():
    try:
        conn = psycopg2.connect(DATABASE_URL, sslmode='require')
        return conn
    except Exception as e:
        print(f"DATABASE CONNECTION ERROR: {e}", file=sys.stderr)
        return None

# --- 資料庫操作函式 ---

def add_reporter(group_id, reporter_name):
    conn = get_db_connection()
    if conn is None: return "❌ 資料庫連線失敗。"
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM reporters WHERE group_id = %s AND reporter_name = %s", (group_id, reporter_name))
            if cur.fetchone():
                return f"🤨 {reporter_name} 早就在名單裡面坐好坐滿了。"
            
            cur.execute("INSERT INTO reporters (group_id, reporter_name) VALUES (%s, %s)", (group_id, reporter_name))
            conn.commit()
            return f"🎉 好嘞～ {reporter_name} 已成功加入名單！"
    except Exception as e:
        print(f"ADD ERROR: {e}", file=sys.stderr)
        return "❌ 新增失敗，請稍後再試。"
    finally:
        conn.close()

def delete_reporter(group_id, reporter_name):
    conn = get_db_connection()
    if conn is None: return "❌ 資料庫連線失敗。"
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM reporters WHERE group_id = %s AND reporter_name = %s", (group_id, reporter_name))
            if cur.rowcount > 0:
                # 同步刪除該人名的歷史紀錄
                cur.execute("DELETE FROM reports WHERE group_id = %s AND reporter_name = %s", (group_id, reporter_name))
                conn.commit()
                return f"🗑️ {reporter_name} 已從名單中被溫柔移除。"
            return f"❓名單裡根本沒有 {reporter_name} 啊！"
    except Exception as e:
        print(f"DELETE ERROR: {e}", file=sys.stderr)
        return "❌ 刪除失敗，請稍後再試。"
    finally:
        conn.close()

def get_reporter_list(group_id):
    conn = get_db_connection()
    if conn is None: return "❌ 資料庫連線失敗。"
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT reporter_name FROM reporters WHERE group_id = %s ORDER BY reporter_name", (group_id,))
            reporters = [row[0] for row in cur.fetchall()]
            if reporters:
                # 正規化顯示 (合併重複的實體人名)
                normalized_set = sorted(list(set([normalize_name(r) for r in reporters])))
                list_str = "\n".join([f"🔸 {name}" for name in normalized_set])
                return f"📋 最新回報觀察名單如下：\n{list_str}\n\n（嗯，看起來大家都還活著。）"
            return "📭 名單空空如也～"
    except Exception as e:
        print(f"LIST ERROR: {e}", file=sys.stderr)
        return "❌ 查詢失敗。"
    finally:
        conn.close()

def log_report(group_id, report_date, reporter_name):
    conn = get_db_connection()
    if conn is None: return "❌ 資料庫連線失敗。"
    
    normalized_input = normalize_name(reporter_name)
    date_str = report_date.strftime('%Y.%m.%d')

    try:
        with conn.cursor() as cur:
            # 1. 自動補名單
            cur.execute("INSERT INTO reporters (group_id, reporter_name) VALUES (%s, %s) ON CONFLICT DO NOTHING", (group_id, reporter_name))
            
            # 2. 檢查是否重複 (使用正規化名稱比對)
            cur.execute("SELECT reporter_name FROM reports WHERE group_id = %s AND report_date = %s", (group_id, report_date))
            submitted_raw_names = [row[0] for row in cur.fetchall()]
            submitted_normalized = [normalize_name(n) for n in submitted_raw_names]

            if normalized_input in submitted_normalized:
                return f"⚠️ {reporter_name} ({date_str}) 今天已經回報過了！"

            # 3. 寫入紀錄
            cur.execute(
                "INSERT INTO reports (group_id, reporter_name, report_date, report_content) VALUES (%s, %s, %s, %s)",
                (group_id, reporter_name, report_date, "打卡紀錄 (內容已省略)")
            )
            conn.commit()
            return f"👌 收到！{reporter_name} ({date_str}) 的心得已成功登入檔案。"
            
    except Exception as e:
        print(f"LOG ERROR: {e}", file=sys.stderr)
        return "❌ 記錄失敗，請稍後再試。"
    finally:
        conn.close()

# --- 管理指令 ---
def set_global_pause(state):
    conn = get_db_connection()
    if not conn: return "💥 DB Error"
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE settings SET value = %s WHERE key = 'is_paused'", (state,))
            conn.commit()
        status = "暫停" if state == 'true' else "恢復"
        return f"⚙️ 全域回報提醒已 **{status}**。"
    except Exception as e:
        print(f"PAUSE ERROR: {e}", file=sys.stderr)
        return "❌ 設定失敗。"
    finally:
        conn.close()

def test_daily_reminder(group_id):
    if group_id in EXCLUDE_GROUP_IDS:
         return "🚫 測試群組 (Excluded) - 排程器將跳過此處。"
    return "🔔 測試指令 OK！請等待排程器執行或檢查 Log。"

def get_help_message():
    return (
        "🤖 心得分享 Bot 指令一覽 🤖\n\n"
        "--- [ 日常回報 ] ---\n"
        "格式：YYYY.MM.DD [星期幾] 姓名\n"
        "範例：2025.11.14(五)彼得\n"
        "注意：Bot 只會擷取第一行的日期和姓名作為打卡。\n\n"
        "--- [ 名單管理 ] ---\n"
        "▸ 新增人名 [姓名]\n"
        "▸ 刪除人名 [姓名]\n"
        "▸ 查詢名單\n\n"
        "--- [ 系統/測試 ] ---\n"
        "▸ 指令 (或 幫助)\n"
        "▸ 測試排程\n"
        "▸ 暫停回報提醒 / 恢復回報提醒\n"
    )

# --- Webhook ---
@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    except LineBotApiError:
        abort(500)
    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    text = event.message.text
    group_id = None
    if isinstance(event.source, SourceGroup): group_id = event.source.group_id
    elif isinstance(event.source, SourceRoom): group_id = event.source.room_id
    elif isinstance(event.source, SourceUser): group_id = event.source.user_id 
    
    if not group_id or group_id in EXCLUDE_GROUP_IDS: return

    # 預處理
    processed_text = text.strip().replace('（', '(').replace('）', ')')
    first_line = processed_text.split('\n')[0].strip()
    reply = None

    # 指令匹配
    if first_line in ["指令", "幫助", "help"]:
        reply = get_help_message()
    
    elif first_line == "暫停回報提醒": reply = set_global_pause('true')
    elif first_line == "恢復回報提醒": reply = set_global_pause('false')
    elif first_line in ["發送提醒測試", "測試排程"]: reply = test_daily_reminder(group_id)

    match_add = re.match(r"^新增人名[\s　]+(.+)$", first_line)
    if match_add: reply = add_reporter(group_id, match_add.group(1).strip())

    match_del = re.match(r"^刪除人名[\s　]+(.+)$", first_line)
    if match_del: reply = delete_reporter(group_id, match_del.group(1).strip())

    if first_line in ["查詢名單", "查看人員", "名單", "list"]:
        reply = get_reporter_list(group_id)

    # 回報匹配
    match_report = re.match(r"^(\d{4}\.\d{2}\.\d{2})\s*(?:\(.*\))?\s*(.+?)\s*([\s\S]*)", text, re.DOTALL)
    if match_report:
        date_str, name = match_report.group(1), match_report.group(2).strip()
        try:
            r_date = datetime.strptime(date_str, '%Y.%m.%d').date()
            if name: reply = log_report(group_id, r_date, name)
        except ValueError:
            pass 

    if reply:
        try:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        except Exception as e:
            print(f"REPLY ERROR: {e}", file=sys.stderr)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)