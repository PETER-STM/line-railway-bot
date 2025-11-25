import os
import sys
import re
from datetime import datetime, timedelta
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError, LineBotApiError, LineBotApiError
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
    # 匹配模式: ^(起始) + 任意空白 + 括號開頭 + 非括號內容(1到10個) + 括號結尾 + 任意空白
    normalized = re.sub(r'^\s*[（(\[【][^()\[\]]{1,10}[)）\]】]\s*', '', name).strip()
    
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
if not DATABASE_URL:
    print("WARNING: DATABASE_URL is missing. DB functions will fail.", file=sys.stderr)

app = Flask(__name__)
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# --- Gemini AI 初始化 ---
# **修正 AI 模型名稱：使用最新且支援的 2.5 Flash 預覽模型**
GEMINI_MODEL = 'gemini-2.5-flash-preview-09-2025'
ai_client = None
if GOOGLE_API_KEY:
    try:
        genai.configure(api_key=GOOGLE_API_KEY)
        ai_client = genai.Client()
        print("Gemini AI client initialized successfully.", file=sys.stderr)
    except Exception as e:
        print(f"Gemini AI client failed to initialize: {e}", file=sys.stderr)


# --- 資料庫連線函式 ---
def get_db_connection():
    # 檢查 DATABASE_URL 是否存在
    if not DATABASE_URL:
        raise Exception("Database URL is not configured.")
    # 設置 SSL mode 為 require
    return psycopg2.connect(DATABASE_URL, sslmode='require')

# --- 資料庫初始化 ---
def create_tables_if_not_exist():
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # 1. groups 表：儲存群組設定 (如 VIP 名單, 模式)
        # mode: 'CHECKIN' (打卡模式, 預設) 或 'AI' (AI 閒聊模式)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS groups (
                group_id VARCHAR(50) PRIMARY KEY,
                vip_list TEXT NOT NULL DEFAULT '', -- 逗號分隔的正規化人名
                mode VARCHAR(10) NOT NULL DEFAULT 'CHECKIN', -- 模式：'CHECKIN' 或 'AI'
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            );
        """)

        # 2. reports 表：儲存每日打卡紀錄
        # **修正：移除 report_content 欄位，因為目前邏輯只記錄打卡狀態 (日期+人名)**
        cur.execute("""
            CREATE TABLE IF NOT EXISTS reports (
                id SERIAL PRIMARY KEY,
                group_id VARCHAR(50) NOT NULL,
                report_date DATE NOT NULL,
                reporter_name VARCHAR(100) NOT NULL,
                normalized_reporter_name VARCHAR(100) NOT NULL,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                
                -- 確保同一群組、同一天、同一個人 (正規化後) 只有一筆紀錄
                UNIQUE (group_id, normalized_reporter_name, report_date)
            );
        """)
        
        conn.commit()
        print("Database tables ensured to exist.", file=sys.stderr)
    except Exception as e:
        print(f"DB INIT ERROR: {e}", file=sys.stderr)
    finally:
        if conn: conn.close()

# 確保在應用程式啟動時執行資料庫初始化
create_tables_if_not_exist()


# --- 資料庫操作：紀錄打卡報告 ---
def log_report(group_id, report_date, reporter_name):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # 取得正規化名稱
        normalized_name = normalize_name(reporter_name)

        # **修正 SQL 語句：移除 report_content 欄位及其值**
        cur.execute("""
            INSERT INTO reports (group_id, report_date, reporter_name, normalized_reporter_name)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (group_id, normalized_reporter_name, report_date) 
            DO UPDATE SET 
                reporter_name = EXCLUDED.reporter_name, 
                created_at = NOW();
        """, (group_id, report_date, reporter_name, normalized_name))
        
        conn.commit()
        
        return f"✅ {report_date.strftime('%Y/%m/%d')} 的心得已記錄！感謝 {normalized_name}。"
        
    except Exception as e:
        print(f"DB LOG REPORT ERROR: {e}", file=sys.stderr)
        return "⚠️ 抱歉，資料庫記錄打卡失敗了...請稍後再試。"
    finally:
        if conn: conn.close()


# --- 資料庫操作：設定/更新 VIP 名單 ---
def set_vip_list(group_id, vip_names_str):
    conn = None
    try:
        # 將輸入的 VIP 名單字串分割並正規化
        raw_names = [name.strip() for name in vip_names_str.split(',') if name.strip()]
        normalized_names = sorted(list(set([normalize_name(name) for name in raw_names])))
        
        # 將正規化後的名單存回字串，以逗號分隔
        vip_list_normalized = ','.join(normalized_names)
        
        conn = get_db_connection()
        cur = conn.cursor()
        
        # 使用 INSERT ... ON CONFLICT DO UPDATE 處理不存在的群組
        cur.execute("""
            INSERT INTO groups (group_id, vip_list)
            VALUES (%s, %s)
            ON CONFLICT (group_id) DO UPDATE SET 
                vip_list = EXCLUDED.vip_list,
                updated_at = NOW();
        """, (group_id, vip_list_normalized,))
        
        conn.commit()
        
        if not normalized_names:
            return "🗑️ VIP 名單已清空。"
        else:
            list_of_names = "\n".join([f"- {name}" for name in normalized_names])
            return f"🌟 VIP 名單設定成功！\n共 {len(normalized_names)} 人：\n{list_of_names}\n\n請以「YYYY.MM.DD 姓名」格式發送心得來打卡。"
            
    except Exception as e:
        print(f"DB SET VIP ERROR: {e}", file=sys.stderr)
        return "⚠️ 抱歉，資料庫設定 VIP 名單失敗了...請稍後再試。"
    finally:
        if conn: conn.close()


# --- 資料庫操作：取得 VIP 名單 ---
def get_vip_list(group_id):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT vip_list FROM groups WHERE group_id = %s;", (group_id,))
        result = cur.fetchone()
        
        if result and result[0]:
            vip_list_str = result[0]
            # 返回原始逗號分隔字串
            return [name.strip() for name in vip_list_str.split(',') if name.strip()]
        return []
    except Exception as e:
        print(f"DB GET VIP ERROR: {e}", file=sys.stderr)
        return [] # 失敗時返回空列表
    finally:
        if conn: conn.close()

# --- 資料庫操作：設定群組模式 ---
def set_group_mode(group_id, mode):
    conn = None
    try:
        mode = mode.upper()
        if mode not in ('CHECKIN', 'AI'):
            return "❌ 模式設定錯誤。請使用 'CHECKIN' 或 'AI'。"
            
        conn = get_db_connection()
        cur = conn.cursor()
        
        # 使用 INSERT ... ON CONFLICT DO UPDATE 確保群組記錄存在
        cur.execute("""
            INSERT INTO groups (group_id, mode)
            VALUES (%s, %s)
            ON CONFLICT (group_id) DO UPDATE SET 
                mode = EXCLUDED.mode,
                updated_at = NOW();
        """, (group_id, mode))
        
        conn.commit()
        
        mode_text = "心得打卡" if mode == 'CHECKIN' else "AI 閒聊"
        return f"⚙️ 群組模式已切換為：【{mode_text}】"
            
    except Exception as e:
        print(f"DB SET MODE ERROR: {e}", file=sys.stderr)
        return "⚠️ 抱歉，資料庫設定模式失敗了...請稍後再試。"
    finally:
        if conn: conn.close()

# --- 資料庫操作：取得群組模式 ---
def get_group_mode(group_id):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT mode FROM groups WHERE group_id = %s;", (group_id,))
        result = cur.fetchone()
        
        # 如果找到結果，返回模式字串，否則返回預設 'CHECKIN'
        return result[0] if result else 'CHECKIN'
    except Exception as e:
        print(f"DB GET MODE ERROR: {e}", file=sys.stderr)
        return 'CHECKIN' # 失敗時返回預設模式
    finally:
        if conn: conn.close()

# --- AI 閒聊功能 ---
def generate_ai_reply(prompt):
    if not ai_client:
        return "🤖 AI 服務未啟用 (缺少 GOOGLE_API_KEY)。"

    # 設置 AI 角色和行為
    system_instruction = ("你是一個專門用於 LINE 群組的有趣、幽默、且友好的聊天機器人。 "
                          "當使用者詢問你的工作時，你要解釋你的主要功能是協助群組記錄「心得打卡」，"
                          "並提供 VIP 名單和模式切換等指令，但你也可以進行輕鬆有趣的閒聊。 "
                          "你的回答應簡潔、使用繁體中文、並帶有表情符號。")

    try:
        # 使用 genai.Client().models.generate_content 
        # 確保使用正確的 GEMINI_MODEL
        response = ai_client.models.generate_content(
            model=GEMINI_MODEL, 
            contents=prompt,
            config=genai.types.GenerateContentConfig(
                system_instruction=system_instruction,
                # 限制長度，避免 LINE 訊息過長
                max_output_tokens=150 
            )
        )
        return response.text
    except Exception as e:
        # 捕獲 AI API 錯誤，包括模型未找到的 404 錯誤
        print(f"AI GENERATION ERROR with {GEMINI_MODEL}: {e}", file=sys.stderr)
        return "🤖 抱歉，AI 發生了一點小故障，我正在修理中...🛠️"

# --- LINE 訊息處理 ---
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    text = event.message.text.strip()
    group_id = None
    
    # 確保只處理群組/聊天室/私聊中的訊息
    if isinstance(event.source, (SourceGroup, SourceRoom, SourceUser)):
        group_id = event.source.group_id if isinstance(event.source, SourceGroup) else \
                   event.source.room_id if isinstance(event.source, SourceRoom) else \
                   event.source.user_id
    
    if not group_id or group_id in EXCLUDE_GROUP_IDS:
        return # 跳過不處理的群組

    reply_text = None
    
    # --- 1. 指令處理 ---
    if text.startswith('VIP名單設定：'):
        # 格式: VIP名單設定：姓名A, 姓名B, 姓名C
        vip_names_str = text[6:].strip()
        reply_text = set_vip_list(group_id, vip_names_str)
        
    elif text == '查看VIP名單':
        vip_list = get_vip_list(group_id)
        if not vip_list:
            reply_text = "🧐 目前 VIP 名單為空。請使用「VIP名單設定：姓名A, 姓名B」來設定。"
        else:
            list_of_names = "\n".join([f"- {name}" for name in vip_list])
            reply_text = f"📝 目前 VIP 名單 (共 {len(vip_list)} 人)：\n{list_of_names}"

    elif text.startswith('設定模式：'):
        # 格式: 設定模式：打卡 或 設定模式：AI
        mode = text[5:].strip()
        reply_text = set_group_mode(group_id, mode)

    elif text == '查看模式':
        current_mode = get_group_mode(group_id)
        mode_text = "心得打卡" if current_mode == 'CHECKIN' else "AI 閒聊"
        reply_text = f"⚙️ 目前群組模式是：【{mode_text}】"
        
    elif text == '幫助':
        reply_text = (
            "🤖 我的主要功能是提醒大家交心得並記錄打卡。\n\n"
            "【打卡】\n"
            "請發送：YYYY.MM.DD 姓名\n"
            "範例：2025.01.01 浣熊\n\n"
            "【指令】\n"
            "1. VIP名單設定：姓名A,姓名B (設定/更新 VIP 名單)\n"
            "2. 查看VIP名單 (查看目前 VIP 名單)\n"
            "3. 設定模式：打卡 (切換到心得打卡模式)\n"
            "4. 設定模式：AI (切換到 AI 閒聊模式)\n"
            "5. 查看模式 (查看目前模式)\n"
            "6. 幫助 (顯示此列表)"
        )
        
    # --- 2. 打卡報告處理 (僅在 'CHECKIN' 模式下) ---
    current_mode = get_group_mode(group_id)
    if not reply_text and current_mode == 'CHECKIN':
        # 格式檢查 (YYYY.MM.DD 姓名)
        # 正則表達式： (\d{4}[./]\d{2}[./]\d{2})\s+(.+)
        match_report = re.match(r"^(\d{4}[./]\d{2}[./]\d{2})\s+(.+)$", text)
        
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

    # --- 3. AI 閒聊 (僅在 'AI' 模式下，且沒有被指令或打卡處理掉) ---
    if not reply_text and current_mode == 'AI':
        # 呼叫 AI 生成回覆
        reply_text = generate_ai_reply(text)


    # 發送回覆訊息
    if reply_text:
        try:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=reply_text)
            )
        except LineBotApiError as e:
            # 如果 reply_message 失敗，嘗試 PUSH 訊息 (通常用於群組權限不足以 reply)
            # 在這裡我們只打印錯誤，因為 reply 失敗通常是 LINE 平台的問題
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
        print(f"LINE HANDLER ERROR: {e}", file=sys.stderr)
        abort(500)
    return 'OK'

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)