import os
import sys
import re
from datetime import datetime, timedelta
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError, LineBotApiError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, SourceGroup, SourceRoom, SourceUser
import psycopg2
# 引入 Google Gemini (如果 GOOGLE_API_KEY 有設置)
import google.generativeai as genai 

# --- 姓名正規化工具 (用於確保 VIP 記錄唯一性) ---
def normalize_name(name):
    """
    對人名進行正規化處理，主要移除開頭的班級或編號標記。
    例如: "(三) 浣熊🦝" -> "浣熊🦝"
    """
    # 移除開頭被括號 (圓括號、全形括號、方括號、書名號) 包裹的內容
    normalized = re.sub(r'^\s*[（(\[【][^()\[\]]{1,10}[)）\]】]\s*', '', name).strip()
    
    # 如果正規化結果為空，返回原始名稱
    return normalized if normalized else name

# --- 環境變數設定 ---
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
DATABASE_URL = os.environ.get('DATABASE_URL')
GOOGLE_API_KEY = os.environ.get('GOOGLE_API_KEY') 
# 排除的群組ID列表
EXCLUDE_GROUP_IDS_STR = os.environ.get('EXCLUDE_GROUP_IDS', '')
EXCLUDE_GROUP_IDS = set(EXCLUDE_GROUP_IDS_STR.split(',')) if EXCLUDE_GROUP_IDS_STR else set()

# --- 診斷與初始化 ---
if not LINE_CHANNEL_ACCESS_TOKEN:
    sys.exit("LINE_CHANNEL_ACCESS_TOKEN is missing!")
if not LINE_CHANNEL_SECRET:
    sys.exit("LINE_CHANNEL_SECRET is missing!")

# 初始化 AI 模型
model = None
if GOOGLE_API_KEY:
    try:
        genai.configure(api_key=GOOGLE_API_KEY)
        model = genai.GenerativeModel('gemini-1.5-flash')
        # print("INFO: Gemini AI model initialized successfully.", file=sys.stderr) 
    except Exception as e:
        print(f"WARNING: Failed to initialize Gemini AI: {e}", file=sys.stderr)
else:
    print("WARNING: GOOGLE_API_KEY not found. AI features will be disabled.", file=sys.stderr)

app = Flask(__name__)
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# --- 活潑・幽默・微毒舌 回覆模板 ---
UNKNOWN_ERROR_TEXT = (
    "💥 發生未知錯誤。\n"
    "可能是宇宙磁場不順，或系統在叛逆。\n"
    "稍後再試，或找管理員用愛（或一包綠色包裝的乖乖）感化它。"
)

# --- 資料庫連線函式 ---
def get_db_connection():
    conn = None
    try:
        # 使用 sslmode='require' 以確保安全連線
        conn = psycopg2.connect(DATABASE_URL, sslmode='require')
        return conn
    except Exception as e:
        print(f"Database connection error: {e}", file=sys.stderr)
        return None

# --- 資料庫初始化 (最終修正版，強制 DROP 舊表結構) ---
def ensure_tables_exist():
    conn = get_db_connection()
    if conn is None: 
        print("DB INIT ERROR: Cannot get database connection.", file=sys.stderr)
        return

    try:
        with conn.cursor() as cur:
            # 🚨 關鍵修正：強制刪除舊結構的資料表，以確保後續的 CREATE 語句能創建正確的結構。
            # 這能徹底解決「column "key" of relation "settings" does not exist」的問題
            cur.execute("DROP TABLE IF EXISTS settings CASCADE;")
            cur.execute("DROP TABLE IF EXISTS group_modes CASCADE;")
            cur.execute("DROP TABLE IF EXISTS group_vips CASCADE;")
            cur.execute("DROP TABLE IF EXISTS reports CASCADE;")

            # 1. VIP 名單表
            cur.execute("""
                CREATE TABLE group_vips (
                    group_id TEXT NOT NULL, 
                    vip_name TEXT NOT NULL,
                    normalized_vip_name TEXT NOT NULL, 
                    PRIMARY KEY (group_id, vip_name)
                );
            """)
            # 2. 回報紀錄表
            cur.execute("""
                CREATE TABLE reports (
                    id SERIAL PRIMARY KEY, 
                    group_id TEXT NOT NULL,
                    report_date DATE NOT NULL,
                    reporter_name TEXT NOT NULL, 
                    normalized_reporter_name TEXT NOT NULL, 
                    log_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (group_id, report_date, normalized_reporter_name) 
                );
            """)
            # 3. 系統設定表 (全域暫停)
            cur.execute("""
                CREATE TABLE settings (
                    key TEXT PRIMARY KEY, 
                    value TEXT NOT NULL
                );
            """)
            # 4. 群組模式表 (AI 開關)
            cur.execute("""
                CREATE TABLE group_modes (
                    group_id TEXT PRIMARY KEY,
                    mode TEXT DEFAULT 'NORMAL' -- 'NORMAL' or 'AI'
                );
            """)
            
            # 初始化全域暫停狀態 (現在 settings 表是乾淨的，不會報錯)
            cur.execute("INSERT INTO settings (key, value) VALUES ('is_paused', 'false') ON CONFLICT DO NOTHING;")
            conn.commit()
            print("INFO: Database tables checked/created.", file=sys.stderr)
    except Exception as e:
        print(f"DB INIT ERROR: {e}", file=sys.stderr)
    finally:
        conn.close()

# 啟動時初始化 DB
with app.app_context():
    ensure_tables_exist()

# --- 資料庫操作函式 (新增/刪除/查詢 VIP) ---

def add_vip_to_group(group_id, name):
    conn = get_db_connection()
    if not conn: return UNKNOWN_ERROR_TEXT

    # 修正: 先處理名稱，避免在 f-string 內執行複雜運算
    name_for_db = name.split('\n', 1)[0].strip()
    normalized_name = normalize_name(name_for_db)

    try:
        with conn.cursor() as cursor:
            # 檢查 VIP 是否已存在 (只檢查原始名稱)
            cursor.execute(
                "SELECT COUNT(*) FROM group_vips WHERE group_id = %s AND vip_name = %s;",
                (group_id, name_for_db)
            )
            if cursor.fetchone()[0] > 0:
                return f"🤨 {name_for_db} 早就在名單裡面坐好坐滿了，\n\n你該不會…忘記上一次也加過吧？"

            # 新增 VIP
            cursor.execute(
                "INSERT INTO group_vips (group_id, vip_name, normalized_vip_name) VALUES (%s, %s, %s);",
                (group_id, name_for_db, normalized_name)
            )
            conn.commit()
            return f"🎉 好嘞～ {name_for_db} 已成功加入名單！\n\n（逃不掉了，祝他順利回報。）"

    except Exception as e:
        print(f"DB Error (add_vip_to_group): {e}", file=sys.stderr)
        return UNKNOWN_ERROR_TEXT
    finally:
        if conn: conn.close()

def remove_vip_from_group(group_id, name):
    conn = get_db_connection()
    if not conn: return UNKNOWN_ERROR_TEXT

    # 修正: 先處理名稱，避免在 f-string 內執行複雜運算 (解決 SyntaxError)
    name_to_display = name.split('\n', 1)[0].strip()
    normalized_name_to_remove = normalize_name(name_to_display)

    try:
        with conn.cursor() as cursor:
            # 刪除所有正規化名稱匹配的記錄
            cursor.execute(
                "DELETE FROM group_vips WHERE group_id = %s AND normalized_vip_name = %s;",
                (group_id, normalized_name_to_remove)
            )
            rows_deleted = cursor.rowcount
            
            # 也要刪除 reports 裡的紀錄，防止殘留
            cursor.execute(
                "DELETE FROM reports WHERE group_id = %s AND normalized_reporter_name = %s;",
                (group_id, normalized_name_to_remove)
            )
            cursor.rowcount # 確保 reports 表操作被執行
            conn.commit()

            if rows_deleted > 0:
                # 修正: 使用 name_to_display 變數
                return f"🗑️ {name_to_display} 已從名單中被溫柔移除。\n\n（放心，我沒有把人綁走，只是移出名單。）"
            else:
                # 修正: 使用 name_to_display 變數
                return f"❓名單裡根本沒有 {name_to_display} 啊！\n\n是不是名字打錯，還是你其實不想他回報？"

    except Exception as e:
        print(f"DB Error (remove_vip_from_group): {e}", file=sys.stderr)
        return UNKNOWN_ERROR_TEXT
    finally:
        if conn: conn.close()


def list_vips_in_group(group_id):
    conn = get_db_connection()
    if not conn: return UNKNOWN_ERROR_TEXT

    try:
        with conn.cursor() as cursor:
            # 查詢所有 VIP 的原始名稱和正規化名稱
            cursor.execute(
                "SELECT vip_name, normalized_vip_name FROM group_vips WHERE group_id = %s ORDER BY normalized_vip_name, vip_name;",
                (group_id,)
            )
            
            # 優化：根據 normalized_name 去重，並優先保留不帶括號的名稱作為顯示名稱
            unique_vips = {}
            for vip_name, normalized_name in cursor.fetchall():
                # 如果這個正規化名稱還沒被記錄，或者當前的 vip_name 是一個更「乾淨」的版本
                # 這裡的邏輯是確保同一個人的不同稱謂 (如：(三) 浣熊 / 浣熊) 只會顯示一次。
                if normalized_name not in unique_vips or (
                   len(normalized_name) < len(unique_vips[normalized_name])
                ):
                    unique_vips[normalized_name] = vip_name
            
            vip_list = sorted(list(unique_vips.values()))

            if not vip_list:
                return "📭 名單空空如也～\n\n快用 `加VIP [姓名]` 把第一位勇者召喚進來吧！"

            # 格式化輸出
            list_of_names = "\n".join(vip_list) 
            reply_text = (
                f"📋 最新回報觀察名單如下：\n"
                f"{list_of_names}\n\n"
                f"（嗯，看起來大家都還活著。）"
            )
            return reply_text

    except Exception as e:
        print(f"DB Error (list_vips_in_group): {e}", file=sys.stderr)
        return UNKNOWN_ERROR_TEXT
    finally:
        if conn: conn.close()

def log_report(group_id, report_date, reporter_name):
    conn = get_db_connection()
    if not conn: return UNKNOWN_ERROR_TEXT
    
    # 修正: 先處理名稱，避免在 f-string 內執行複雜運算
    name_for_db = reporter_name.split('\n', 1)[0].strip()
    normalized_name = normalize_name(name_for_db)

    try:
        with conn.cursor() as cursor:
            # 1. 檢查這個正規化後的人名是否在 VIP 名單中
            cursor.execute(
                "SELECT vip_name FROM group_vips WHERE group_id = %s AND normalized_vip_name = %s LIMIT 1;",
                (group_id, normalized_name)
            )
            is_vip = cursor.fetchone()

            if not is_vip:
                # 提示使用者不在 VIP 名單中
                return (
                    f"🧐 系統找不到 {name_for_db} 在 VIP 名單中。\n\n"
                    f"請先請管理員用指令： `加VIP {name_for_db}` 把你加進來喔！\n"
                    f"（不然系統會假裝沒看到你交的心得... 😏）"
                )

            # 2. 檢查是否已經提交過心得
            cursor.execute(
                "SELECT id FROM reports WHERE group_id = %s AND report_date = %s AND normalized_reporter_name = %s LIMIT 1;",
                (group_id, report_date, normalized_name)
            )
            if cursor.fetchone():
                date_str = report_date.strftime('%Y.%m.%d')
                return f"⚠️ {name_for_db} ({date_str}) 今天已經回報過了！\n\n別想靠重複交作業刷存在感，我看的很清楚 👀"

            # 3. 記錄心得 (這裡使用 name_for_db 來儲存原始名稱)
            cursor.execute(
                "INSERT INTO reports (group_id, report_date, reporter_name, normalized_reporter_name) VALUES (%s, %s, %s, %s);",
                (group_id, report_date, name_for_db, normalized_name)
            )
            conn.commit()

            date_str = report_date.strftime('%Y.%m.%d')
            return f"👌 收到！{name_for_db} ({date_str}) 的心得已成功登入檔案。\n\n（今天有乖，給你一個隱形貼紙 ⭐）"

    except Exception as e:
        print(f"DB Error (log_report): {e}", file=sys.stderr)
        return UNKNOWN_ERROR_TEXT
    finally:
        if conn: conn.close()


# --- AI/Settings 相關函式 ---
def get_group_mode(group_id):
    conn = get_db_connection()
    if not conn: return 'NORMAL' 
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT mode FROM group_modes WHERE group_id = %s", (group_id,))
            res = cur.fetchone()
            return res[0] if res else 'NORMAL'
    except Exception as e:
        # 這裡可能會因為 group_modes 不存在而報錯，返回預設值
        print(f"MODE GET ERROR: {e}", file=sys.stderr)
        return 'NORMAL'
    finally:
        if conn: conn.close()

def set_group_mode(group_id, mode):
    conn = get_db_connection()
    if not conn: return UNKNOWN_ERROR_TEXT 
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO group_modes (group_id, mode) VALUES (%s, %s)
                ON CONFLICT (group_id) DO UPDATE SET mode = EXCLUDED.mode
            """, (group_id, mode))
            conn.commit()
        status_text = "🤖 智能對話 (AI)" if mode == 'AI' else "🔇 一般安靜 (NORMAL)"
        return f"🔄 模式已切換為：**{status_text}**"
    except Exception as e:
        print(f"MODE SET ERROR: {e}", file=sys.stderr)
        return UNKNOWN_ERROR_TEXT
    finally:
        if conn: conn.close()

def generate_ai_reply(user_message):
    if not model: return None
    try:
        system_prompt = (
            "你是一個幽默、有點毒舌但很樂於助人的團隊助理 Bot。你的名字叫「摳你錢3000」。"
            "你的主要任務是陪伴群組成員聊天。請用繁體中文簡短回應，不要長篇大論。"
        )
        full_prompt = f"{system_prompt}\n\n使用者說：{user_message}"
        response = model.generate_content(full_prompt)
        return response.text.strip()
    except Exception as e:
        print(f"AI GEN ERROR: {e}", file=sys.stderr)
        return "😵‍💫 AI 腦袋打結了，請稍後再試。"

def set_global_pause(state):
    conn = get_db_connection()
    if not conn: return UNKNOWN_ERROR_TEXT
    try:
        with conn.cursor() as cur:
            # 檢查 settings 表格是否已經初始化
            cur.execute("SELECT value FROM settings WHERE key = 'is_paused'")
            if cur.fetchone() is None:
                # 如果沒有，先插入預設值
                cur.execute("INSERT INTO settings (key, value) VALUES ('is_paused', 'false') ON CONFLICT DO NOTHING;")
            
            cur.execute("UPDATE settings SET value = %s WHERE key = 'is_paused'", (state,))
            conn.commit()
        status = "暫停" if state == 'true' else "恢復"
        return f"⚙️ 全域回報提醒已 **{status}**。" 
    finally:
        if conn: conn.close()

def test_daily_reminder(group_id):
    if group_id in EXCLUDE_GROUP_IDS:
         return "🚫 這個群組在「排除名單」裡，\n\n排程器看到這邊會自動裝死，不會發任何提醒。"
    return "🔔 測試指令 OK！\n\n請坐等排程器在設定時間跳出來嚇你，\n\n以確認系統正常運作。"

# --- LINE 事件處理 ---

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    if not isinstance(event.source, (SourceGroup, SourceRoom, SourceUser)):
        return

    group_id = None
    if isinstance(event.source, (SourceGroup, SourceRoom)):
        group_id = event.source.group_id if isinstance(event.source, SourceGroup) else event.source.room_id
    elif isinstance(event.source, SourceUser):
        group_id = event.source.user_id 
    
    if group_id in EXCLUDE_GROUP_IDS:
        return

    text = event.message.text.strip()
    reply_text = None

    # 預處理：全形轉半形，便於指令匹配
    processed_text = text.replace('（', '(').replace('）', ')')
    
    # --- 1. 系統指令 ---
    if processed_text == "指令" or processed_text == "幫助":
        reply_text = (
            "🤖 **功能選單**\n\n"
            "📝 **回報**: `YYYY.MM.DD [姓名] [內容]`\n"
            "👥 **管理**: `加VIP [姓名]`, `減VIP [姓名]`, `查詢名單`\n"
            "⚙️ **AI**: `開啟智能模式`, `關閉智能模式`\n"
            "🔧 **系統**: `測試排程`, `暫停回報提醒`, `恢復回報提醒`"
        )
    elif processed_text == "開啟智能模式": reply_text = set_group_mode(group_id, 'AI')
    elif processed_text == "關閉智能模式": reply_text = set_group_mode(group_id, 'NORMAL')
    elif processed_text == "暫停回報提醒": reply_text = set_global_pause('true')
    elif processed_text == "恢復回報提醒": reply_text = set_global_pause('false')
    elif processed_text in ["發送提醒測試", "測試排程"]: reply_text = test_daily_reminder(group_id)

    # --- 2. 管理與回報指令 ---
    if not reply_text:
        # 查詢 VIP 名單指令
        if text in ["查VIP", "列出VIP", "查詢名單", "名單", "誰是VIP"]:
            reply_text = list_vips_in_group(group_id)
        # 新增 VIP 指令 (加VIP 姓名)
        elif text.startswith("加VIP") or text.startswith("新增人名"):
            parts = text.split(maxsplit=1)
            if len(parts) == 2 and parts[1].strip():
                name_to_add = parts[1].strip()
                reply_text = add_vip_to_group(group_id, name_to_add)
            else:
                reply_text = "🤷‍♀️ 請問想加誰進 VIP 名單？\n\n請使用格式： `加VIP 姓名`"
        # 移除 VIP 指令 (減VIP 姓名)
        elif text.startswith("減VIP") or text.startswith("移除VIP") or text.startswith("刪除人名"):
            parts = text.split(maxsplit=1)
            if len(parts) == 2 and parts[1].strip():
                name_to_remove = parts[1].strip()
                reply_text = remove_vip_from_group(group_id, name_to_remove)
            else:
                reply_text = "🤷‍♀️ 請問想移除誰出 VIP 名單？\n\n請使用格式： `減VIP 姓名`"
        
        # 心得回報/打卡處理 (YYYY.MM.DD 姓名 OR YYYY/MM/DD 姓名)
        # Regex: 抓取日期 + 至少一個空格 + 人名 (直到換行)
        match_report = re.match(r"^(\d{4}[./]\d{2}[./]\d{2})\s+([^\n]+)", text)
        
        if match_report:
            date_str = match_report.group(1) # 日期
            name_and_rest = match_report.group(2).strip() # 人名及後續的字串
            
            try:
                # 轉換分隔符號為點號，以便統一解析
                date_str = date_str.replace('/', '.') 
                report_date = datetime.strptime(date_str, '%Y.%m.%d').date()
                reporter_name = name_and_rest # 將整個字串傳入 log_report 處理
                
                # 確保人名不為空
                if not reporter_name or not normalize_name(reporter_name):
                    reply_text = "⚠️ 日期後面請記得加上人名，不然我不知道誰交的啊！\n\n（你總不會想讓我自己猜吧？）"
                else:
                    reply_text = log_report(group_id, report_date, reporter_name)
                
            except ValueError:
                reply_text = "❌ 日期長得怪怪的。\n\n請用標準格式：YYYY.MM.DD 姓名\n\n（小數點不是你的自由發揮。）"

    # --- 3. AI 閒聊 ---
    if not reply_text and get_group_mode(group_id) == 'AI':
        reply_text = generate_ai_reply(text)

    # 發送回覆訊息
    if reply_text:
        try:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=reply_text)
            )
        except LineBotApiError as e:
            print(f"LINE API PUSH/REPLY ERROR: {e}", file=sys.stderr)
            pass 

# --- Webhook 主入口 ---
@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        print("Invalid signature. Please check your channel access token/secret.", file=sys.stderr)
        abort(400)
    except Exception as e:
        print(f"General Error during webhook handling: {e}", file=sys.stderr)
        pass 
    return 'OK'

# --- 啟動 Flask 應用 (通常用於本地測試) ---
if __name__ == "__main__":
    port = int(os.environ.get('PORT', 8080))
    print(f"Note: Running via Gunicorn in production. Use 'gunicorn app:app' to start.", file=sys.stderr)