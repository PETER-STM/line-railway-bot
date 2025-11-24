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
from google.generativeai.errors import APIError # 引入 APIError 以便處理模型錯誤

# --- 姓名正規化工具 (用於確保 VIP 記錄唯一性，並解決重複名稱問題) ---
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
# NEW: 預設使用 gemini-2.5-flash，修復 gemini-1.5-flash 404 錯誤
GEMINI_MODEL_NAME = os.environ.get('GEMINI_MODEL_NAME', 'gemini-2.5-flash') 
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

# 初始化 Gemini AI (如果有提供 KEY)
if GOOGLE_API_KEY:
    try:
        genai.configure(api_key=GOOGLE_API_KEY)
        print("INFO: Gemini AI configured.", file=sys.stderr)
    except Exception as e:
        print(f"WARNING: Failed to configure Gemini AI: {e}", file=sys.stderr)
else:
    print("INFO: GOOGLE_API_KEY not found. AI chat feature disabled.", file=sys.stderr)

# --- 資料庫連線函式 ---
def get_db_connection():
    return psycopg2.connect(DATABASE_URL)

# --- 資料庫初始化 ---
def initialize_db_schema():
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # 1. reports table (心得/打卡紀錄)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS reports (
                id SERIAL PRIMARY KEY,
                group_id VARCHAR(50) NOT NULL,
                report_date DATE NOT NULL,
                reporter_name VARCHAR(100) NOT NULL,
                normalized_name VARCHAR(100) NOT NULL,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (group_id, report_date, normalized_name)
            );
        """)
        
        # 2. vip_list table (群組 VIP 名單)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS vip_list (
                id SERIAL PRIMARY KEY,
                group_id VARCHAR(50) NOT NULL,
                vip_name VARCHAR(100) NOT NULL,
                normalized_name VARCHAR(100) NOT NULL,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (group_id, normalized_name)
            );
        """)

        # 3. group_settings table (新增：群組設定，用於 AI 開關)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS group_settings (
                group_id VARCHAR(50) PRIMARY KEY,
                ai_mode VARCHAR(10) NOT NULL DEFAULT 'OFF' -- 'OFF' or 'AI'
            );
        """)
        
        conn.commit()
        print("INFO: DB Schema initialized.", file=sys.stderr)
    except Exception as e:
        print(f"DB Schema initialization FAILED: {e}", file=sys.stderr)
    finally:
        if conn: conn.close()

# 確保資料庫在啟動時初始化
initialize_db_schema()

# --- 活潑・幽默・微毒舌 回覆模板 ---
UNIVERSAL_REPLY = [
    "✅ 收到！你的心得（或打卡紀錄）已像閃電一樣被我記下了！",
    "👍 紀錄完成！你今天超棒der~",
    "🎉 Good job！我已經把這筆光榮紀錄存進資料庫了，逃不掉囉！",
    "💾 登錄成功！看來你還是個守紀律的好孩子嘛！",
    "👀 記住了！明天繼續，不然我會派催繳大隊去你家站崗！",
]

# --- 資料庫操作函式 ---

# 記錄心得/打卡
def log_report(group_id, report_date, reporter_name):
    """
    記錄心得/打卡到資料庫。
    """
    conn = None
    try:
        normalized_name = normalize_name(reporter_name)
        if not normalized_name:
            return "❌ 姓名正規化失敗，請確保姓名不是只有括號！"

        conn = get_db_connection()
        cur = conn.cursor()

        # 1. 嘗試插入心得紀錄
        cur.execute("""
            INSERT INTO reports (group_id, report_date, reporter_name, normalized_name)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (group_id, report_date, normalized_name)
            DO UPDATE SET 
                reporter_name = EXCLUDED.reporter_name,
                created_at = CURRENT_TIMESTAMP
            RETURNING id;
        """, (group_id, report_date, reporter_name, normalized_name))
        
        # 2. 確保 VIP 名單中有此人 (如果沒有，則新增)
        cur.execute("""
            INSERT INTO vip_list (group_id, vip_name, normalized_name)
            VALUES (%s, %s, %s)
            ON CONFLICT (group_id, normalized_name)
            DO UPDATE SET vip_name = EXCLUDED.vip_name
            RETURNING id;
        """, (group_id, reporter_name, normalized_name))
        
        conn.commit()

        # 根據是否為更新來選擇回覆 (雖然 ON CONFLICT DO UPDATE 總是返回 1 行，但邏輯上還是依賴 DB 操作)
        import random
        return random.choice(UNIVERSAL_REPLY)

    except Exception as e:
        if conn: conn.rollback()
        print(f"DB Report LOG FAILED: {e}", file=sys.stderr)
        return "🔥 資料庫炸了... 你的紀錄沒存到啦！快找工程師！"
    finally:
        if conn: conn.close()

# 取得群組模式 (AI 開關)
def get_group_mode(group_id):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT ai_mode FROM group_settings WHERE group_id = %s;", (group_id,))
        result = cur.fetchone()
        return result[0] if result else 'OFF' # 預設為 'OFF'
    except Exception as e:
        print(f"DB Get Group Mode FAILED: {e}", file=sys.stderr)
        return 'OFF'
    finally:
        if conn: conn.close()

# 設定群組模式 (AI 開關)
def set_group_mode(group_id, mode):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO group_settings (group_id, ai_mode)
            VALUES (%s, %s)
            ON CONFLICT (group_id)
            DO UPDATE SET ai_mode = EXCLUDED.ai_mode;
        """, (group_id, mode))
        conn.commit()
        return f"AI 模式已切換為：『{mode}』！"
    except Exception as e:
        if conn: conn.rollback()
        print(f"DB Set Group Mode FAILED: {e}", file=sys.stderr)
        return "AI 模式切換失敗，請重試！"
    finally:
        if conn: conn.close()

# --- AI 生成回覆函式 ---
def generate_ai_reply(prompt):
    """
    使用 Gemini 模型生成回覆。
    """
    if not GOOGLE_API_KEY:
        return "AI 聊天功能未開啟 (缺少 GOOGLE_API_KEY 環境變數)。"

    # 設定 AI 角色和系統指令
    system_instruction = (
        "你是一個活潑、幽默、帶有微毒舌風格的 LINE Bot 助理。你的主要職責是協助記錄學員的每日心得/打卡。 "
        "當被問到心得記錄相關問題時，請專業地回答；當被問到與記錄無關的問題時，請用幽默或微毒舌的語氣閒聊。 "
        "回答請簡潔，不要超過 3 句話。"
    )

    try:
        # 使用 genai.GenerativeModel 進行配置
        model = genai.GenerativeModel(
            model_name=GEMINI_MODEL_NAME, # 使用環境變數或預設的 gemini-2.5-flash
            system_instruction=system_instruction
        )
        
        # 執行內容生成
        response = model.generate_content(prompt)
        
        # 檢查是否有內容
        if response.text:
            return response.text.strip()
        else:
            return "AI 助理今天在午休，請晚點再試。（可能是問了太難的問題啦！）"

    except APIError as e:
        print(f"Gemini API Error: {e}", file=sys.stderr)
        # 捕捉到 404 錯誤時的特定提示
        if "404" in str(e) and GEMINI_MODEL_NAME == 'gemini-1.5-flash':
             return "AI 助理表示：『系統出錯了！好像是模型名稱被換掉了。工程師，請把 model 換成 gemini-2.5-flash！』"
        return "AI 助理表示：『系統出錯了！你的問題太犀利，我當機了。』"
    except Exception as e:
        print(f"General AI Error: {e}", file=sys.stderr)
        return "AI 助理表示：『系統出錯了！你的問題太犀利，我當機了。』"

# --- LINE 訊息處理 ---
@handler.add(MessageEvent, message=TextMessage)
def handle_text_message(event):
    text = event.message.text.strip()
    source = event.source
    reply_text = None
    
    # 只處理群組和聊天室
    if not isinstance(source, (SourceGroup, SourceRoom)):
        if isinstance(source, SourceUser):
             reply_text = "嗨！我主要在 LINE 群組/聊天室服務喔！請把我加進去，才能幫大家記錄心得跟催交啦！"
        else:
             print("Source type not recognized.", file=sys.stderr)
             return

    # 取得群組 ID
    group_id = source.group_id if isinstance(source, SourceGroup) else (
               source.room_id if isinstance(source, SourceRoom) else None)
    
    if not group_id or group_id in EXCLUDE_GROUP_IDS:
        print(f"Skipping event from excluded or invalid source: {group_id}", file=sys.stderr)
        return

    # 1. 指令模式 (AI 開關與狀態查詢)
    command = text.lower()
    if command == '/ai on':
        reply_text = set_group_mode(group_id, 'AI')
    elif command == '/ai off':
        reply_text = set_group_mode(group_id, 'OFF')
    elif command == '/狀態' or command == '/status':
        mode = get_group_mode(group_id)
        reply_text = (
            f"🚨 目前模式：『{mode}』\n\n"
            f"📢 心得記錄格式：YYYY.MM.DD 姓名\n\n"
            f"💡 AI 閒聊開關：\n"
            f"- 輸入 /ai on 開啟\n"
            f"- 輸入 /ai off 關閉"
        )


    # 2. 心得/打卡記錄模式 (優先處理，如果指令模式未觸發)
    if not reply_text:
        # 格式：YYYY.MM.DD 姓名 或 YYYY/MM/DD 姓名
        # 正則表達式： (\d{4}[./]\\d{2}[./]\\d{2})\s+(.+)
        match_report = re.match(r"^(\d{4}[./]\\d{2}[./]\\d{2})\\s+(.+)$", text)
        
        if match_report:
            date_str = match_report.group(1) # 日期是第一個捕獲組
            name_str = match_report.group(2).strip() # 人名是第二個捕獲組

            try:
                # 轉換分隔符號為點號，以便統一解析
                date_str = date_str.replace('/', '.') 
                report_date = datetime.strptime(date_str, '%Y.%m.%d').date()
                reporter_name = name_str
                
                # 確保人名不為空
                if not reporter_name:
                    # 記錄回報 (人名遺失) 模板
                    reply_text = "⚠️ 日期後面請記得加上人名，不然我不知道誰交的啊！\n\n（你總不會想讓我自己猜吧？）"
                else:
                    # 呼叫 log_report，只記錄打卡資訊
                    reply_text = log_report(group_id, report_date, reporter_name)
                
            except ValueError:
                # 記錄回報 (日期格式錯誤) 模板
                reply_text = "❌ 日期長得怪怪的。\n\n請用標準格式：YYYY.MM.DD 姓名\n\n（小數點不是你的自由發揮。）"

    # --- 3. AI 閒聊 (如果沒有觸發記錄或指令，且 AI 模式開啟) ---
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
            # 如果 reply_message 失敗，嘗試 push_message (例如：超過 3 秒回覆期限)
            try:
                line_bot_api.push_message(group_id, TextSendMessage(text=reply_text))
            except LineBotApiError as push_e:
                print(f"LINE API PUSH/REPLY ERROR: {push_e}", file=sys.stderr)
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
    except LineBotApiError as e:
        print(f"LINE Bot API Error: {e}", file=sys.stderr)
        abort(500)
    return 'OK'

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)