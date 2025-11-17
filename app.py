import os
import sys
import re
from datetime import datetime
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError, LineBotApiError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, SourceGroup, SourceRoom, SourceUser
import psycopg2

# --- 環境變數設定 ---
# 確保這些變數存在於 Railway 環境變數中
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
DATABASE_URL = os.environ.get('DATABASE_URL')

# --- 診斷程式碼 (用於檢查環境變數是否讀取成功) ---
# 如果程式碼在初始化時崩潰，這些 print 語句會幫助我們診斷問題
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

# 檢查變數，如果缺少則讓程式崩潰以顯示明確錯誤
if not LINE_CHANNEL_ACCESS_TOKEN:
    sys.exit("LINE_CHANNEL_ACCESS_TOKEN is missing!")
if not LINE_CHANNEL_SECRET:
    sys.exit("LINE_CHANNEL_SECRET is missing!")

app = Flask(__name__)

# 初始化 LINE Bot API 和 Handler
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# --- 資料庫連線函式 ---
def get_db_connection():
    try:
        conn = psycopg2.connect(DATABASE_URL)
        return conn
    except Exception as e:
        # 在連線失敗時打印錯誤到日誌中
        print(f"DATABASE CONNECTION ERROR: {e}", file=sys.stderr)
        # 讓應用程序在啟動時保持活動，但資料庫操作會失敗
        return None

# --- 資料庫操作：新增回報人 ---
def add_reporter(group_id, reporter_name):
    conn = get_db_connection()
    if conn is None:
        return "Database connection failed."

    try:
        with conn.cursor() as cur:
            # 檢查是否已存在
            cur.execute("SELECT group_id FROM group_reporters WHERE group_id = %s AND reporter_name = %s;", (group_id, reporter_name))
            if cur.fetchone():
                return f"⚠️ **{reporter_name}** 已經是回報人！"

            # 插入新回報人
            cur.execute("INSERT INTO group_reporters (group_id, reporter_name) VALUES (%s, %s);", (group_id, reporter_name))
            conn.commit()
            return f"✅ 已成功新增：**{reporter_name}** 為回報人！"
    except Exception as e:
        conn.rollback()
        print(f"DB ERROR (add_reporter): {e}", file=sys.stderr)
        return f"🚨 資料庫操作失敗: {e}"
    finally:
        conn.close()

# --- 資料庫操作：儲存回報 ---
def save_report(group_id, report_date_str, reporter_name):
    conn = get_db_connection()
    if conn is None:
        return "Database connection failed."

    try:
        # 轉換日期格式為 PostgreSQL 接受的格式
        report_date = datetime.strptime(report_date_str, '%Y.%m.%d').date()
    except ValueError:
        return "⚠️ 日期格式錯誤，請使用 **YYYY.MM.DD** 格式！"

    try:
        with conn.cursor() as cur:
            # 檢查回報人是否在名單中
            cur.execute("SELECT group_id FROM group_reporters WHERE group_id = %s AND reporter_name = %s;", (group_id, reporter_name))
            if not cur.fetchone():
                return f"❌ **{reporter_name}** 不在回報人名單中，請先使用 **新增人名 {reporter_name}** 加入！"

            # 檢查當天是否已回報過
            cur.execute("SELECT * FROM reports WHERE group_id = %s AND report_date = %s AND name = %s;", (group_id, report_date, reporter_name))
            if cur.fetchone():
                return f"⚠️ **{reporter_name}** 已經回報過 {report_date_str} 的記錄了！"

            # 儲存回報
            cur.execute("INSERT INTO reports (group_id, report_date, name) VALUES (%s, %s, %s);", (group_id, report_date, reporter_name))
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
    
    # 僅處理群組/聊天室訊息，如果需要個人聊天也處理，請修改此處邏輯
    if isinstance(event.source, SourceGroup) or isinstance(event.source, SourceRoom):
        # 獲取群組 ID (V2 語法)
        group_id = event.source.group_id if isinstance(event.source, SourceGroup) else event.source.room_id

        reply_text = None

        # 1. 處理「新增人名 [人名]」指令
        match_add = re.match(r"^新增人名\s+(.+)$", text)
        if match_add:
            reporter_name = match_add.group(1).strip()
            reply_text = add_reporter(group_id, reporter_name)

        # 2. 處理「YYYY.MM.DD 人名」回報指令
        # 匹配日期格式 YYYY.MM.DD 後跟著人名
        match_report = re.match(r"^(\d{4}\.\d{2}\.\d{2})\s+(.+)$", text)
        if match_report:
            date_str = match_report.group(1)
            reporter_name = match_report.group(2).strip()
            reply_text = save_report(group_id, date_str, reporter_name)

        # 3. 處理「查詢名單」指令 (可選)
        if text == "查詢名單":
            # 這裡可以加入查詢所有回報人的邏輯，但為了穩定性，暫時省略，
            # 避免因 DB 連線問題導致應用程式崩潰。
            pass

        # 回覆訊息
        if reply_text:
            try:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
            except Exception as e:
                print(f"LINE REPLY ERROR: {e}", file=sys.stderr)


# --- 啟動 Flask 應用程式 ---
if __name__ == "__main__":
    app.run(debug=True, host='0.0.0.0', port=os.getenv('PORT', 8080))