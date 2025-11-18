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

# --- 診斷與初始化 ---
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
        conn = psycopg2.connect(DATABASE_URL, sslmode='require')
        return conn
    except Exception as e:
        print(f"DATABASE CONNECTION ERROR: {e}", file=sys.stderr)
        return None

# --- 資料庫操作：新增/刪除/查詢回報人 ---

def add_reporter(group_id, reporter_name):
    conn = get_db_connection()
    if conn is None:
        return "Database connection failed."

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT group_id FROM group_reporters WHERE group_id = %s AND reporter_name = %s;", (group_id, reporter_name))
            if cur.fetchone():
                return f"😉 哎呀，**{reporter_name}** 已經在名單中囉！感謝您的熱情！🔥"

            cur.execute("INSERT INTO group_reporters (group_id, reporter_name) VALUES (%s, %s);", (group_id, reporter_name))
            conn.commit()
            return f"🥳 太棒了！歡迎 **{reporter_name}** 加入回報名單！從今天起一起努力吧！💪"
    except Exception as e:
        conn.rollback()
        print(f"DB ERROR (add_reporter): {e}", file=sys.stderr)
        return f"🚨 資料庫操作失敗: {e}"
    finally:
        if conn: conn.close()

def delete_reporter(group_id, reporter_name):
    conn = get_db_connection()
    if conn is None:
        return "Database connection failed."

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT group_id FROM group_reporters WHERE group_id = %s AND reporter_name = %s;", (group_id, reporter_name))
            if not cur.fetchone():
                return f"🤔 咦？我查了一下，**{reporter_name}** 不在回報人名單上耶。是不是名字打錯了呢？請再檢查一下喔！"

            cur.execute("DELETE FROM group_reporters WHERE group_id = %s AND reporter_name = %s;", (group_id, reporter_name))
            cur.execute("DELETE FROM reports WHERE group_id = %s AND name = %s;", (group_id, reporter_name))

            conn.commit()
            return f"👋 好的，我們已經跟 **{reporter_name}** 說掰掰了，資料庫也順利清空。管理名單完成！🧹"
    except Exception as e:
        conn.rollback()
        print(f"DB ERROR (delete_reporter): {e}", file=sys.stderr)
        return f"🚨 資料庫操作失敗: {e}"
    finally:
        if conn: conn.close()

def get_reporter_list(group_id):
    conn = get_db_connection()
    if conn is None:
        return "Database connection failed."

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT reporter_name FROM group_reporters WHERE group_id = %s ORDER BY reporter_name;", (group_id,))
            reporters = [row[0] for row in cur.fetchall()]
            
            if not reporters:
                return "📋 目前名單空空如也！快來當第一個回報者吧！使用 **新增人名 [人名]** 啟動您的進度追蹤！🚀"
            
            list_text = "⭐ 本團隊回報名單：\n\n"
            list_text += "\n".join([f"🔸 {name}" for name in reporters])
            
            return list_text
    except Exception as e:
        conn.rollback()
        print(f"DB ERROR (get_reporter_list): {e}", file=sys.stderr)
        return f"🚨 資料庫操作失敗: {e}"
    finally:
        if conn: conn.close()

def save_report(group_id, report_date_str, reporter_name):
    conn = get_db_connection()
    if conn is None:
        return "Database connection failed."

    try:
        report_date = datetime.strptime(report_date_str, '%Y.%m.%d').date()
    except ValueError:
        return "📆 日期格式小錯誤！別擔心，請記得使用 **YYYY.MM.DD** 這種格式喔！例如：2025.11.17。"

    try:
        with conn.cursor() as cur:
            # 檢查回報人是否在名單中
            cur.execute("SELECT group_id FROM group_reporters WHERE group_id = %s AND reporter_name = %s;", (group_id, reporter_name))
            if not cur.fetchone():
                return f"🧐 **{reporter_name}** 看起來您還沒加入回報名單呢！請先用 **新增人名 {reporter_name}** 讓我認識您一下喔！😊"

            # 檢查是否重複回報
            cur.execute("SELECT * FROM reports WHERE group_id = %s AND report_date = %s AND name = %s;", (group_id, report_date, reporter_name))
            if cur.fetchone():
                return f"👍 效率超高！**{reporter_name}** {report_date_str} 的回報狀態早已是 **已完成** 囉！不用再操作啦，您休息一下吧！☕"

            # 儲存回報
            cur.execute("INSERT INTO reports (group_id, report_date, name) VALUES (%s, %s, %s);", (group_id, report_date, reporter_name))
            conn.commit()
            return f"✨ 成功！**{reporter_name}** 您今天做得非常棒！{report_date_str} 的進度已完美記錄！💯"
    except Exception as e:
        conn.rollback()
        print(f"DB ERROR (save_report): {e}", file=sys.stderr)
        return f"🚨 資料庫操作失敗: {e}"
    finally:
        if conn: conn.close()

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
    full_text = event.message.text
    text_to_match = full_text.split('\n')[0].strip() # 只匹配第一行指令

    if isinstance(event.source, SourceGroup) or isinstance(event.source, SourceRoom):
        group_id = event.source.group_id if isinstance(event.source, SourceGroup) else event.source.room_id

        reply_text = None

        # 1. 處理管理指令 (新增/刪除人名, 查詢名單)
        match_add = re.match(r"^新增人名[\s　]+(.+)$", text_to_match)
        if match_add:
            reporter_name = match_add.group(1).strip()
            reply_text = add_reporter(group_id, reporter_name)

        match_delete = re.match(r"^刪除人名[\s　]+(.+)$", text_to_match)
        if match_delete:
            reporter_name = match_delete.group(1).strip()
            reply_text = delete_reporter(group_id, reporter_name)

        if text_to_match in ["查詢名單", "查看人員", "名單", "list"]:
            reply_text = get_reporter_list(group_id)

        # 2. 處理「YYYY.MM.DD [星期幾] [人名]」回報指令
        regex_pattern = r"^(\d{4}\.\d{2}\.\d{2})\s*(?:[\s　]*[（(][\s\w\u4e00-\u9fff]+[)）])?\s*(.+)$"
        match_report = re.match(regex_pattern, text_to_match)

        if match_report:
            date_str = match_report.group(1)
            reporter_name = match_report.group(2).strip() 
            reply_text = save_report(group_id, date_str, reporter_name)

        # 統一回覆訊息
        if reply_text:
            try:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
            except Exception as e:
                print(f"LINE REPLY ERROR: {e}", file=sys.stderr)

# --- 啟動 Flask 應用程式 ---
if __name__ == "__main__":
    app.run(debug=True, host='0.0.0.0', port=os.getenv('PORT', 8080))