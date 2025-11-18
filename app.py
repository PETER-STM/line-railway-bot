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
# NEW: 排除的群組ID列表 (用於跳過特定群組的提醒)
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
        # 使用 DSN (Connection String) 連線
        conn = psycopg2.connect(DATABASE_URL)
        return conn
    except Exception as e:
        print(f"ERROR: Database connection failed! Details: {e}", file=sys.stderr)
        return None

# --- 資料庫表格檢查與建立 ---
def ensure_tables_exist():
    """確保所有必要的資料庫表格存在 (reporters, reports, settings)"""
    conn = get_db_connection()
    if conn is None:
        print("ERROR: Cannot create tables, database connection failed.", file=sys.stderr)
        return False
        
    try:
        with conn.cursor() as cur:
            # 1. 建立 reporters 表格 (儲存群組成員名單)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS reporters (
                    id SERIAL PRIMARY KEY,
                    group_id VARCHAR(50) NOT NULL,
                    name VARCHAR(100) NOT NULL,
                    UNIQUE (group_id, name)
                );
            """)
            # 2. 建立 reports 表格 (儲存每日回報紀錄)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS reports (
                    id SERIAL PRIMARY KEY,
                    group_id VARCHAR(50) NOT NULL,
                    reporter_name VARCHAR(100) NOT NULL,
                    report_date DATE NOT NULL,
                    UNIQUE (group_id, reporter_name, report_date)
                );
            """)
            # 3. 建立 settings 表格 (儲存系統設定，例如暫停狀態)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    key VARCHAR(50) PRIMARY KEY,
                    value VARCHAR(255) NOT NULL
                );
            """)
            
            # 初始化暫停狀態 (如果 settings 表格是新的)
            cur.execute("SELECT COUNT(*) FROM settings WHERE key = 'is_paused';")
            if cur.fetchone()[0] == 0:
                cur.execute("INSERT INTO settings (key, value) VALUES ('is_paused', 'false');")
                print("INFO: Initial 'is_paused' setting created.", file=sys.stderr)
            
            conn.commit()
            print("INFO: 'reporters', 'reports', and 'settings' tables checked/created successfully.", file=sys.stderr)
            return True
    except Exception as e:
        print(f"FATAL ERROR: Failed to create database tables! Details: {e}", file=sys.stderr)
        return False
    finally:
        if conn: conn.close()

# --- 啟動時檢查資料庫 ---
print("INFO: Running database table setup...", file=sys.stderr)
ensure_tables_exist()

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
        print(f"LINE API Error: {e.status_code}, {e.message}", file=sys.stderr)
        abort(500)

    return 'OK'

# --- 資料庫操作輔助函式 ---

def add_reporter(group_id, name):
    """將人名加入群組名單"""
    if not name:
        return "❌ 姓名不可為空，請輸入 `新增人名 [姓名]`。"
        
    conn = get_db_connection()
    if conn is None:
        return "❌ 資料庫連線失敗，無法新增人名！"

    try:
        with conn.cursor() as cur:
            # 檢查是否已存在
            cur.execute("SELECT name FROM reporters WHERE group_id = %s AND name = %s;", (group_id, name))
            if cur.fetchone():
                return f"⚠️ {name} 已經在名單內了，無需重複新增。"

            # 插入新的人名
            cur.execute(
                "INSERT INTO reporters (group_id, name) VALUES (%s, %s);", 
                (group_id, name)
            )
            conn.commit()
            return f"✅ 已成功新增 {name} 到名單中。\n\n每日提醒檢查將開始涵蓋 {name}。"
    except Exception as e:
        print(f"DB ERROR (add_reporter): {e}", file=sys.stderr)
        return f"❌ 新增人名時發生資料庫錯誤：{e}"
    finally:
        if conn: conn.close()

def delete_reporter(group_id, name):
    """將人名從群組名單刪除"""
    if not name:
        return "❌ 姓名不可為空，請輸入 `刪除人名 [姓名]`。"

    conn = get_db_connection()
    if conn is None:
        return "❌ 資料庫連線失敗，無法刪除人名！"

    try:
        with conn.cursor() as cur:
            # 刪除人名
            cur.execute("DELETE FROM reporters WHERE group_id = %s AND name = %s;", (group_id, name))
            
            if cur.rowcount == 0:
                return f"⚠️ {name} 不在名單內，無需刪除。"

            # 刪除成功後，同時刪除該成員的歷史回報紀錄
            cur.execute("DELETE FROM reports WHERE group_id = %s AND reporter_name = %s;", (group_id, name))
            
            conn.commit()
            return f"✅ 已成功將 {name} 從名單中移除，相關歷史回報紀錄也已清除。"
    except Exception as e:
        print(f"DB ERROR (delete_reporter): {e}", file=sys.stderr)
        return f"❌ 刪除人名時發生資料庫錯誤：{e}"
    finally:
        if conn: conn.close()

def get_reporter_list(group_id):
    """查詢群組名單"""
    conn = get_db_connection()
    if conn is None:
        return "❌ 資料庫連線失敗，無法查詢名單！"

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT name FROM reporters WHERE group_id = %s ORDER BY name;", (group_id,))
            names = [row[0] for row in cur.fetchall()]
            
            if not names:
                return "📜 目前名單為空。請使用 `新增人名 [姓名]` 來加入成員。"
            
            name_list = "\n".join([f"- {name}" for name in names])
            return f"📜 **目前心得回報名單：**\n\n{name_list}\n\n總計：{len(names)} 位成員"
    except Exception as e:
        print(f"DB ERROR (get_reporter_list): {e}", file=sys.stderr)
        return f"❌ 查詢名單時發生資料庫錯誤：{e}"
    finally:
        if conn: conn.close()

def handle_report(group_id, date_str, reporter_name):
    """處理成員回報心得的訊息"""
    conn = get_db_connection()
    if conn is None:
        return "❌ 資料庫連線失敗，無法記錄回報！"
        
    try:
        # 1. 檢查回報人是否在名單內
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM reporters WHERE group_id = %s AND name = %s;", (group_id, reporter_name))
            if not cur.fetchone():
                return f"⚠️ {reporter_name} 不在當前名單內，請先使用 `新增人名 {reporter_name}` 加入名單。"

            # 2. 驗證日期格式並轉換
            try:
                report_date = datetime.strptime(date_str, '%Y.%m.%d').date()
            except ValueError:
                return "❌ 日期格式錯誤，請使用 `YYYY.MM.DD [姓名]` 格式，例如: `2025.11.18 小明`。"
            
            # 3. 檢查是否已經回報過 (使用 ON CONFLICT DO NOTHING)
            cur.execute(
                """
                INSERT INTO reports (group_id, reporter_name, report_date) 
                VALUES (%s, %s, %s)
                ON CONFLICT (group_id, reporter_name, report_date) 
                DO NOTHING;
                """, 
                (group_id, reporter_name, report_date)
            )
            
            if cur.rowcount == 0:
                # 已經存在紀錄
                return f"💡 {report_date.strftime('%Y/%m/%d')} 的心得，{reporter_name} 已經回報過了喔！"
            else:
                # 成功新增紀錄
                conn.commit()
                return f"✅ 感謝 {reporter_name}！已成功記錄 {report_date.strftime('%Y/%m/%d')} 的心得回報。"

    except Exception as e:
        print(f"DB ERROR (handle_report): {e}", file=sys.stderr)
        return f"❌ 記錄回報時發生資料庫錯誤：{e}"
    finally:
        if conn: conn.close()

# --- 管理指令函式 (與暫停狀態相關) ---

def set_scheduler_pause_state(group_id, state):
    """設定排程器暫停狀態"""
    conn = get_db_connection()
    if conn is None:
        return "❌ 資料庫連線失敗，無法設定暫停狀態！"

    try:
        with conn.cursor() as cur:
            # 使用 ON CONFLICT DO UPDATE 確保 key 存在
            cur.execute(
                "INSERT INTO settings (key, value) VALUES ('is_paused', %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;",
                (state,)
            )
            conn.commit()
            state_text = "已暫停 (PAUSED) ⏸️" if state == 'true' else "已啟動 (RUNNING) ▶️"
            return f"✅ 排程提醒功能設定成功！目前狀態為：{state_text}\n\n所有群組的每日提醒將遵循此設定。"
    except Exception as e:
        print(f"DB ERROR (set_pause_state): {e}", file=sys.stderr)
        return f"❌ 設定暫停狀態時發生資料庫錯誤：{e}"

def test_daily_reminder(group_id):
    """測試每日提醒功能是否開啟"""
    conn = get_db_connection()
    if conn is None:
        return "❌ 資料庫連線失敗，無法查詢排程器狀態！"

    try:
        with conn.cursor() as cur:
            # 檢查是否暫停
            cur.execute("SELECT value FROM settings WHERE key = 'is_paused';")
            result = cur.fetchone()
            is_paused = result[0].lower() if result else 'false'
            
            status_text = "已暫停 (PAUSED) ⏸️" if is_paused == 'true' else "正在運行 (RUNNING) ▶️"
            
            # 檢查目標群組是否被排除
            is_excluded = group_id in EXCLUDE_GROUP_IDS
            exclude_text = "❌ 警告：此群組 ID 被列在環境變數 EXCLUDE_GROUP_IDS 中，排程器會跳過此群組！" if is_excluded else "✅ 此群組未被排除。"

            # 顯示檢查日期 (UTC 前一天)
            # 因為排程器 (Worker) 檢查的是前一天的報告
            target_date = (datetime.utcnow() - timedelta(days=1)).strftime('%Y.%m.%d')
            
            return f"""
✨ 排程器 (Worker) 狀態檢查結果 ✨
- **功能總開關：** {status_text}
- **排程時間：** 每日 UTC 01:00 (台灣時間 09:00) 執行。
- **檢查日期：** 針對 {target_date} 的回報狀態進行提醒。
- **群組排除狀態：** {exclude_text}

ℹ️ 使用 `管理員指令 暫停提醒` 或 `管理員指令 啟動提醒` 來控制開關。
"""
    except Exception as e:
        print(f"DB ERROR (test_daily_reminder): {e}", file=sys.stderr)
        return f"❌ 查詢排程器狀態時發生資料庫錯誤：{e}"


@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    
    # 僅處理群組/聊天室訊息
    if not isinstance(event.source, (SourceGroup, SourceRoom)):
        # 如果是個人聊天，可以回覆一個提示
        if isinstance(event.source, SourceUser):
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="本機器人設計為在群組或聊天室內使用，以進行心得回報與提醒管理。請將我加入群組後使用相關指令。")
            )
        return

    group_id = event.source.group_id if isinstance(event.source, SourceGroup) else event.source.room_id
    text = event.message.text
    text_to_match = text.strip().lower()
    reply_text = None
    
    # --- 處理管理指令 ---
    if text_to_match.startswith("管理員指令"):
        if text_to_match == "管理員指令 暫停提醒":
            reply_text = set_scheduler_pause_state(group_id, 'true')
        
        elif text_to_match == "管理員指令 啟動提醒":
            reply_text = set_scheduler_pause_state(group_id, 'false')
            
        elif text_to_match == "管理員指令 測試提醒":
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
    # 允許中間有可選的 (星期幾) 或 （星期幾）
    regex_pattern = r"^(\d{4}\.\d{2}\.\d{2})\s*(?:[\s　]*[（(][\s\w\u4e00-\u9fff]+[)）])?\s*(.+)$"
    match_report = re.match(regex_pattern, text_to_match)

    if match_report:
        date_str = match_report.group(1)
        # 確保名字沒有包含太多空格
        reporter_name = match_report.group(2).strip()
        
        # 嘗試處理回報
        reply_text = handle_report(group_id, date_str, reporter_name)

    # --- 處理預設指令 (在所有匹配之後) ---
    if reply_text is None and text_to_match in ["嗨", "hello", "hi", "help", "幫助", "指令"]:
        reply_text = (
            "🤖 每日心得提醒機器人指令清單 🤖\n\n"
            "👥 **成員管理 (僅群組/聊天室可用):**\n"
            "  - `新增人名 [姓名]`\n"
            "  - `刪除人名 [姓名]`\n"
            "  - `查詢名單`\n\n"
            "📝 **心得回報 (必須在群組內發送):**\n"
            "  - `YYYY.MM.DD [姓名]` (例如: `2025.11.18 小明`)\n"
            "  - 可在日期後加入星期幾，例如：`2025.11.18 (一) 小明`\n\n"
            "🔑 **管理員指令 (排程器總開關):**\n"
            "  - `管理員指令 暫停提醒`\n"
            "  - `管理員指令 啟動提醒`\n"
            "  - `管理員指令 測試提醒` (查看目前狀態)\n\n"
            "🔔 **提醒邏輯:**\n"
            "  - 每日 UTC 01:00 (台灣時間 09:00) 檢查前一天是否有未回報者。\n"
            "  - 只有名單上的成員才會被檢查。"
        )
    
    # 回覆訊息
    if reply_text:
        try:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=reply_text)
            )
        except LineBotApiError as e:
            print(f"LINE API Reply Error: {e.status_code}, {e.message}", file=sys.stderr)
            
# --- 啟動 Flask 應用 ---
# Gunicorn 會使用 Procfile 中指定的 $PORT (通常為 8080)。
# 這裡的 if __name__ == "__main__": 塊僅用於本地開發/測試。
if __name__ == "__main__":
    # 使用環境變數 $PORT，如果沒有則使用 8080 作為本地開發的默認端口。
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)