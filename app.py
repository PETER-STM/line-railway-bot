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

# --- 診斷程式碼 ---
try:
    if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_CHANNEL_SECRET or not DATABASE_URL:
        print("ERROR: Missing required environment variables!", file=sys.stderr)
    else:
        print(f"LINE_SECRET length: {len(LINE_CHANNEL_SECRET)}", file=sys.stderr)
        print(f"LINE_TOKEN length: {len(LINE_CHANNEL_ACCESS_TOKEN)}", file=sys.stderr)
        print(f"DB_URL length: {len(DATABASE_URL)}", file=sys.stderr)
except Exception as e:
    print(f"FATAL INIT ERROR during variable check: {e}", file=sys.stderr)
# --- 診斷程式碼結束 ---

if not LINE_CHANNEL_ACCESS_TOKEN:
    sys.exit("LINE_CHANNEL_ACCESS_TOKEN is missing!")
if not LINE_CHANNEL_SECRET:
    sys.exit("LINE_CHANNEL_SECRET is missing!")

app = Flask(__name__)

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# --- 資料庫連線函式 ---
def get_db_connection():
    try:
        # 強制使用 SSL mode='require'
        conn = psycopg2.connect(DATABASE_URL, sslmode='require')
        return conn
    except Exception as e:
        print(f"DATABASE CONNECTION ERROR: {e}", file=sys.stderr)
        return None

# --- 資料庫操作：新增回報人 (group_reporters 表使用 group_id，無需修改) ---
def add_reporter(group_id, reporter_name):
    conn = get_db_connection()
    if conn is None:
        return "Database connection failed."

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT group_id FROM group_reporters WHERE group_id = %s AND reporter_name = %s;", (group_id, reporter_name))
            if cur.fetchone():
                return f"⚠️ **{reporter_name}** 已經是回報人！"

            cur.execute("INSERT INTO group_reporters (group_id, reporter_name) VALUES (%s, %s);", (group_id, reporter_name))
            conn.commit()
            return f"✅ 已成功新增：**{reporter_name}** 為回報人！"
    except Exception as e:
        conn.rollback()
        print(f"DB ERROR (add_reporter): {e}", file=sys.stderr)
        return f"🚨 資料庫操作失敗: {e}"
    finally:
        conn.close()

# --- 資料庫操作：儲存回報 (reports 表使用 source_id，需要修改) ---
def save_report(group_id, report_date_str, reporter_name):
    conn = get_db_connection()
    if conn is None:
        return "Database connection failed."

    try:
        report_date = datetime.strptime(report_date_str, '%Y.%m.%d').date()
    except ValueError:
        return "⚠️ 日期格式錯誤，請使用 **YYYY.MM.DD** 格式！"

    try:
        with conn.cursor() as cur:
            # 檢查回報人是否在名單中
            cur.execute("SELECT group_id FROM group_reporters WHERE group_id = %s AND reporter_name = %s;", (group_id, reporter_name))
            if not cur.fetchone():
                return f"❌ **{reporter_name}** 不在回報人名單中，請先使用 **新增人名 {reporter_name}** 加入！"

            # 修正 1: 檢查當天是否已回報過 (使用 source_id)
            cur.execute("SELECT * FROM reports WHERE source_id = %s AND report_date = %s AND name = %s;", (group_id, report_date, reporter_name))
            if cur.fetchone():
                return f"⚠️ **{reporter_name}** 已經回報過 {report_date_str} 的記錄了！"

            # 修正 2: 儲存回報 (使用 source_id)
            cur.execute("INSERT INTO reports (source_id, report_date, name) VALUES (%s, %s, %s);", (group_id, report_date, reporter_name))
            conn.commit()
            return f"🎉 **{reporter_name}** 成功回報 {report_date_str}！"
    except Exception as e:
        conn.rollback()
        print(f"DB ERROR (save_report): {e}", file=sys.stderr)
        return f"🚨 資料庫操作失敗: {e}"
    finally:
        conn.close()

# --- Webhook 路由 ---
@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    app.logger.info("Request body: " + body)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        print("Invalid signature. Check your channel secret/token.", file=sys.stderr)
        abort(400)
    except LineBotApiError as e:
        print(f"LINE API Error: {e}", file=sys.stderr)
        abort(500)
    
    return 'OK'

# --- 訊息處理：接收訊息事件 ---
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    text = event.message.text.strip()
    
    if isinstance(event.source, SourceGroup) or isinstance(event.source, SourceRoom):
        group_id = event.source.group_id if isinstance(event.source, SourceGroup) else event.source.room_id

        reply_text = None

        # 1. 處理「新增人名 [人名]」指令
        match_add = re.match(r"^新增人名\s+(.+)$", text)
        if match_add:
            reporter_name = match_add.group(1).strip()
            reply_text = add_reporter(group_id, reporter_name)

        # 2. 處理「YYYY.MM.DD 人名」回報指令
        match_report = re.match(r"^(\d{4}\.\d{2}\.\d{2})\s+(.+)$", text)
        if match_report:
            date_str = match_report.group(1)
            reporter_name = match_report.group(2).strip()
            reply_text = save_report(group_id, date_str, reporter_name)

        # 回覆訊息
        if reply_text:
            try:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
            except Exception as e:
                print(f"LINE REPLY ERROR: {e}", file=sys.stderr)


# --- START SCHEDULER LOGIC ---

# 輔助函數：獲取所有回報人名單
def get_all_reporters(conn):
    cur = conn.cursor()
    cur.execute("SELECT group_id, reporter_name FROM group_reporters ORDER BY group_id;")
    all_reporters = cur.fetchall()
    return all_reporters

# 核心邏輯：發送每日提醒
def send_daily_reminder(line_bot_api):
    conn = get_db_connection()
    if conn is None:
        return "Error: Database connection failed."

    # 設定要檢查的日期 (昨天)
    check_date = datetime.now().date() - timedelta(days=1)
    check_date_str = check_date.strftime('%Y.%m.%d')
    
    print(f"Scheduler running for date: {check_date_str}", file=sys.stderr)

    try:
        all_reporters = get_all_reporters(conn)
        
        groups_to_check = {}
        for group_id, reporter_name in all_reporters:
            if group_id not in groups_to_check:
                groups_to_check[group_id] = []
            groups_to_check[group_id].append(reporter_name)

        # 針對每個群組檢查未回報的人
        for group_id, reporters in groups_to_check.items():
            missing_reports = []
            
            with conn.cursor() as cur:
                for reporter_name in reporters:
                    # 修正 3: 檢查該回報人在該日期是否有報告記錄 (使用 source_id)
                    cur.execute("SELECT name FROM reports WHERE source_id = %s AND report_date = %s AND name = %s;", 
                                (group_id, check_date, reporter_name))
                    
                    if not cur.fetchone():
                        missing_reports.append(reporter_name)

            # 如果有未回報的人，則發送提醒
            if missing_reports:
                message_text = f"🚨 **{check_date_str}** 回報提醒！以下成員尚未回報：\n\n"
                message_text += "\n".join([f"👉 {name}" for name in missing_reports])
                message_text += "\n\n請儘快回報！"
                
                try:
                    line_bot_api.push_message(group_id, TextSendMessage(text=message_text))
                    print(f"Sent reminder to group {group_id} for {len(missing_reports)} missing reports.", file=sys.stderr)
                except LineBotApiError as e:
                    print(f"LINE API PUSH ERROR to {group_id}: {e}", file=sys.stderr)
                    
    except Exception as e:
        # 捕捉並打印錯誤訊息
        print(f"SCHEDULER DB ERROR: {e}", file=sys.stderr)
        # 返回錯誤訊息給瀏覽器
        return f"Error during schedule processing: {e}"
    finally:
        conn.close()
    
    return "Scheduler execution finished successfully."


# --- 新增的排程觸發路由 ---
@app.route("/run_scheduler")
def run_scheduler_endpoint():
    result = send_daily_reminder(line_bot_api)
    return result

# --- END SCHEDULER LOGIC ---


# --- 啟動 Flask 應用程式 ---
if __name__ == "__main__":
    app.run(debug=True, host='0.0.0.0', port=os.getenv('PORT', 8080))