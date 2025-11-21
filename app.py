import os
import sys
import re
from datetime import datetime, timedelta
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError, LineBotApiError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, SourceGroup, SourceRoom, SourceUser
import psycopg2
import google.generativeai as genai

# --- 環境變數設定 ---
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
DATABASE_URL = os.environ.get('DATABASE_URL')
GOOGLE_API_KEY = os.environ.get('GOOGLE_API_KEY') # 新增：Gemini API Key

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
        print("INFO: Gemini AI model initialized successfully.", file=sys.stderr)
    except Exception as e:
        print(f"WARNING: Failed to initialize Gemini AI: {e}", file=sys.stderr)
else:
    print("WARNING: GOOGLE_API_KEY not found. AI features will be disabled.", file=sys.stderr)

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

# --- 資料庫初始化 ---
def ensure_tables_exist():
    conn = get_db_connection()
    if conn is None: return
    try:
        with conn.cursor() as cur:
            # 1. 成員名單表
            cur.execute("""
                CREATE TABLE IF NOT EXISTS reporters (
                    group_id TEXT NOT NULL, reporter_name TEXT NOT NULL,
                    PRIMARY KEY (group_id, reporter_name)
                );
            """)
            # 2. 回報紀錄表 (含心得內容)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS reports (
                    id SERIAL PRIMARY KEY, group_id TEXT NOT NULL,
                    reporter_name TEXT NOT NULL, report_date DATE NOT NULL,
                    report_content TEXT, log_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (group_id, reporter_name, report_date)
                );
            """)
            # 3. 系統設定表 (全域暫停)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL
                );
            """)
            # 4. 群組模式表 (控制每個群組是否開啟 AI)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS group_modes (
                    group_id TEXT PRIMARY KEY,
                    mode TEXT DEFAULT 'NORMAL' -- 'NORMAL' or 'AI'
                );
            """)
            
            # 初始化全域暫停狀態
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

# --- 姓名正規化工具 ---
def normalize_name(name):
    # 移除開頭括號內容 (如：(三) 浣熊 -> 浣熊)
    normalized = re.sub(r'^\s*[（(\[【][^()\[\]]{1,10}[)）\]】]\s*', '', name).strip()
    return normalized if normalized else name

# --- AI 相關函式 ---

def get_group_mode(group_id):
    """檢查群組模式 (NORMAL / AI)"""
    conn = get_db_connection()
    if not conn: return 'NORMAL'
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT mode FROM group_modes WHERE group_id = %s", (group_id,))
            res = cur.fetchone()
            return res[0] if res else 'NORMAL'
    finally:
        conn.close()

def set_group_mode(group_id, mode):
    """切換群組模式"""
    conn = get_db_connection()
    if not conn: return "💥 資料庫連線失敗。"
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
        return "💥 設定失敗，請稍後再試。"
    finally:
        conn.close()

def generate_ai_reply(user_message):
    """呼叫 Gemini 生成回應"""
    if not model: return None
    try:
        # 設定系統提示 (Persona)
        system_prompt = (
            "你是一個幽默、有點毒舌但很樂於助人的團隊助理 Bot。你的名字叫「摳你錢3000」。"
            "你的主要任務是陪伴群組成員聊天。請用繁體中文簡短回應，不要長篇大論。"
            "如果有人問你問題，就盡量回答。如果有人在閒聊，就陪他聊。"
        )
        # 簡單的單次對話 (無記憶版，最省資源)
        full_prompt = f"{system_prompt}\n\n使用者說：{user_message}"
        
        response = model.generate_content(full_prompt)
        return response.text.strip()
    except Exception as e:
        print(f"AI GEN ERROR: {e}", file=sys.stderr)
        return "😵‍💫 AI 腦袋打結了，請稍後再試。"

# --- 核心指令與資料庫操作 (保持原有的回報功能) ---

def add_reporter(group_id, reporter_name):
    conn = get_db_connection()
    if not conn: return "💥 資料庫連線失敗。"
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO reporters (group_id, reporter_name) VALUES (%s, %s) ON CONFLICT DO NOTHING", (group_id, reporter_name))
            if cur.rowcount > 0:
                conn.commit()
                return f"🎉 好嘞～ {reporter_name} 已成功加入名單！\n\n（逃不掉了，祝他順利回報。）"
            return f"🤨 {reporter_name} 早就在名單裡面坐好坐滿了。"
    finally:
        conn.close()

def delete_reporter(group_id, reporter_name):
    conn = get_db_connection()
    if not conn: return "💥 資料庫連線失敗。"
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM reporters WHERE group_id = %s AND reporter_name = %s", (group_id, reporter_name))
            if cur.rowcount > 0:
                cur.execute("DELETE FROM reports WHERE group_id = %s AND reporter_name = %s", (group_id, reporter_name))
                conn.commit()
                return f"🗑️ {reporter_name} 已從名單中被溫柔移除。"
            return f"❓名單裡根本沒有 {reporter_name} 啊！"
    finally:
        conn.close()

def get_reporter_list(group_id):
    conn = get_db_connection()
    if not conn: return "💥 資料庫連線失敗。"
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT reporter_name FROM reporters WHERE group_id = %s ORDER BY reporter_name", (group_id,))
            reporters = [row[0] for row in cur.fetchall()]
            if reporters:
                return f"📋 最新回報觀察名單如下：\n" + "\n".join(reporters) + "\n\n（嗯，看起來大家都還活著。）"
            return "📭 名單空空如也～\n\n快用 `新增人名 [姓名]` 把第一位勇者召喚進來吧！"
    finally:
        conn.close()

def log_report(group_id, date_str, reporter_name, content):
    conn = get_db_connection()
    if not conn: return "💥 資料庫連線失敗。"
    
    # 這裡進行正規化，確保資料庫比對正確
    normalized_name = normalize_name(reporter_name)
    
    try:
        report_date = datetime.strptime(date_str, '%Y.%m.%d').date()
        with conn.cursor() as cur:
            # 自動補名單 (使用原始名稱)
            cur.execute("INSERT INTO reporters (group_id, reporter_name) VALUES (%s, %s) ON CONFLICT DO NOTHING", (group_id, reporter_name))
            
            # 檢查是否重複 (使用正規化名稱比對)
            # 為了比對，我們需要找出該群組當天已提交的所有 normalized_name
            cur.execute("SELECT reporter_name FROM reports WHERE group_id = %s AND report_date = %s", (group_id, report_date))
            submitted_raw_names = [row[0] for row in cur.fetchall()]
            submitted_normalized = [normalize_name(n) for n in submitted_raw_names]
            
            if normalized_name in submitted_normalized:
                 return f"⚠️ {reporter_name} ({date_str}) 今天已經回報過了！\n\n別想靠重複交作業刷存在感，我看的很清楚 👀"

            # 插入報告 (儲存原始名稱和內容)
            cur.execute(
                "INSERT INTO reports (group_id, reporter_name, report_date, report_content) VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (group_id, reporter_name, report_date, content)
            )
            conn.commit()
            return f"👌 收到！{reporter_name} ({date_str}) 的心得已成功登入檔案。\n\n（今天有乖，給你一個隱形貼紙 ⭐）"
    except ValueError:
        return "❌ 日期格式錯誤 (YYYY.MM.DD)。"
    except Exception as e:
        print(f"LOG ERROR: {e}", file=sys.stderr)
        return "💥 發生錯誤，請聯繫管理員。"
    finally:
        conn.close()

def set_global_pause(state):
    conn = get_db_connection()
    if not conn: return "💥 DB Error"
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE settings SET value = %s WHERE key = 'is_paused'", (state,))
            conn.commit()
        status = "暫停" if state == 'true' else "恢復"
        return f"⚙️ 全域回報提醒已 **{status}**。"
    finally:
        conn.close()

def test_daily_reminder(group_id):
    if group_id in EXCLUDE_GROUP_IDS:
         return "🚫 測試群組 (Excluded) - 排程器將跳過此處。"
    return "🔔 測試指令 OK！請等待排程器執行或檢查 Log。"


# --- LINE Webhook ---
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
    # 取得 Group ID
    group_id = None
    if isinstance(event.source, SourceGroup): group_id = event.source.group_id
    elif isinstance(event.source, SourceRoom): group_id = event.source.room_id
    elif isinstance(event.source, SourceUser): group_id = event.source.user_id
    
    if not group_id: return

    # 預處理：全形轉半形，取第一行作為指令判斷
    processed_text = text.strip().replace('（', '(').replace('）', ')')
    first_line = processed_text.split('\n')[0].strip()
    
    reply = None

    # --- 1. 優先處理：系統指令 (最高優先級) ---
    if first_line in ["指令", "幫助", "help"]:
        reply = (
            "🤖 **功能選單**\n\n"
            "📝 **回報**: `YYYY.MM.DD [姓名]`\n"
            "👥 **管理**: `新增人名 [姓名]`, `刪除人名 [姓名]`, `查詢名單`\n"
            "⚙️ **AI**: `開啟智能模式`, `關閉智能模式`\n"
            "🔧 **系統**: `測試排程`, `暫停回報提醒`, `恢復回報提醒`"
        )
    elif first_line == "暫停回報提醒": reply = set_global_pause('true')
    elif first_line == "恢復回報提醒": reply = set_global_pause('false')
    elif first_line in ["發送提醒測試", "測試排程"]: reply = test_daily_reminder(group_id)
    
    # AI 模式切換
    elif first_line == "開啟智能模式": reply = set_group_mode(group_id, 'AI')
    elif first_line == "關閉智能模式": reply = set_group_mode(group_id, 'NORMAL')

    # --- 2. 次要處理：回報與名單管理 (必須攔截，不可讓 AI 搶話) ---
    if not reply:
        # 名單管理
        match_add = re.match(r"^新增人名[\s　]+(.+)$", first_line)
        if match_add: reply = add_reporter(group_id, match_add.group(1).strip())

        match_del = re.match(r"^刪除人名[\s　]+(.+)$", first_line)
        if match_del: reply = delete_reporter(group_id, match_del.group(1).strip())

        if first_line in ["查詢名單", "查看人員", "名單", "list"]:
            reply = get_reporter_list(group_id)

        # 回報指令 (YYYY.MM.DD 姓名) - 寬鬆 Regex
        match_report = re.match(r"^(\d{4}\.\d{2}\.\d{2})\s*(?:\(.*\))?\s*(.+?)\s*([\s\S]*)", text, re.DOTALL)
        if match_report:
            date_str = match_report.group(1)
            name_str = match_report.group(2).strip()
            # 如果 Regex 沒抓到內容，或是內容在第一行之後，我們保存原始 text 作為完整紀錄
            content_str = text 
            
            if name_str:
                reply = log_report(group_id, date_str, name_str, content_str)
            else:
                reply = "⚠️ 日期後面請記得加上人名！"

    # --- 3. 最後處理：AI 閒聊 (僅在 AI 模式且無上述指令時觸發) ---
    if not reply and get_group_mode(group_id) == 'AI':
        # 呼叫 Gemini
        reply = generate_ai_reply(text)

    # 發送回覆
    if reply:
        try:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        except Exception as e:
            print(f"REPLY ERROR: {e}", file=sys.stderr)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)