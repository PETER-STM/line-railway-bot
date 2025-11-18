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
        return psycopg2.connect(DATABASE_URL)
    except Exception as e:
        print(f"DATABASE CONNECTION ERROR: {e}", file=sys.stderr)
        return None

# --- 資料庫初始化函式 ---
def ensure_tables_exist():
    """檢查並建立所有必需的資料庫表 (group_reporters, reports, settings)"""
    conn = get_db_connection()
    if conn is None:
        print("ERROR: Failed to establish database connection for table creation.", file=sys.stderr)
        return False
    
    cur = conn.cursor()
    success = True
    try:
        # 1. group_reporters 表 (存放群組ID和成員姓名)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS group_reporters (
                group_id VARCHAR(255) NOT NULL,
                reporter_name VARCHAR(255) NOT NULL,
                PRIMARY KEY (group_id, reporter_name)
            );
        """)
        
        # 2. reports 表 (存放每日回報紀錄)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS reports (
                group_id VARCHAR(255) NOT NULL,
                report_date DATE NOT NULL,
                reporter_name VARCHAR(255) NOT NULL,
                PRIMARY KEY (group_id, report_date, reporter_name)
            );
        """)
        
        # 3. settings 表 (存放全域設定，例如提醒是否暫停)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key VARCHAR(255) PRIMARY KEY,
                value VARCHAR(255) NOT NULL
            );
        """)
        
        conn.commit()
        print("INFO: Database tables checked/created successfully.", file=sys.stderr)

    except Exception as e:
        print(f"DATABASE INITIALIZATION ERROR: {e}", file=sys.stderr)
        conn.rollback()
        success = False
    finally:
        if conn: conn.close()
    
    return success

# --- 全域設定函式 ---
def set_global_pause_state(is_paused: bool) -> str:
    """設定全域提醒暫停狀態 (True: 暫停, False: 恢復)"""
    conn = get_db_connection()
    if conn is None:
        return "🚨 資料庫連線失敗！"

    cur = conn.cursor()
    state_value = 'true' if is_paused else 'false'
    reply_prefix = "⏸️ 提醒已暫停！" if is_paused else "▶️ 提醒已恢復！"
    
    try:
        cur.execute("""
            INSERT INTO settings (key, value) VALUES ('is_paused', %s)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;
        """, (state_value,))
        
        conn.commit()
        return f"{reply_prefix} 每日心得催交通知已設為：{'暫停' if is_paused else '恢復'}。"

    except Exception as e:
        print(f"DB ERROR (set_global_pause_state): {e}", file=sys.stderr)
        return f"🚨 資料庫操作失敗: {e}"
    finally:
        if conn: conn.close()

def is_global_pause_state() -> bool:
    """檢查全域提醒是否暫停"""
    conn = get_db_connection()
    if conn is None:
        return True # 資料庫無法連線時，視為暫停，避免錯誤

    cur = conn.cursor()
    try:
        # 注意: 如果 settings 表還沒建立，這裡會拋出 'relation "settings" does not exist' 錯誤
        cur.execute("SELECT value FROM settings WHERE key = 'is_paused';")
        result = cur.fetchone()
        if result and result[0].lower() == 'true':
            return True
        return False
    except Exception as e:
        # 如果發生錯誤 (通常是表不存在)，我們在這裡捕獲並記錄
        # 由於我們在啟動時會確保表存在，這個錯誤應該只會在初始化失敗時發生
        print(f"DB CHECK ERROR (is_global_pause_state): {e}", file=sys.stderr)
        return False # 發生錯誤時，讓它嘗試繼續運行 (如果可以)
    finally:
        if conn: conn.close()
        
# --- 新增/刪除人名函式 ---
def add_reporter(group_id, reporter_name):
    """將人名加入群組名單"""
    conn = get_db_connection()
    if conn is None: return "🚨 資料庫連線失敗！"
    
    cur = conn.cursor()
    try:
        cur.execute("INSERT INTO group_reporters (group_id, reporter_name) VALUES (%s, %s) ON CONFLICT DO NOTHING;", 
                    (group_id, reporter_name))
        conn.commit()
        if cur.rowcount > 0:
            return f"✅ 已成功將「{reporter_name}」加入本群組追蹤名單。"
        else:
            return f"ℹ️ 「{reporter_name}」已在名單中，無需重複新增。"
    except Exception as e:
        print(f"DB ERROR (add_reporter): {e}", file=sys.stderr)
        return f"🚨 資料庫操作失敗: {e}"
    finally:
        if conn: conn.close()

def delete_reporter(group_id, reporter_name):
    """從群組名單中刪除人名"""
    conn = get_db_connection()
    if conn is None: return "🚨 資料庫連線失敗！"
    
    cur = conn.cursor()
    try:
        # 1. 先刪除該人名所有的歷史回報紀錄
        cur.execute("DELETE FROM reports WHERE group_id = %s AND reporter_name = %s;", 
                    (group_id, reporter_name))
        # 2. 再從名單中刪除該人名
        cur.execute("DELETE FROM group_reporters WHERE group_id = %s AND reporter_name = %s;", 
                    (group_id, reporter_name))
        conn.commit()

        if cur.rowcount > 0:
            return f"🗑️ 已成功將「{reporter_name}」從追蹤名單中移除，同時刪除了他的所有回報紀錄。"
        else:
            return f"ℹ️ 名單中找不到「{reporter_name}」，無法刪除。"
    except Exception as e:
        print(f"DB ERROR (delete_reporter): {e}", file=sys.stderr)
        return f"🚨 資料庫操作失敗: {e}"
    finally:
        if conn: conn.close()

def get_reporter_list(group_id):
    """查詢群組名單"""
    conn = get_db_connection()
    if conn is None: return "🚨 資料庫連線失敗！"

    cur = conn.cursor()
    try:
        cur.execute("SELECT reporter_name FROM group_reporters WHERE group_id = %s ORDER BY reporter_name;", (group_id,))
        reporters = cur.fetchall()
        
        if not reporters:
            return "📋 本群組的心得追蹤名單目前為空。\n\n💡 請輸入 `新增人名 [姓名]` 來加入成員！"
        
        name_list = "\n🔸 ".join([r[0] for r in reporters])
        return f"⭐ 本團隊回報名單：\n\n🔸 {name_list}\n\n📝 **心得回報格式：**\n`今天 [姓名]` 或 `2025.11.18 [姓名]`"
    except Exception as e:
        print(f"DB ERROR (get_reporter_list): {e}", file=sys.stderr)
        return f"🚨 資料庫操作失敗: {e}"
    finally:
        if conn: conn.close()

# --- 心得回報函式 ---
def record_report(group_id, date_str, reporter_name):
    """記錄特定日期的回報"""
    conn = get_db_connection()
    if conn is None: return "🚨 資料庫連線失敗！"

    # 檢查人名是否在名單中
    cur = conn.cursor()
    try:
        cur.execute("SELECT 1 FROM group_reporters WHERE group_id = %s AND reporter_name = %s;", (group_id, reporter_name))
        if cur.fetchone() is None:
            return f"⚠️ 找不到「{reporter_name}」！請先用 `新增人名 {reporter_name}` 將他加入名單。"
            
        # 嘗試解析日期
        try:
            report_date = datetime.strptime(date_str, '%Y.%m.%d').date()
        except ValueError:
            return "🚨 日期格式錯誤！請使用 YYYY.MM.DD 格式 (例如: 2025.11.18)。"

        # 記錄回報
        cur.execute("""
            INSERT INTO reports (group_id, report_date, reporter_name) 
            VALUES (%s, %s, %s)
            ON CONFLICT (group_id, report_date, reporter_name) DO NOTHING;
        """, (group_id, report_date, reporter_name))
        conn.commit()
        
        if cur.rowcount > 0:
            return f"🎉 成功！已記錄「{reporter_name}」在 {report_date} 的心得回報。"
        else:
            return f"ℹ️ 「{reporter_name}」在 {report_date} 的心得回報已存在，無需重複記錄。"

    except Exception as e:
        print(f"DB ERROR (record_report): {e}", file=sys.stderr)
        return f"🚨 資料庫操作失敗: {e}"
    finally:
        if conn: conn.close()

# --- 模擬排程器發送提醒 ---
def test_daily_reminder(group_id):
    """
    這個函式用於模擬 scheduler.py 的邏輯，但只針對單一群組ID。
    它檢查 '昨天' 的回報狀態。
    """
    if is_global_pause_state():
        # 如果 is_global_pause_state 拋出錯誤 (因為表不存在)，它會返回 False，但我們在指令層應該要處理
        # 為了安全，這裡重新檢查一次全域暫停狀態，如果資料庫連線失敗，則直接返回錯誤
        conn = get_db_connection()
        if conn is None: return "🚨 測試失敗：資料庫連線失敗！"
        
        # 再次檢查全域狀態，這次如果為 True 則返回暫停訊息
        if is_global_pause_state_internal(conn):
            conn.close()
            return "⏸️ 全域提醒目前處於【暫停】狀態，測試功能無法執行。請先使用 `恢復回報提醒`。"
        conn.close()
        
    conn = get_db_connection()
    if conn is None: return "🚨 測試失敗：資料庫連線失敗！"

    # 檢查昨天的日期
    yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
    yesterday_display = (datetime.now() - timedelta(days=1)).strftime('%Y.%m.%d')
    
    cur = conn.cursor()
    try:
        # 1. 取得該群組所有應回報的人員
        cur.execute("SELECT reporter_name FROM group_reporters WHERE group_id = %s;", (group_id,))
        all_reporters = {r[0] for r in cur.fetchall()}

        if not all_reporters:
            return "ℹ️ 測試完成，但名單為空。請先新增成員！"

        # 2. 取得昨天已回報的人員
        cur.execute("SELECT reporter_name FROM reports WHERE group_id = %s AND report_date = %s;", 
                    (group_id, yesterday))
        reported_reporters = {r[0] for r in cur.fetchall()}
        
        # 3. 計算未回報人員
        missing_reports = sorted(list(all_reporters - reported_reporters))

        # 4. 建立測試回覆訊息
        reply_text = f"⚙️ **全群組測試結果 (針對 {yesterday_display})**\n\n"
        
        if not missing_reports:
            reply_text += "🎉 所有成員的心得回報已完成！無需提醒。"
        else:
            missing_names = "\n🔸 ".join(missing_reports)
            reply_text += f"📢 以下 {len(missing_reports)} 位成員尚未完成回報：\n\n🔸 {missing_names}\n\n"
            reply_text += f"💡 請趕快回報：`昨天 [姓名]` 或 `{yesterday_display} [姓名]`"
            
        return reply_text
        
    except Exception as e:
        print(f"DB ERROR (test_daily_reminder): {e}", file=sys.stderr)
        return f"🚨 全群組測試時發生錯誤: {e}"
    finally:
        if conn: conn.close()


# --- LINE Webhook 處理 ---
@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    app.logger.info("Request body: " + body)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        print("Invalid signature. Please check your channel access token/secret.")
        abort(400)
    except Exception as e:
        print(f"Error handling request: {e}", file=sys.stderr)
        abort(500)

    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    text = event.message.text
    source = event.source
    reply_text = None
    
    # 確保只處理群組/聊天室訊息，或與 Bot 的私聊
    if isinstance(source, SourceGroup):
        group_id = source.group_id
    elif isinstance(source, SourceRoom):
        group_id = source.room_id
    elif isinstance(source, SourceUser):
        group_id = source.user_id
    else:
        # 忽略其他來源
        return

    # 簡化指令匹配，不區分大小寫
    text_to_match = text.upper().strip()

    # --- 處理特殊指令 ---
    # 1. NEW: 獲取 ID
    if text_to_match == "/GET ID":
        reply_text = f"🆔 當前聊天 ID (群組/聊天室/個人)：\n{group_id}"
        
    # 2. NEW: 全域提醒暫停/恢復
    elif text_to_match == "暫停回報提醒":
        reply_text = set_global_pause_state(True)
    elif text_to_match == "恢復回報提醒":
        reply_text = set_global_pause_state(False)
        
    # 3. NEW: 測試所有群組提醒 (僅測試當前群組)
    elif text_to_match == "/TEST ALL REMINDER":
        # 測試功能時，我們先檢查全域狀態，如果資料庫初始化失敗，會在內部處理
        reply_text = test_daily_reminder(group_id)
        
    # 4. 幫助指令
    elif text_to_match in ["幫助", "HELP", "指令"]:
        reply_text = ("🤖 **心得追蹤 Bot 指令清單** 📝\n\n"
                      "**名單管理：**\n"
                      "  `新增人名 [姓名]`\n"
                      "  `刪除人名 [姓名]`\n"
                      "  `查詢名單` / `LIST`\n\n"
                      "**回報心得：**\n"
                      "  `今天 [姓名]`\n"
                      "  `昨天 [姓名]`\n"
                      "  `YYYY.MM.DD [姓名]` (如: `2025.11.18 張曉美`)\n\n"
                      "**管理員控制：**\n"
                      "  `暫停回報提醒`\n"
                      "  `恢復回報提醒`\n"
                      "  `/TEST ALL REMINDER` (測試催交)\n"
                      "  `/GET ID` (獲取當前 ID)"
                      )

    # 處理管理指令 (新增/刪除人名, 查詢名單)
    match_add = re.match(r"^新增人名[\s　]+(.+)$", text_to_match)
    if match_add and reply_text is None:
        reporter_name = match_add.group(1).strip()
        reply_text = add_reporter(group_id, reporter_name)

    match_delete = re.match(r"^刪除人名[\s　]+(.+)$", text_to_match)
    if match_delete and reply_text is None:
        reporter_name = match_delete.group(1).strip()
        reply_text = delete_reporter(group_id, reporter_name)

    if text_to_match in ["查詢名單", "查看人員", "名單", "LIST"] and reply_text is None:
        reply_text = get_reporter_list(group_id)

    # 處理「YYYY.MM.DD [星期幾] [人名]」回報指令
    regex_pattern = r"^(\d{4}\.\d{2}\.\d{2})\s*(?:[\s　]*[（(][\s\w\u4e00-\u9fff]+[)）])?\s*(.+)$"
    match_report = re.match(regex_pattern, text_to_match)

    if match_report and reply_text is None:
        date_str = match_report.group(1)
        reporter_name = match_report.group(2).strip()
        reply_text = record_report(group_id, date_str, reporter_name)
    
    # 處理「今天/昨天 [人名]」回報指令
    match_today = re.match(r"^(今天)[\s　]+(.+)$", text_to_match)
    match_yesterday = re.match(r"^(昨天)[\s　]+(.+)$", text_to_match)

    if (match_today or match_yesterday) and reply_text is None:
        match_obj = match_today if match_today else match_yesterday
        time_tag = match_obj.group(1)
        reporter_name = match_obj.group(2).strip()
        
        if time_tag == "今天":
            report_date = datetime.now().strftime('%Y.%m.%d')
        elif time_tag == "昨天":
            report_date = (datetime.now() - timedelta(days=1)).strftime('%Y.%m.%d')
        
        reply_text = record_report(group_id, report_date, reporter_name)


    # 發送回覆
    if reply_text:
        try:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=reply_text)
            )
        except LineBotApiError as e:
            # 如果是群組/聊天室，Bot 被移除後 reply_message 會失敗，需做錯誤處理
            print(f"LINE API Reply ERROR: {e}", file=sys.stderr)
            pass

@app.before_first_request
def setup_application():
    """在應用程式第一次請求前執行，確保資料庫表存在"""
    print("INFO: Initializing database tables...", file=sys.stderr)
    ensure_tables_exist()


if __name__ == "__main__":
    # 在本地執行時，如果資料庫連線資訊缺失，則印出警告
    if not DATABASE_URL:
        print("WARNING: DATABASE_URL is not set. Running in development mode without DB.", file=sys.stderr)
    
    # 確保資料庫在本地啟動前被初始化
    ensure_tables_exist()
    
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)