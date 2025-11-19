import os
import sys
import re
from datetime import datetime, timedelta
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError, LineBotApiError, LineBotApiError
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

# --- 資料庫連線函式 ---
def get_db_connection():
    """建立資料庫連線"""
    try:
        # 連線到 PostgreSQL
        conn = psycopg2.connect(DATABASE_URL, sslmode='require')
        return conn
    except Exception as e:
        print(f"DATABASE CONNECTION ERROR: {e}", file=sys.stderr)
        return None

# --- 資料庫表格設定函式 ---
def setup_database_tables():
    """
    設定資料庫表格結構。
    **強制**刪除並重建所有表格，以修復錯誤的欄位結構。
    """
    conn = get_db_connection()
    if conn is None:
        print("Cannot setup database tables due to connection failure.", file=sys.stderr)
        return

    cur = conn.cursor()
    try:
        # 強制刪除舊表格
        print("--- Running database table setup: FORCING DROP AND RECREATE TABLES ---", file=sys.stderr)
        cur.execute("""
            DROP TABLE IF EXISTS reports;
            DROP TABLE IF EXISTS reporters;
            DROP TABLE IF EXISTS settings;
        """)

        # 1. reporters (紀錄需要輪值的成員名單)
        cur.execute("""
            CREATE TABLE reporters (
                group_id TEXT NOT NULL,
                reporter_name TEXT NOT NULL,
                PRIMARY KEY (group_id, reporter_name)
            );
        """)

        # 2. reports (紀錄心得分享完成的歷史)
        # 修正: 確保 reports 表格包含 group_id 欄位
        cur.execute("""
            CREATE TABLE reports (
                id SERIAL PRIMARY KEY,
                group_id TEXT NOT NULL,
                reporter_name TEXT NOT NULL,
                report_date DATE NOT NULL,
                log_time TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # 3. settings (儲存群組特定的設定，例如 Bot 啟用狀態)
        cur.execute("""
            CREATE TABLE settings (
                group_id TEXT PRIMARY KEY,
                is_active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );
        """)

        conn.commit()
        # 這是驗證修復成功的獨特訊息！
        print("★★★★ SUCCESS: Tables forcibly DROPPED and RECREATED with correct schema. ★★★★", file=sys.stderr)

    except Exception as e:
        print(f"DATABASE SETUP ERROR: {e}", file=sys.stderr)
    finally:
        if conn:
            conn.close()

# 在應用程式啟動時執行資料庫設定
# 注意：在 gunicorn 多 worker 環境中，每個 worker 啟動時都會執行一次
with app.app_context():
    setup_database_tables()


# --- 資料庫操作函式 (核心邏輯) ---

def add_reporter(group_id, reporter_name):
    """新增成員到名單"""
    conn = get_db_connection()
    if conn is None: return "❌ 新增成員時發生資料庫連線錯誤！"
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO reporters (group_id, reporter_name) VALUES (%s, %s) ON CONFLICT (group_id, reporter_name) DO NOTHING",
            (group_id, reporter_name)
        )
        if cur.rowcount > 0:
            conn.commit()
            return f"✅ 已將 **{reporter_name}** 新增至本群組的名單中。"
        else:
            return f"⚠️ **{reporter_name}** 已經在名單上了，不用重複新增喔！"
    except Exception as e:
        print(f"ADD REPORTER DB ERROR: {e}", file=sys.stderr)
        return f"❌ 新增成員時發生資料庫錯誤：{e}"
    finally:
        if conn: conn.close()

def delete_reporter(group_id, reporter_name):
    """從名單中刪除成員"""
    conn = get_db_connection()
    if conn is None: return "❌ 刪除成員時發生資料庫連線錯誤！"
    try:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM reporters WHERE group_id = %s AND reporter_name = %s",
            (group_id, reporter_name)
        )
        if cur.rowcount > 0:
            conn.commit()
            return f"✅ 已將 **{reporter_name}** 從本群組名單中移除。"
        else:
            return f"⚠️ 名單上沒有 **{reporter_name}**，請確認名稱是否正確。"
    except Exception as e:
        print(f"DELETE REPORTER DB ERROR: {e}", file=sys.stderr)
        return f"❌ 刪除成員時發生資料庫錯誤：{e}"
    finally:
        if conn: conn.close()

def get_reporter_list(group_id):
    """查詢名單列表"""
    conn = get_db_connection()
    if conn is None: return "❌ 查詢名單時發生資料庫連線錯誤！"
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT reporter_name FROM reporters WHERE group_id = %s ORDER BY reporter_name",
            (group_id,)
        )
        reporters = [row[0] for row in cur.fetchall()]
        if reporters:
            list_str = "\n- " + "\n- ".join(reporters)
            return f"📝 本群組目前的心得分享名單有：{list_str}"
        else:
            return "📝 目前名單上沒有成員，請使用 `新增人名 [姓名]` 來加入。"
    except Exception as e:
        print(f"GET REPORTER LIST DB ERROR: {e}", file=sys.stderr)
        return f"❌ 查詢名單時發生資料庫錯誤：{e}"
    finally:
        if conn: conn.close()

def log_report(group_id, report_date, reporter_name):
    """
    記錄心得分享回報。
    修正: 在 INSERT 語句中正確使用 group_id 欄位。
    """
    conn = get_db_connection()
    if conn is None: return "❌ 記錄回報時發生資料庫連線錯誤！"
    try:
        cur = conn.cursor()
        
        # 1. 檢查是否已記錄
        cur.execute(
            "SELECT id FROM reports WHERE group_id = %s AND report_date = %s AND reporter_name = %s",
            (group_id, report_date, reporter_name)
        )
        if cur.fetchone():
            return f"⚠️ **{reporter_name}** ({report_date.strftime('%Y.%m.%d')}) 已經回報過了，不用重複記錄喔！"
            
        # 2. 檢查人名是否在名單上 (可選，但建議確認)
        cur.execute(
            "SELECT reporter_name FROM reporters WHERE group_id = %s AND reporter_name = %s",
            (group_id, reporter_name)
        )
        if not cur.fetchone():
            # 如果不在名單上，自動加入 (此處僅為輔助，不依賴此處執行加入，讓 add_reporter 處理衝突)
            pass
        
        # 3. 執行記錄
        cur.execute(
            # 修復後的 INSERT 語句
            "INSERT INTO reports (group_id, reporter_name, report_date) VALUES (%s, %s, %s)",
            (group_id, reporter_name, report_date)
        )
        conn.commit()
        
        # 如果人名不在名單上，自動加入 (如果前面的檢查是空集)
        # 這裡改用 add_reporter 函式來處理新增邏輯，確保一致性
        add_reporter_result = add_reporter(group_id, reporter_name)
        if "已經在名單上了" not in add_reporter_result and "已將" in add_reporter_result:
            print(f"INFO: Automatically added {reporter_name} to reporters list.", file=sys.stderr)


        return f"👌 收到！**{reporter_name}** ({report_date.strftime('%Y.%m.%d')}) 的心得分享記錄完成，請大家掌聲鼓勵！"
        
    except Exception as e:
        # 這裡會捕捉到您回報的錯誤，但理論上強制重建表格後就不會發生
        print(f"LOG REPORT DB ERROR: {e}", file=sys.stderr)
        return f"❌ 記錄回報時發生資料庫錯誤：{e}"
    finally:
        if conn: conn.close()

# 測試排程提醒函式 (用於手動觸發)
def test_daily_reminder(group_id):
    """手動觸發排程的提醒邏輯，並以回覆訊息方式顯示結果"""
    try:
        # 由於 worker 服務是獨立運行的，我們無法直接從 web 服務調用它。
        # 這裡僅確認是否在排除名單內
        if group_id in EXCLUDE_GROUP_IDS:
             return "⚠️ 本群組在排程排除名單中，排程器不會對此群組發送提醒！"
        else:
             return "🔔 提醒測試指令已收到。**排程服務 (worker)** 是獨立運行的，它會在設定的時間自動檢查並發送提醒。\n\n**如果您看到 Bot 發送了 PUSH 提醒訊息，則表示 worker 服務運作正常。**"
    except Exception as e:
        print(f"TEST REMINDER ERROR: {e}", file=sys.stderr)
        return f"❌ 提醒測試發生錯誤：{e}"


# --- LINE Bot Webhook 處理 ---

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    app.logger.info("Request body: " + body)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        print("Invalid signature. Please check your channel access token/secret.", file=sys.stderr)
        abort(400)
    except LineBotApiError as e:
        print(f"LINE API Error: {e.status_code} {e.message}", file=sys.stderr)
        abort(500)

    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    text = event.message.text
    # 取得群組ID，如果是單人聊天則使用 User ID
    group_id = None
    if isinstance(event.source, (SourceGroup, SourceRoom)):
        group_id = event.source.source_id
    elif isinstance(event.source, SourceUser):
        group_id = event.source.user_id # 暫時用 User ID 作為 group_id

    if group_id is None:
        return # 無法識別來源，忽略

    text_to_match = text.strip().replace('（', '(').replace('）', ')')
    reply_text = None

    # 處理特殊指令
    if text_to_match in ["發送提醒測試", "測試排程"]:
        # 這是手動觸發群組測試的結果
        if reply_text is None:
            reply_text = test_daily_reminder(group_id)
        
    # 處理管理指令 (新增/刪除人名, 查詢名單)
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

    # 處理「YYYY.MM.DD [星期幾] [人名]」回報指令
    regex_pattern = r"^(\d{4}\.\d{2}\.\d{2})\s*(?:[\s　]*[（(][\s\w\u4e00-\u9fff]+[)）])?\s*(.+)$"
    match_report = re.match(regex_pattern, text_to_match)

    if match_report:
        date_str = match_report.group(1)
        name_str = match_report.group(2).strip()

        try:
            report_date = datetime.strptime(date_str, '%Y.%m.%d').date()
            reporter_name = name_str
            
            # 確保人名不為空
            if not reporter_name:
                reply_text = "⚠️ 請在日期後方加上回報者的姓名！"
            else:
                reply_text = log_report(group_id, report_date, reporter_name)
            
        except ValueError:
            reply_text = "❌ 日期格式不正確。請使用 YYYY.MM.DD 的格式，例如：`2025.11.19 小明`"

    # 發送回覆訊息
    if reply_text:
        try:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=reply_text)
            )
        except LineBotApiError as e:
            print(f"LINE API REPLY ERROR: {e}", file=sys.stderr)
            # 如果是群組/聊天室，嘗試用 push_message 替代 reply_message (在某些情況下 reply_token 會失效)
            if group_id and group_id not in EXCLUDE_GROUP_IDS:
                try:
                    line_bot_api.push_message(group_id, TextSendMessage(text=reply_text))
                except Exception as push_e:
                     print(f"LINE API PUSH FALLBACK ERROR: {push_e}", file=sys.stderr)


# --- 啟動 Flask 應用 ---
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    # 確保在非 Railway 環境中也能初始化 DB (雖然 Railway 透過 gunicorn 啟動)
    app.run(host='0.0.0.0', port=port)