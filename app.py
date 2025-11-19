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
    """
    conn = get_db_connection()
    if conn is None:
        print("Cannot setup database tables due to connection failure.", file=sys.stderr)
        return

    cur = conn.cursor()
    try:
        # 確保表格存在 (這裡假設已經存在，但為了健壯性，可以再次執行檢查或創建)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS reporters (
                group_id TEXT NOT NULL,
                reporter_name TEXT NOT NULL,
                PRIMARY KEY (group_id, reporter_name)
            );
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS reports (
                id SERIAL PRIMARY KEY,
                group_id TEXT NOT NULL,
                reporter_name TEXT NOT NULL,
                report_date DATE NOT NULL,
                report_content TEXT,
                log_time TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                group_id TEXT PRIMARY KEY,
                is_active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );
        """)

        conn.commit()
        print("INFO: Database tables checked/created.", file=sys.stderr)

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
            # 新增人名 (成功)
            return f"🎉 好嘞～ {reporter_name} 已成功加入名單！\n\n（逃不掉了，祝他順利回報。）"
        else:
            # 新增人名 (重複)
            return f"🤨 {reporter_name} 早就在名單裡面坐好坐滿了，\n\n你該不會…忘記上一次也加過吧？"
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
            # 刪除人名 (成功)
            return f"🗑️ {reporter_name} 已從名單中被溫柔移除。\n\n（放心，我沒有把人綁走，只是移出名單。）"
        else:
            # 刪除人名 (未找到)
            return f"❓名單裡根本沒有 {reporter_name} 啊！\n\n是不是名字打錯，還是你其實不想他回報？"
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
    記錄心得分享回報，只儲存簡單的打卡資訊。
    """
    conn = get_db_connection()
    if conn is None: return DB_ERROR_MSG
    try:
        cur = conn.cursor()
        date_str = report_date.strftime('%Y.%m.%d')
        
        # 1. 檢查是否已記錄 (防止重複打卡)
        cur.execute(
            "SELECT id FROM reports WHERE group_id = %s AND report_date = %s AND reporter_name = %s",
            (group_id, report_date, reporter_name)
        )
        if cur.fetchone():
            # 記錄回報 (重複記錄)
            return f"⚠️ {reporter_name} ({date_str}) 今天已經回報過了！\n\n別想靠重複交作業刷存在感，我看的很清楚 👀"
            
        # 2. 準備簡化內容 for report_content (只記錄打卡，忽略詳細日報)
        simple_content = f"打卡紀錄: {date_str} {reporter_name} (內容已省略)"
        
        # 3. 執行記錄 (使用 simple_content)
        cur.execute(
            "INSERT INTO reports (group_id, reporter_name, report_date, report_content) VALUES (%s, %s, %s, %s)",
            (group_id, reporter_name, report_date, simple_content)
        )
        conn.commit()
        
        # 自動將人名加入名單（如果不在）
        # 這裡不返回 add_reporter 的結果，確保返回的是 log_report 的結果
        temp_result = add_reporter(group_id, reporter_name) 

        # 記錄回報 (成功)
        return f"👌 收到！{reporter_name} ({date_str}) 的心得已成功登入檔案。\n\n（今天有乖，給你一個隱形貼紙 ⭐）"
        
    except Exception as e:
        print(f"LOG REPORT DB ERROR: {e}", file=sys.stderr)
        return DB_ERROR_MSG
    finally:
        if conn: conn.close()

# --- NEW: 手動測試提醒函式 (核心更新) ---

def run_manual_reminder_test(group_id):
    """
    手動觸發單一群組的提醒邏輯。
    執行資料庫檢查，並直接向該群組發送實際的催繳訊息。
    """
    if group_id in EXCLUDE_GROUP_IDS:
        # 測試排程 (已排除群組)
        return "🚫 這個群組在「排除名單」裡，\n\n排程器看到這邊會自動裝死，不會發任何提醒。"

    conn = get_db_connection()
    if conn is None:
        return DB_ERROR_MSG
    
    cur = conn.cursor()
    # 檢查日期為今天 (UTC time)
    today = datetime.utcnow().date()
    date_str = today.strftime('%Y.%m.%d')
    
    try:
        # 1. 取得該群組所有成員名單
        cur.execute(
            "SELECT reporter_name FROM reporters WHERE group_id = %s",
            (group_id,)
        )
        all_reporters = [row[0] for row in cur.fetchall()]

        # 2. 取得該群組今天已回報的成員名單
        cur.execute(
            "SELECT reporter_name FROM reports WHERE group_id = %s AND report_date = %s",
            (group_id, today)
        )
        reported_reporters = set(row[0] for row in cur.fetchall())

        # 3. 找出未回報的成員
        missing_reports = [name for name in all_reporters if name not in reported_reporters]
        
        if not all_reporters:
            return "🤷 名單空空如也，沒有人可以提醒！\n\n（請先用 `新增人名 [姓名]` 把人拉進來吧。）"

        if missing_reports:
            # 4. 準備提醒訊息 (使用活潑幽默模板)
            if len(missing_reports) == 1:
                # 單人未回報
                reporter_name = missing_reports[0]
                message_text = (
                    f"🔔 心得分享提醒 🔔\n"
                    f"今天快截止囉～\n\n"
                    f"目前還沒收到 {reporter_name} 的回報 ({date_str})。\n"
                    f"兄弟姊妹，別再拖了，\n\n"
                    f"再不回報我都要先幫你寫一篇了 😏"
                )
            else:
                # 多人未回報
                list_of_names = "\n".join(missing_reports)
                message_text = (
                    f"📢 心得分享催繳大隊報到 📢\n"
                    f"以下 VIP 仍未交心得：\n\n"
                    f"{list_of_names}\n\n"
                    f"大家快來補交吧～\n\n"
                    f"不要逼系統變成奧客催款模式 😌"
                )
            
            # 5. 發送實際的 PUSH 提醒到該群組
            line_bot_api.push_message(group_id, TextSendMessage(text=message_text))
            
            # 6. 返回一個確認訊息給使用者
            missing_names_str = '、'.join(missing_reports)
            return f"🔔 測試指令 OK！\n\n已成功對以下 {len(missing_reports)} 位勇者發送催繳提醒：\n{missing_names_str}\n\n（請檢查群組訊息，確認系統運作正常。）"
        else:
            # 名單乾淨
            return "✅ 測試指令 OK！\n\n不過名單很乾淨，今天沒人欠作業喔！\n\n（大家都很乖，不給你催繳的機會。）"

    except LineBotApiError as e:
        print(f"MANUAL TEST PUSH ERROR to {group_id}: {e}", file=sys.stderr)
        # 即使推播失敗，也要給使用者一個友善的回覆
        return f"🚨 測試發送 LINE API 錯誤！\n\n雖然資料庫檢查正常，但訊息推播失敗：{e.status_code}。\n\n（系統被 LINE 擋住了，請找管理員確認權限。）"
    except Exception as e:
        print(f"MANUAL TEST DB/Logic ERROR: {e}", file=sys.stderr)
        return DB_ERROR_MSG
    finally:
        if conn: conn.close()

def get_help_message():
    """返回 Bot 的所有可用指令列表"""
    return (
        "🤖 心得分享 Bot 指令一覽 🤖\n\n"
        "--- [ 日常回報 (只記錄打卡) ] ---\n"
        "格式：YYYY.MM.DD [星期幾] 姓名\n"
        "範例：2025.12.31 Peter\n"
        "範例：2025.11.14(五)彼得\n"
        "**注意：** Bot 只會擷取日期和姓名作為打卡紀錄，**完整日報內容將不會被儲存**。\n\n"
        "--- [ 名單管理 ] ---\n"
        "▸ 新增人名 [姓名]\n"
        "▸ 刪除人名 [姓名]\n"
        "▸ 查詢名單 (別名：查看人員, 名單, list)\n\n"
        "--- [ 系統/測試 ] ---\n"
        "▸ 指令 (或 幫助, help)\n"
        "功能：顯示此列表。\n"
        "▸ **測試排程** (或 **發送提醒測試**)\n"
        "功能：**立即觸發**催繳檢查，並將實際提醒訊息推播到此群組/聊天室。\n"
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
    text_processed = text.strip().replace('（', '(').replace('）', ')')
    reply_text = None
    
    # --- 處理幫助與測試指令 (NEW: 執行實際催繳邏輯) ---
    if text_processed in ["指令", "幫助", "help"]:
        reply_text = get_help_message()

    if text_processed in ["發送提醒測試", "測試排程"]:
        if reply_text is None:
            # 呼叫新的手動測試函式，它會執行檢查並 push 訊息
            reply_text = run_manual_reminder_test(group_id)
        
    # 處理管理指令 (新增/刪除人名, 查詢名單)
    match_add = re.match(r"^新增人名[\s　]+(.+)$", text_processed)
    if match_add:
        reporter_name = match_add.group(1).strip()
        reply_text = add_reporter(group_id, reporter_name)

    match_delete = re.match(r"^刪除人名[\s　]+(.+)$", text_processed)
    if match_delete:
        reporter_name = match_delete.group(1).strip()
        reply_text = delete_reporter(group_id, reporter_name)

    if text_processed in ["查詢名單", "查看人員", "名單", "list"]:
        reply_text = get_reporter_list(group_id)

    # 處理「YYYY.MM.DD [星期幾] [人名]」回報指令
    # Regex 僅用於擷取第一行的人名和日期
    regex_pattern = r"^(\d{4}\.\d{2}\.\d{2})\s*(\(.*\))?\s*([^\n]+)"
    match_report = re.match(regex_pattern, text) # 對原始 text 進行匹配

    if match_report:
        date_str = match_report.group(1)
        
        # 人名是第三個捕獲組
        name_str = match_report.group(3).strip()

        try:
            report_date = datetime.strptime(date_str, '%Y.%m.%d').date()
            reporter_name = name_str
            
            # 確保人名不為空
            if not reporter_name:
                # 記錄回報 (人名遺失)
                reply_text = "⚠️ 日期後面請記得加上人名，不然我不知道誰交的啊！\n\n（你總不會想讓我自己猜吧？）"
            else:
                # 呼叫 log_report，只記錄打卡資訊
                reply_text = log_report(group_id, report_date, reporter_name)
            
        except ValueError:
            # 記錄回報 (日期格式錯誤)
            reply_text = "❌ 日期長得怪怪的。\n\n請用標準格式：YYYY.MM.DD 姓名\n\n（小數點不是你的自由發揮。）"

    # 發送回覆訊息 (這是對使用者的指令回覆，不是催繳訊息)
    if reply_text:
        try:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=reply_text)
            )
        except LineBotApiError as e:
            # 如果 reply_message 失敗，嘗試 push_message (例如：超過 3 秒回覆期限)
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