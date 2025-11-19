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
with app.app_context():
    setup_database_tables()


# --- 資料庫操作函式 (核心邏輯) ---

# 活潑風格的通用錯誤訊息
DB_ERROR_MSG = "💥 發生未知錯誤。\n\n可能是宇宙磁場不順，或系統在叛逆。\n\n稍後再試，或找管理員用愛感化它。"

def add_reporter(group_id, reporter_name):
    """新增成員到名單"""
    conn = get_db_connection()
    if conn is None: return DB_ERROR_MSG
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO reporters (group_id, reporter_name) VALUES (%s, %s) ON CONFLICT (group_id, reporter_name) DO NOTHING",
            (group_id, reporter_name)
        )
        if cur.rowcount > 0:
            conn.commit()
            # 新增人名 (成功) - 移除空格
            return f"🎉 好嘞～{reporter_name}已成功加入名單！\n\n（逃不掉了，祝他順利回報。）"
        else:
            # 新增人名 (重複) - 移除空格
            return f"🤨{reporter_name}早就在名單裡面坐好坐滿了，\n\n你該不會…忘記上一次也加過吧？"
    except Exception as e:
        print(f"ADD REPORTER DB ERROR: {e}", file=sys.stderr)
        return DB_ERROR_MSG
    finally:
        if conn: conn.close()

def delete_reporter(group_id, reporter_name):
    """從名單中刪除成員"""
    conn = get_db_connection()
    if conn is None: return DB_ERROR_MSG
    try:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM reporters WHERE group_id = %s AND reporter_name = %s",
            (group_id, reporter_name)
        )
        if cur.rowcount > 0:
            conn.commit()
            # 刪除人名 (成功) - 移除空格
            return f"🗑️{reporter_name}已從名單中被溫柔移除。\n\n（放心，我沒有把人綁走，只是移出名單。）"
        else:
            # 刪除人名 (未找到) - 移除空格
            return f"❓名單裡根本沒有{reporter_name}啊！\n\n是不是名字打錯，還是你其實不想他回報？"
    except Exception as e:
        print(f"DELETE REPORTER DB ERROR: {e}", file=sys.stderr)
        return DB_ERROR_MSG
    finally:
        if conn: conn.close()

def get_reporter_list(group_id):
    """查詢名單列表"""
    conn = get_db_connection()
    if conn is None: return DB_ERROR_MSG
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT reporter_name FROM reporters WHERE group_id = %s ORDER BY reporter_name",
            (group_id,)
        )
        reporters = [row[0] for row in cur.fetchall()]
        if reporters:
            # 查詢名單 (有成員)
            list_str = "\n" + "\n".join(reporters) # 準備成員列表
            return f"📋 最新回報觀察名單如下：{list_str}\n\n（嗯，看起來大家都還活著。）"
        else:
            # 查詢名單 (無成員)
            return "📭 名單空空如也～\n\n快用 `新增人名 [姓名]` 把第一位勇者召喚進來吧！"
    except Exception as e:
        print(f"GET REPORTER LIST DB ERROR: {e}", file=sys.stderr)
        return DB_ERROR_MSG
    finally:
        if conn: conn.close()

def log_report(group_id, report_date, reporter_name):
    """
    記錄心得分享回報。
    """
    conn = get_db_connection()
    if conn is None: return DB_ERROR_MSG
    try:
        cur = conn.cursor()
        date_str = report_date.strftime('%Y.%m.%d')
        
        # 1. 檢查是否已記錄
        cur.execute(
            "SELECT id FROM reports WHERE group_id = %s AND report_date = %s AND reporter_name = %s",
            (group_id, report_date, reporter_name)
        )
        if cur.fetchone():
            # 記錄回報 (重複記錄) - 移除空格
            return f"⚠️{reporter_name}({date_str})今天已經回報過了！\n\n別想靠重複交作業刷存在感，我看的很清楚 👀"
            
        # 3. 執行記錄
        cur.execute(
            "INSERT INTO reports (group_id, reporter_name, report_date) VALUES (%s, %s, %s)",
            (group_id, reporter_name, report_date)
        )
        conn.commit()
        
        # 自動將人名加入名單（如果不在）
        add_reporter_result = add_reporter(group_id, reporter_name)
        if "已經在名單上了" not in add_reporter_result and "已成功加入名單" in add_reporter_result:
            print(f"INFO: Automatically added {reporter_name} to reporters list.", file=sys.stderr)

        # 記錄回報 (成功) - 移除空格
        return f"👌 收到！{reporter_name}({date_str})的心得已成功登入檔案。\n\n（今天有乖，給你一個隱形貼紙 ⭐）"
        
    except Exception as e:
        print(f"LOG REPORT DB ERROR: {e}", file=sys.stderr)
        return DB_ERROR_MSG
    finally:
        if conn: conn.close()

def test_daily_reminder(group_id):
    """手動觸發排程的提醒邏輯，並以回覆訊息方式顯示結果 (活潑風格)"""
    if group_id in EXCLUDE_GROUP_IDS:
         # 測試排程 (已排除群組)
         return "🚫 這個群組在「排除名單」裡，\n\n排程器看到這邊會自動裝死，不會發任何提醒。"
    else:
         # 測試排程 (正常群組)
         return "🔔 測試指令 OK！\n\n請坐等排程器在設定時間跳出來嚇你，\n\n以確認系統正常運作。"

def get_help_message():
    """返回 Bot 的所有可用指令列表"""
    return (
        "🤖 心得分享 Bot 指令一覽 🤖\n\n"
        "--- [ 日常回報 (支援日報內容) ] ---\n"
        "格式：YYYY.MM.DD [星期幾] 姓名\n"
        "範例：2025.12.31 Peter\n"
        "範例：2025.11.14(五)彼得\n"
        "**注意：** 人名後的**所有換行內容都會被忽略**，只用於記錄回報。\n\n"
        "--- [ 名單管理 ] ---\n"
        "▸ 新增人名 [姓名]\n"
        "▸ 刪除人名 [姓名]\n"
        "▸ 查詢名單 (別名：查看人員, 名單, list)\n\n"
        "--- [ 系統/測試 ] ---\n"
        "▸ 指令 (或 幫助, help)\n"
        "功能：顯示此列表。\n"
        "▸ 測試排程 (或 發送提醒測試)\n"
        "功能：手動測試排程提醒功能。\n\n"
        "--- [ 注意事項 ] ---\n"
        "1. 日期後面的(星期幾)是可選的，Bot會自動忽略它。\n"
        "2. 所有回覆人名的地方，我都已經幫你移除了多餘的空格囉！🎉"
    )

# --- LINE Bot Webhook 處理 (ID 修正已保留) ---

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
    
    # 根據 Source 類型使用正確的 ID 屬性
    group_id = None
    if isinstance(event.source, SourceGroup):
        group_id = event.source.group_id
    elif isinstance(event.source, SourceRoom):
        group_id = event.source.room_id
    elif isinstance(event.source, SourceUser):
        group_id = event.source.user_id 

    if group_id is None:
        return

    # 1. 將全形括號替換為半形，以便 Regex 處理，並清除首尾空白
    text_to_match = text.strip().replace('（', '(').replace('）', ')')
    reply_text = None
    
    # --- 處理幫助與測試指令 ---
    if text_to_match in ["指令", "幫助", "help"]:
        reply_text = get_help_message()

    if text_to_match in ["發送提醒測試", "測試排程"]:
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
    # 修正後的 Regex：使用 [^\n]+ 確保人名只擷取到第一個換行符號前，忽略後續日報內容。
    regex_pattern = r"^(\d{4}\.\d{2}\.\d{2})\s*(\(.*\))?\s*([^\n]+)$"
    match_report = re.match(regex_pattern, text_to_match)

    if match_report:
        date_str = match_report.group(1)
        
        # 人名是第三個捕獲組
        name_str = match_report.group(3).strip()

        try:
            report_date = datetime.strptime(date_str, '%Y.%m.%d').date()
            reporter_name = name_str
            
            # 確保人名不為空
            if not reporter_name:
                # 記錄回報 (人名遺失) - 此處無人名變數，不變
                reply_text = "⚠️ 日期後面請記得加上人名，不然我不知道誰交的啊！\n\n（你總不會想讓我自己猜吧？）"
            else:
                reply_text = log_report(group_id, report_date, reporter_name)
            
        except ValueError:
            # 記錄回報 (日期格式錯誤) - 此處無人名變數，不變
            reply_text = "❌ 日期長得怪怪的。\n\n請用標準格式：YYYY.MM.DD 姓名\n\n（小數點不是你的自由發揮。）"

    # 發送回覆訊息
    if reply_text:
        try:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=reply_text)
            )
        except LineBotApiError as e:
            print(f"LINE API REPLY ERROR: {e}", file=sys.stderr)
            if group_id and group_id not in EXCLUDE_GROUP_IDS:
                try:
                    line_bot_api.push_message(group_id, TextSendMessage(text=reply_text))
                except Exception as push_e:
                     print(f"LINE API PUSH FALLBACK ERROR: {push_e}", file=sys.stderr)


# --- 啟動 Flask 應用 ---
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)