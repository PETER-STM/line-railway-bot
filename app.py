import os
import sys
import re
import json
from datetime import datetime, timedelta
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError, LineBotApiError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, SourceGroup, SourceRoom, SourceUser
import psycopg2
import psycopg2.extras # 引入用於錯誤處理
import google.generativeai as genai 

# --- 姓名正規化工具 (用於確保 VIP 記錄唯一性) ---
def normalize_name(name):
    """
    對人名進行正規化處理，主要移除開頭的班級或編號標記。
    例如: "(三) 浣熊🦝" -> "浣熊🦝"
    """
    # 移除開頭被括號 (圓括號、全形括號、方括號、書名號) 包裹的內容
    # 匹配模式: ^(起始) + 任意空白 + 括號開頭 + 非括號內容(1到10個) + 括號結尾 + 任意空白
    normalized = re.sub(r'^\\s*[（(\\[【][^()\\[\\]]{1,10}[)）\\]】]\\s*', '', name).strip()
    
    # 如果正規化結果為空，返回原始名稱
    return normalized if normalized else name

# --- 環境變數設定 ---
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
DATABASE_URL = os.environ.get('DATABASE_URL')
GOOGLE_API_KEY = os.environ.get('GOOGLE_API_KEY')
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

# 初始化 Gemini AI
if GOOGLE_API_KEY:
    try:
        genai.configure(api_key=GOOGLE_API_KEY)
        ai_model = genai.GenerativeModel('gemini-2.5-flash')
        print("INFO: Gemini AI initialized.")
    except Exception as e:
        print(f"LOG ERROR: Gemini AI initialization failed: {e}", file=sys.stderr)
        ai_model = None
else:
    print("INFO: GOOGLE_API_KEY not set. AI features disabled.")
    ai_model = None


# --- 資料庫連線函式 ---
def get_db_connection():
    try:
        # 使用 DSN (Data Source Name) 連線字串
        conn = psycopg2.connect(DATABASE_URL)
        return conn
    except Exception as e:
        print(f"LOG ERROR: Database connection failed: {e}", file=sys.stderr)
        return None

# --- 資料庫結構初始化/遷移函式 ---
def initialize_db():
    conn = None
    try:
        conn = get_db_connection()
        if not conn: return

        cur = conn.cursor()
        
        # 1. 建立 reports 表 (用於儲存每日心得)
        # **重要變更**: 確保 reports 表格包含 report_content TEXT 欄位
        cur.execute("""
            CREATE TABLE IF NOT EXISTS reports (
                id SERIAL PRIMARY KEY,
                group_id TEXT NOT NULL,
                reporter_name TEXT NOT NULL,
                report_date DATE NOT NULL,
                report_content TEXT,  -- 新增/確保有此欄位
                submission_timestamp TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (group_id, reporter_name, report_date)
            );
        """)

        # **資料庫遷移**: 檢查 reports 表是否有 report_content 欄位，若無則新增 (處理現有部署)
        try:
            cur.execute("SELECT report_content FROM reports LIMIT 0")
        except psycopg2.ProgrammingError:
            print("INFO: Altering reports table to add 'report_content' column.", file=sys.stderr)
            conn.rollback() # 需要 rollback 以清除失敗的 SELECT 查詢
            cur.execute("ALTER TABLE reports ADD COLUMN report_content TEXT;")
            print("INFO: 'report_content' column added successfully.", file=sys.stderr)
        
        # 2. 建立 group_vips 表 (用於儲存各群組的 VIP 名單)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS group_vips (
                id SERIAL PRIMARY KEY,
                group_id TEXT NOT NULL,
                vip_name TEXT NOT NULL,
                UNIQUE (group_id, vip_name)
            );
        """)

        # 3. 建立 group_configs 表 (用於儲存各群組的配置，例如 AI 模式)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS group_configs (
                group_id TEXT PRIMARY KEY,
                ai_mode BOOLEAN DEFAULT FALSE,
                # 其他配置可在此新增
                UNIQUE (group_id)
            );
        """)

        conn.commit()
        print("INFO: DB Schema initialized/migrated.")
    except Exception as e:
        print(f"LOG ERROR: DB Schema initialization failed: {e}", file=sys.stderr)
    finally:
        if conn:
            conn.close()

# 首次運行時執行資料庫初始化
initialize_db()

# --- 心得紀錄函式 (負責儲存到資料庫) ---
# **重要變更**: 新增 report_content 參數
def log_report(group_id, report_date, reporter_name, report_content):
    conn = None
    # 確保名稱被正規化，以便與 VIP 名單比對
    normalized_name = normalize_name(reporter_name) 
    reply_text = None
    try:
        conn = get_db_connection()
        if not conn:
            return "💥 記錄失敗。無法連線到資料庫，請聯繫管理員！"
            
        cur = conn.cursor()

        # SQL: 嘗試插入心得，如果主鍵衝突 (同一人同一天已交)，則更新內容與提交時間
        # **重要變更**: 插入 report_content 欄位，並在衝突時更新它
        cur.execute("""
            INSERT INTO reports (group_id, reporter_name, report_date, report_content)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (group_id, reporter_name, report_date) 
            DO UPDATE SET 
                report_content = EXCLUDED.report_content, 
                submission_timestamp = CURRENT_TIMESTAMP
        """, (group_id, normalized_name, report_date, report_content))
        
        conn.commit()
        
        # 產生回覆訊息
        report_date_str = report_date.strftime('%Y/%m/%d')
        reply_text = f"✅ 【{normalized_name}】 在 {report_date_str} 的心得已成功記錄！\n\n（內容已自動存入資料庫。）"

    except psycopg2.Error as e:
        # 捕獲所有 psycopg2 相關錯誤
        print(f"LOG ERROR: Report logging failed for {normalized_name}: {e}", file=sys.stderr)
        reply_text = f"💥 記錄失敗。發生資料庫錯誤 (代碼: {e.pgcode})，請聯繫管理員！"
    except Exception as e:
        print(f"LOG ERROR: Report logging failed for {normalized_name}: {e}", file=sys.stderr)
        reply_text = "💥 記錄失敗。發生未知錯誤，請聯繫管理員！"
    finally:
        if conn:
            conn.close()
    return reply_text

# --- 輔助函式：取得群組/聊天室 ID ---
def get_source_id(source):
    if isinstance(source, SourceGroup):
        return source.group_id
    elif isinstance(source, SourceRoom):
        return source.room_id
    elif isinstance(source, SourceUser):
        return source.user_id # 在單人聊天中，使用用戶 ID
    return "UNKNOWN_SOURCE"


# --- AI 回覆生成函式 ---
def generate_ai_reply(prompt):
    if not ai_model:
        return "AI 助理未啟用，請檢查 GOOGLE_API_KEY 設定。"
    
    try:
        # 使用 Google Search Tool 進行接地氣 (Grounded) 回答
        config = {
            "systemInstruction": "你是一位親切、樂於助人的 LINE 聊天機器人助理。請使用繁體中文和親切的語氣來回覆使用者。",
            "tools": [{"google_search": {}}]
        }
        
        response = ai_model.generate_content(
            prompt,
            config=config
        )
        
        return response.text
    except Exception as e:
        print(f"LOG ERROR: AI generation failed: {e}", file=sys.stderr)
        return "🤖 抱歉，AI 系統忙碌中，請稍後再試。"

# --- 取得群組 AI 模式狀態 ---
def get_group_mode(group_id):
    conn = None
    try:
        conn = get_db_connection()
        if not conn: return 'OFF' # 預設關閉
        cur = conn.cursor()
        cur.execute("SELECT ai_mode FROM group_configs WHERE group_id = %s", (group_id,))
        result = cur.fetchone()
        if result and result[0]:
            return 'AI'
        
        # 檢查 VIP 模式 (如果群組未設定 AI 模式，則可能是 VIP 模式)
        cur.execute("SELECT COUNT(*) FROM group_vips WHERE group_id = %s", (group_id,))
        vip_count = cur.fetchone()[0]
        if vip_count > 0:
            return 'VIP'
            
        return 'OFF'
    except Exception as e:
        print(f"LOG ERROR: Failed to get group mode for {group_id}: {e}", file=sys.stderr)
        return 'OFF'
    finally:
        if conn:
            conn.close()

# --- 設定群組 AI 模式 ---
def set_group_mode(group_id, enable_ai):
    conn = None
    try:
        conn = get_db_connection()
        if not conn: return "💥 設定失敗：無法連線到資料庫。"
        cur = conn.cursor()
        
        # 使用 INSERT OR UPDATE 確保記錄存在
        cur.execute("""
            INSERT INTO group_configs (group_id, ai_mode)
            VALUES (%s, %s)
            ON CONFLICT (group_id) 
            DO UPDATE SET ai_mode = EXCLUDED.ai_mode
        """, (group_id, enable_ai))
        
        conn.commit()
        return f"✅ AI 閒聊模式已{'開啟' if enable_ai else '關閉'}！"
    except Exception as e:
        print(f"LOG ERROR: Failed to set AI mode for {group_id}: {e}", file=sys.stderr)
        return "💥 設定失敗：資料庫操作錯誤。"
    finally:
        if conn:
            conn.close()

# --- LINE 訊息處理 ---
@handler.add(MessageEvent, message=TextMessage)
def handle_text_message(event):
    message = event.message
    text = message.text.strip()
    group_id = get_source_id(event.source)
    reply_text = None

    # 排除特定群組/ID (主要用於測試隔離)
    if group_id in EXCLUDE_GROUP_IDS:
        print(f"INFO: Message from excluded ID {group_id} ignored.", file=sys.stderr)
        return

    # --- 1. 指令處理 (/help, /add, /mode) ---
    if text.startswith('/'):
        parts = text.split()
        command = parts[0].lower()
        args = parts[1:]
        
        if command == '/help':
            reply_text = (
                "🤖 心得紀錄與 AI 助理 v3.0 指令清單：\n\n"
                "1. **心得提交**：直接貼上您的心得內容，格式須包含：`YYYY.MM.DD 姓名` (例如: `2025.11.20 邦妮...`)。\n\n"
                "2. **/mode on** 或 **/mode off**：開啟/關閉 AI 閒聊模式。開啟後，非指令訊息會由 AI 回覆。\n\n"
                "3. **/add vip [姓名]**：將成員加入 VIP 名單（用於催繳提醒）。\n\n"
                "4. **/del vip [姓名]**：將成員從 VIP 名單移除。\n\n"
                "5. **/list vip**：查看當前 VIP 名單。\n\n"
                "6. **/help**：顯示此幫助訊息。"
            )
        
        # --- VIP 名單管理 ---
        elif command == '/add' and args and args[0].lower() == 'vip' and len(args) == 2:
            vip_name = normalize_name(args[1])
            if vip_name:
                reply_text = manage_vip_list(group_id, vip_name, 'ADD')
            else:
                reply_text = "❌ 請提供有效的人名！"

        elif command == '/del' and args and args[0].lower() == 'vip' and len(args) == 2:
            vip_name = normalize_name(args[1])
            if vip_name:
                reply_text = manage_vip_list(group_id, vip_name, 'DEL')
            else:
                reply_text = "❌ 請提供有效的人名！"

        elif command == '/list' and args and args[0].lower() == 'vip':
            reply_text = manage_vip_list(group_id, None, 'LIST')

        # --- AI 模式切換 ---
        elif command == '/mode' and args and args[0].lower() == 'on':
            reply_text = set_group_mode(group_id, True)
        elif command == '/mode' and args and args[0].lower() == 'off':
            reply_text = set_group_mode(group_id, False)

    # --- 2. 心得紀錄 (例如: 2025.11.20 邦妮 + 內容) ---
    # 目的：從訊息中尋找日期和人名，並將整則訊息內容視為 report_content
    # **重要變更**: 調整解析邏輯以適應用戶的自由格式
    
    match_report = None
    reporter_name = ""
    report_content = ""

    # 嘗試在第一行尋找日期和名字的模式 (例如: 2025.11.20（四）邦妮)
    first_line = text.split('\\n')[0]
    
    # 1. 尋找日期: (\d{4}[./]\d{2}[./]\d{2})
    match_date = re.search(r"(\d{4}[./]\\d{2}[./]\\d{2})", first_line)

    if match_date:
        date_str = match_date.group(1) 
        
        # 2. 尋找名字: 在整個第一行中，尋找最後一個連續的 2-4 個中文字作為回報者名稱
        # 這能穩健地從 "05:18 施恩澤 2025.11.20（四）邦妮" 中抓到 "邦妮"
        name_candidates = re.findall(r'[\u4e00-\u9fa5]{2,4}', first_line)
        if name_candidates:
            # 確保名字不是日期中的數字 (雖然中文名通常不會是數字)
            # 只要找到名字就用它
            reporter_name = name_candidates[-1] 
        else:
            reporter_name = ""
        
        # 3. 完整內容: 將整則訊息作為心得內容
        report_content = text
        
        try:
            # 轉換分隔符號為點號，以便統一解析
            date_str = date_str.replace('/', '.') 
            report_date = datetime.strptime(date_str, '%Y.%m.%d').date()
            
            # 確保人名不為空 (這是我們唯一強制的要求)
            if not reporter_name:
                reply_text = "⚠️ 訊息中找不到人名（2-4個中文字），請確認你的格式：YYYY.MM.DD 姓名 + 內容！"
            else:
                # **重要變更**: 呼叫 log_report，傳入完整內容
                reply_text = log_report(group_id, report_date, reporter_name, report_content) 
            
        except ValueError:
            # 日期格式錯誤 (通常不會發生，因為前面已經匹配成功)
            reply_text = "❌ 日期長得怪怪的。\\n\\n請用標準格式：YYYY.MM.DD + 內容\\n\\n（小數點不是你的自由發揮。）"

    # --- 3. AI 閒聊 (若非指令、非心得，且 AI 模式開啟) ---
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
            # 如果 reply_message 失敗，嘗試 PUSH 訊息 (通常發生在回覆逾時)
            print(f"LINE API REPLY ERROR: {e}. Trying push message.", file=sys.stderr)
            try:
                line_bot_api.push_message(
                    group_id,
                    TextSendMessage(text=reply_text)
                )
            except LineBotApiError as push_e:
                print(f"LINE API PUSH ERROR: {push_e}", file=sys.stderr)
            
# --- VIP 名單管理函式 ---
def manage_vip_list(group_id, vip_name, action):
    conn = None
    try:
        conn = get_db_connection()
        if not conn: return "💥 無法連線到資料庫。"
        cur = conn.cursor()
        
        if action == 'ADD':
            # 插入 VIP 名字，如果重複則忽略 (ON CONFLICT DO NOTHING)
            cur.execute("""
                INSERT INTO group_vips (group_id, vip_name)
                VALUES (%s, %s)
                ON CONFLICT (group_id, vip_name) DO NOTHING
            """, (group_id, vip_name))
            conn.commit()
            if cur.rowcount > 0:
                return f"✅ VIP 成員【{vip_name}】已成功加入！"
            else:
                return f"ℹ️ VIP 成員【{vip_name}】已經在名單中了！"

        elif action == 'DEL':
            cur.execute("""
                DELETE FROM group_vips
                WHERE group_id = %s AND vip_name = %s
            """, (group_id, vip_name))
            conn.commit()
            if cur.rowcount > 0:
                return f"✅ VIP 成員【{vip_name}】已從名單中移除。"
            else:
                return f"ℹ️ 名單中找不到 VIP 成員【{vip_name}】。"

        elif action == 'LIST':
            cur.execute("""
                SELECT vip_name FROM group_vips
                WHERE group_id = %s
                ORDER BY vip_name
            """, (group_id,))
            vips = [row[0] for row in cur.fetchall()]
            
            if vips:
                vip_list = "\\n".join([f"- {name}" for name in vips])
                return f"📜 當前 VIP 名單（{len(vips)} 人）：\\n{vip_list}"
            else:
                return "ℹ️ VIP 名單目前是空的。請使用 `/add vip [姓名]` 新增。"

    except Exception as e:
        print(f"LOG ERROR: VIP management failed: {e}", file=sys.stderr)
        return "💥 資料庫操作失敗，請稍後再試。"
    finally:
        if conn:
            conn.close()

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
        print(f"LINE Webhook handler error: {e}", file=sys.stderr)
        abort(500)
        
    return 'OK'

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=os.environ.get('PORT', 8080))