import os
import sys
import re
import subprocess
from datetime import datetime
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError, LineBotApiError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, SourceGroup, SourceRoom, SourceUser
import psycopg2
import google.generativeai as genai
from apscheduler.schedulers.background import BackgroundScheduler

# --- 環境變數設定 ---
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
DATABASE_URL = os.environ.get('DATABASE_URL')
GOOGLE_API_KEY = os.environ.get('GOOGLE_API_KEY')
EXCLUDE_GROUP_IDS_STR = os.environ.get('EXCLUDE_GROUP_IDS', '')
EXCLUDE_GROUP_IDS = set(EXCLUDE_GROUP_IDS_STR.split(',')) if EXCLUDE_GROUP_IDS_STR else set()

# --- 診斷與初始化 ---
if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_CHANNEL_SECRET:
    sys.exit("Error: LINE Channel Token/Secret is missing!")

# 初始化 AI (退回 gemini-pro 以確保最高相容性)
model = None
if GOOGLE_API_KEY:
    try:
        genai.configure(api_key=GOOGLE_API_KEY)
        model = genai.GenerativeModel('gemini-pro')
        print("INFO: Gemini AI (gemini-pro) initialized.", file=sys.stderr)
    except Exception as e:
        print(f"WARNING: Gemini AI init failed: {e}", file=sys.stderr)

app = Flask(__name__)
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# --- 工具函式：姓名正規化 ---
def normalize_name(name):
    if not name: return ""
    # 移除各種括號與內容，只留名字
    return re.sub(r'^\s*[（(\[【][^()\[\]]{1,10}[)）\]】]\s*', '', name).strip()

# --- 資料庫連線 ---
def get_db_connection():
    try:
        return psycopg2.connect(DATABASE_URL, sslmode='require')
    except Exception as e:
        print(f"DB CONNECTION ERROR: {e}", file=sys.stderr)
        return None

# --- AI 相關函式 ---
def get_group_mode(group_id):
    conn = get_db_connection()
    if not conn: return False
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT ai_mode FROM group_configs WHERE group_id = %s", (group_id,))
            res = cur.fetchone()
            return res[0] if res else False
    finally:
        conn.close()

def set_group_mode(group_id, mode):
    conn = get_db_connection()
    if not conn: return "💥 資料庫連線失敗。"
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO group_configs (group_id, ai_mode) VALUES (%s, %s)
                ON CONFLICT (group_id) DO UPDATE SET ai_mode = EXCLUDED.ai_mode
            """, (group_id, mode))
            conn.commit()
        status = "🤖 智能對話 (AI)" if mode else "🔇 一般安靜 (NORMAL)"
        return f"🔄 模式已切換為：**{status}**"
    except Exception as e:
        return f"💥 設定失敗：{e}"
    finally:
        conn.close()

def chat_with_ai(text):
    if not model: return None
    try:
        prompt = f"你是一個幽默、有點毒舌但很樂於助人的團隊助理 Bot。請用繁體中文簡短回答：{text}"
        response = model.generate_content(prompt)
        return response.text.strip()
    except Exception as e:
        print(f"AI ERROR: {e}", file=sys.stderr)
        return "😵‍💫 AI 暫時無法回應 (請檢查 API Key 或配額)。"

# --- 資料庫操作：名單管理 ---
def manage_vip_list(group_id, vip_name, action):
    conn = get_db_connection()
    if not conn: return "💥 連線失敗。"
    
    # 簡單防呆
    if vip_name and (len(vip_name) < 1 or vip_name in ['(', '（']):
        return "❓ 請輸入有效的人名。"

    normalized = normalize_name(vip_name) if vip_name else None
    
    try:
        with conn.cursor() as cur:
            if action == 'ADD':
                cur.execute("""
                    INSERT INTO group_vips (group_id, vip_name, normalized_name) 
                    VALUES (%s, %s, %s) 
                    ON CONFLICT (group_id, normalized_name) DO NOTHING
                """, (group_id, vip_name, normalized))
                conn.commit()
                return f"🎉 {vip_name} 已加入名單！"
            
            elif action == 'DEL':
                cur.execute("DELETE FROM group_vips WHERE group_id = %s AND normalized_name = %s", (group_id, normalized))
                conn.commit()
                return f"🗑️ {vip_name} 已移除。"

            elif action == 'LIST':
                cur.execute("SELECT vip_name FROM group_vips WHERE group_id = %s ORDER BY vip_name", (group_id,))
                vips = [row[0] for row in cur.fetchall()]
                valid_vips = [v for v in vips if v and v not in ['（', '(', ' ']]
                
                if valid_vips:
                    display_list = sorted(list(set(valid_vips)))
                    list_str = "\n".join([f"🔸 {name}" for name in display_list])
                    return f"📋 最新回報觀察名單：\n{list_str}\n\n（嗯，看起來大家都還活著。）"
                return "📭 名單空空如也～"
    finally:
        conn.close()

# --- 資料庫操作：紀錄心得 ---
def log_report(group_id, date_str, reporter_name, content):
    conn = get_db_connection()
    if not conn: return "💥 連線失敗。"
    
    reporter_name = reporter_name.strip()
    if not reporter_name or reporter_name in ['（', '(']:
         return "⚠️ 名字解析失敗，請確認格式：YYYY.MM.DD (週X) 姓名"

    normalized = normalize_name(reporter_name)
    
    try:
        r_date = datetime.strptime(date_str, '%Y.%m.%d').date()
        with conn.cursor() as cur:
            # 1. 自動補名單
            cur.execute("""
                INSERT INTO group_vips (group_id, vip_name, normalized_name) 
                VALUES (%s, %s, %s) 
                ON CONFLICT (group_id, normalized_name) DO NOTHING
            """, (group_id, reporter_name, normalized))
            
            # 2. 檢查重複
            cur.execute("""
                SELECT reporter_name FROM reports 
                WHERE group_id = %s AND report_date = %s AND normalized_name = %s
            """, (group_id, r_date, normalized))
            
            if cur.fetchone():
                 return f"⚠️ {reporter_name} 今天已經回報過了！"

            # 3. 寫入紀錄
            cur.execute("""
                INSERT INTO reports (group_id, reporter_name, normalized_name, report_date, report_content) 
                VALUES (%s, %s, %s, %s, %s)
            """, (group_id, reporter_name, normalized, r_date, content))
            
            conn.commit()
            return f"👌 收到！{reporter_name} ({date_str}) 的心得已登入。\n（給你的乖寶寶貼紙 ⭐）"
            
    except ValueError:
        return "❌ 日期格式錯誤 (YYYY.MM.DD)。"
    except Exception as e:
        print(f"LOG ERROR: {e}", file=sys.stderr)
        return "💥 記錄失敗，請稍後再試。"
    finally:
        conn.close()

# --- Webhook ---
@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except (InvalidSignatureError, LineBotApiError):
        abort(400)
    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    text = event.message.text
    group_id = None
    if isinstance(event.source, SourceGroup): group_id = event.source.group_id
    elif isinstance(event.source, SourceRoom): group_id = event.source.room_id
    elif isinstance(event.source, SourceUser): group_id = event.source.user_id
    
    if not group_id or group_id in EXCLUDE_GROUP_IDS: return

    processed_text = text.strip().replace('（', '(').replace('）', ')')
    first_line = processed_text.split('\n')[0].strip()
    reply = None

    # 1. 指令
    if first_line.lower() in ["指令", "幫助", "help"]:
        reply = "🤖 **功能選單**\n📝 回報: `YYYY.MM.DD [姓名]`\n👥 管理: `新增人名 [名]`, `刪除人名 [名]`, `查詢名單`\n⚙️ AI: `開啟智能模式`, `關閉智能模式`"
    elif first_line == "開啟智能模式": reply = set_group_mode(group_id, True)
    elif first_line == "關閉智能模式": reply = set_group_mode(group_id, False)

    # 2. 回報與管理
    if not reply:
        if first_line.startswith("新增人名"): 
            name = first_line.replace("新增人名", "").strip()
            if name: reply = manage_vip_list(group_id, name, 'ADD')
        
        elif first_line.startswith("刪除人名"):
            name = first_line.replace("刪除人名", "").strip()
            if name: reply = manage_vip_list(group_id, name, 'DEL')

        elif first_line in ["查詢名單", "名單", "list"]:
            reply = manage_vip_list(group_id, None, 'LIST')

        # 3. 回報匹配 (regex 修正)
        match_report = re.match(r"^(\d{4}\.\d{2}\.\d{2})\s*(?:[（(].*?[)）])?\s*([^\n]+)([\s\S]*)", text, re.DOTALL)
        if match_report:
            d_str = match_report.group(1)
            name = match_report.group(2).strip()
            content = text
            if name: reply = log_report(group_id, d_str, name, content)

    # 4. AI
    if not reply and get_group_mode(group_id):
        reply = chat_with_ai(text)

    if reply:
        try:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        except Exception as e:
            print(f"REPLY ERROR: {e}", file=sys.stderr)

# --- 定時排程 ---
def run_daily_check():
    # 任務 1: 每天晚上 10 點檢查「今天」的進度 (溫柔提醒)
    print("⏰ Running daily check (Today)...", file=sys.stderr)
    subprocess.run(["python", "scheduler.py", "--days-ago", "0"])

def run_makeup_check():
    # 任務 2: 每天下午 1 點檢查「昨天」的缺交 (奧客模式)
    print("⏰ Running makeup check (Yesterday)...", file=sys.stderr)
    subprocess.run(["python", "scheduler.py", "--days-ago", "1"])

# 初始化排程器
scheduler = BackgroundScheduler()

# 設定 1: 台灣時間 22:00 (UTC 14:00) -> 檢查當日
scheduler.add_job(run_daily_check, 'cron', hour=14, minute=0)

# 設定 2: 台灣時間 13:00 (UTC 05:00) -> 補繳昨天的
scheduler.add_job(run_makeup_check, 'cron', hour=5, minute=0)

scheduler.start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)