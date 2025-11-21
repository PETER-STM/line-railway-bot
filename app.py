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

# --- 資料庫初始化 (省略，與原程式碼相同) ---
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

# --- 姓名正規化工具 (保持原程式碼 L105) ---
def normalize_name(name):
    # 移除開頭括號內容 (如：(三) 浣熊 -> 浣熊)
    normalized = re.sub(r'^\s*[（(\[【][^()\[\]]{1,10}[)）\]】]\s*', '', name).strip()
    return normalized if normalized else name

# --- AI 相關函式 (保持原程式碼 L114-L162) ---

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
    if not conn: return "💥 發生未知錯誤。\n\n可能是宇宙磁場不順，或系統在叛逆。\n稍後再試，或找管理員用愛感化它。" # 通用錯誤模板
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO group_modes (group_id, mode) VALUES (%s, %s)
                ON CONFLICT (group_id) DO UPDATE SET mode = EXCLUDED.mode
            """, (group_id, mode))
            conn.commit()
        status_text = "🤖 智能對話 (AI)" if mode == 'AI' else "🔇 一般安靜 (NORMAL)"
        return f"🔄 模式已切換為：**{status_text}**" # 模式切換沿用較為中性的回覆
    except Exception as e:
        print(f"MODE SET ERROR: {e}", file=sys.stderr)
        return "💥 發生未知錯誤。\n\n可能是宇宙磁場不順，或系統在叛逆。\n稍後再試，或找管理員用愛感化它。"
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

# --- 核心指令與資料庫操作 ---

def add_reporter(group_id, reporter_name):
    conn = get_db_connection()
    if not conn: return "💥 發生未知錯誤。\n\n可能是宇宙磁場不順，或系統在叛逆。\n稍後再試，或找管理員用愛感化它。"
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO reporters (group_id, reporter_name) VALUES (%s, %s) ON CONFLICT DO NOTHING", (group_id, reporter_name))
            if cur.rowcount > 0:
                conn.commit()
                # ✅ 新版：新增人名 (成功)
                return f"🎉 好嘞～ {reporter_name} 已成功加入名單！\n\n（逃不掉了，祝他順利回報。）"
            # ✅ 新版：新增人名 (重複)
            return f"🤨 {reporter_name} 早就在名單裡面坐好坐滿了，\n\n你該不會…忘記上一次也加過吧？"
    finally:
        conn.close()

# 修正的 delete_reporter 函式 (保留邏輯修正，替換回覆語氣)
def delete_reporter(group_id, reporter_name):
    conn = get_db_connection()
    if not conn: return "💥 發生未知錯誤。\n\n可能是宇宙磁場不順，或系統在叛逆。\n稍後再試，或找管理員用愛感化它。"
    
    # 1. 為了確保刪除成功，先找出資料庫中匹配的原始名稱
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT reporter_name FROM reporters WHERE group_id = %s", (group_id,))
            all_raw_names = [row[0] for row in cur.fetchall()]
        
        normalized_input = normalize_name(reporter_name) 
        
        target_raw_name = None
        for raw_name in all_raw_names:
            if normalize_name(raw_name) == normalized_input:
                target_raw_name = raw_name
                break
        
        if not target_raw_name:
             # ❓名單裡根本沒有 (未找到)
             return f"❓名單裡根本沒有 {reporter_name} 啊！\n\n是不是名字打錯，還是你其實不想他回報？"

        # 2. 執行刪除 (使用找到的原始名稱 target_raw_name)
        with conn.cursor() as cur:
            cur.execute("DELETE FROM reporters WHERE group_id = %s AND reporter_name = %s", (group_id, target_raw_name))
            cur.execute("DELETE FROM reports WHERE group_id = %s AND reporter_name = %s", (group_id, target_raw_name))
            conn.commit()
            # 🗑️ 刪除人名 (成功)
            return f"🗑️ {target_raw_name} 已從名單中被溫柔移除。\n\n（放心，我沒有把人綁走，只是移出名單。）"
            
    except Exception as e:
        print(f"DELETE ERROR: {e}", file=sys.stderr)
        return "💥 發生未知錯誤。\n\n可能是宇宙磁場不順，或系統在叛逆。\n稍後再試，或找管理員用愛感化它。"
    finally:
        conn.close()


def get_reporter_list(group_id):
    conn = get_db_connection()
    if not conn: return "💥 發生未知錯誤。\n\n可能是宇宙磁場不順，或系統在叛逆。\n稍後再試，或找管理員用愛感化它。"
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT reporter_name FROM reporters WHERE group_id = %s ORDER BY reporter_name", (group_id,))
            reporters = [row[0] for row in cur.fetchall()]
            if reporters:
                # 📋 查詢名單 (有成員)
                return f"📋 最新回報觀察名單如下：\n" + "\n".join(reporters) + "\n\n（嗯，看起來大家都還活著。）"
            # 📭 查詢名單 (無成員)
            return "📭 名單空空如也～\n\n快用 `新增人名 [姓名]` 把第一位勇者召喚進來吧！"
    finally:
        conn.close()

def log_report(group_id, date_str, reporter_name, content):
    conn = get_db_connection()
    if not conn: return "💥 發生未知錯誤。\n\n可能是宇宙磁場不順，或系統在叛逆。\n稍後再試，或找管理員用愛感化它。"
    
    normalized_name = normalize_name(reporter_name) 
    
    try:
        report_date = datetime.strptime(date_str, '%Y.%m.%d').date()
        with conn.cursor() as cur:
            # 自動補名單 (使用原始名稱)
            cur.execute("INSERT INTO reporters (group_id, reporter_name) VALUES (%s, %s) ON CONFLICT DO NOTHING", (group_id, reporter_name))
            
            # 檢查是否重複 (使用正規化名稱比對)
            cur.execute("SELECT reporter_name FROM reports WHERE group_id = %s AND report_date = %s", (group_id, report_date))
            submitted_raw_names = [row[0] for row in cur.fetchall()]
            submitted_normalized = [normalize_name(n) for n in submitted_raw_names]
            
            if normalized_name in submitted_normalized:
                 # ⚠️ 記錄回報 (重複記錄)
                 return f"⚠️ {reporter_name} ({date_str}) 今天已經回報過了！\n\n別想靠重複交作業刷存在感，我看的很清楚 👀"

            # 插入報告 (儲存原始名稱和內容)
            cur.execute(
                "INSERT INTO reports (group_id, reporter_name, report_date, report_content) VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (group_id, reporter_name, report_date, content)
            )
            conn.commit()
            # 👌 記錄回報 (成功)
            return f"👌 收到！{reporter_name} ({date_str}) 的心得已成功登入檔案。\n\n（今天有乖，給你一個隱形貼紙 ⭐）"
    except ValueError:
        # ❌ 記錄回報 (日期格式錯誤)
        return "❌ 日期長得怪怪的。\n\n請用標準格式：YYYY.MM.DD 姓名\n\n（小數點不是你的自由發揮。）"
    except Exception as e:
        print(f"LOG ERROR: {e}", file=sys.stderr)
        return "💥 發生未知錯誤。\n\n可能是宇宙磁場不順，或系統在叛逆。\n稍後再試，或找管理員用愛感化它。"
    finally:
        conn.close()

def set_global_pause(state):
    conn = get_db_connection()
    if not conn: return "💥 發生未知錯誤。\n\n可能是宇宙磁場不順，或系統在叛逆。\n稍後再試，或找管理員用愛感化它。"
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE settings SET value = %s WHERE key = 'is_paused'", (state,))
            conn.commit()
        status = "暫停" if state == 'true' else "恢復"
        return f"⚙️ 全域回報提醒已 **{status}**。" # 沿用中性回覆
    finally:
        conn.close()

def test_daily_reminder(group_id):
    if group_id in EXCLUDE_GROUP_IDS:
         # 🚫 測試排程 (已排除群組)
         return "🚫 這個群組在「排除名單」裡，\n\n排程器看到這邊會自動裝死，不會發任何提醒。"
    # 🔔 測試排程 (正常群組)
    return "🔔 測試指令 OK！\n\n請坐等排程器在設定時間跳出來嚇你，\n\n以確認系統正常運作。"


# --- LINE Webhook (省略，保持原程式碼 L315-L330) ---
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
    group_id = None
    if isinstance(event.source, SourceGroup): group_id = event.source.group_id
    elif isinstance(event.source, SourceRoom): group_id = event.source.room_id
    elif isinstance(event.source, SourceUser): group_id = event.source.user_id
    
    if not group_id: return

    # 預處理：全形轉半形，取第一行作為指令判斷
    processed_text = text.strip().replace('（', '(').replace('）', ')')
    first_line = processed_text.split('\n')[0].strip()
    
    reply = None

    # --- 1. 優先處理：系統指令 ---
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

    # --- 2. 次要處理：回報與名單管理 ---
    if not reply:
        # 名單管理
        match_add = re.match(r"^新增人名[\s　]+(.+)$", first_line)
        if match_add: reply = add_reporter(group_id, match_add.group(1).strip())

        match_del = re.match(r"^刪除人名[\s　]+(.+)$", first_line)
        if match_del: reply = delete_reporter(group_id, match_del.group(1).strip())

        if first_line in ["查詢名單", "查看人員", "名單", "list"]:
            reply = get_reporter_list(group_id)

        # ⭐️ 修正的回報指令邏輯：確保正確解析姓名 ⭐️
        match_report = re.match(r"^(\d{4}\.\d{2}\.\d{2})\s*([^\n]+?)\s*([\s\S]*)", processed_text, re.DOTALL)
        
        if match_report:
            date_str = match_report.group(1)
            raw_name_with_day = match_report.group(2).strip()
            name_str = normalize_name(raw_name_with_day)
            content_str = text 
            
            if name_str:
                reply = log_report(group_id, date_str, name_str, content_str)
            else:
                # 記錄回報 (人名遺失)
                reply = "⚠️ 日期後面請記得加上人名，不然我不知道誰交的啊！\n\n（你總不會想讓我自己猜吧？）"

    # --- 3. 最後處理：AI 閒聊 ---
    if not reply and get_group_mode(group_id) == 'AI':
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