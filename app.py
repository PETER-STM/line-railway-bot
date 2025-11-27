import os
import sys
import re
import subprocess
from datetime import datetime, timedelta
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

# --- 🧠 AI 初始化 (自動偵測可用模型) ---
model = None
if GOOGLE_API_KEY:
    try:
        genai.configure(api_key=GOOGLE_API_KEY)
        # 1. 設定優先順序 (優先使用 2.0 Flash)
        priority_list = [
            'models/gemini-2.0-flash',       
            'models/gemini-2.0-flash-lite',  
            'models/gemini-2.5-pro-preview-03-25', 
            'models/gemini-1.5-flash',
            'models/gemini-pro'
        ]
        
        # 2. 取得可用模型並匹配
        available_models = []
        try:
            for m in genai.list_models():
                if 'generateContent' in m.supported_generation_methods:
                    available_models.append(m.name)
        except Exception:
            pass # 忽略連線錯誤，使用預設邏輯

        selected_model_name = None
        for p in priority_list:
            if p in available_models:
                selected_model_name = p
                break
        
        if not selected_model_name and available_models:
            selected_model_name = available_models[0]

        if selected_model_name:
            clean_name = selected_model_name.replace('models/', '')
            model = genai.GenerativeModel(clean_name)
            print(f"✅ Gemini AI initialized using: {clean_name}", file=sys.stderr)
        else:
            print("❌ FATAL: No text generation models found!", file=sys.stderr)

    except Exception as e:
        print(f"WARNING: Gemini AI init failed: {e}", file=sys.stderr)

app = Flask(__name__)
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# --- 工具函式 ---
def normalize_name(name):
    if not name: return ""
    return re.sub(r'^\s*[（(\[【][^()\[\]]{1,10}[)）\]】]\s*', '', name).strip()

def get_db_connection():
    try:
        return psycopg2.connect(DATABASE_URL, sslmode='require')
    except Exception as e:
        print(f"DB CONNECTION ERROR: {e}", file=sys.stderr)
        return None

# --- AI 與 資料檢索 (RAG) 核心 ---
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

def get_ai_context(group_id, user_text):
    """
    根據使用者的問題，去資料庫撈取相關的「心得回報」作為 AI 的參考資料。
    """
    conn = get_db_connection()
    if not conn: return ""
    
    context_data = ""
    try:
        with conn.cursor() as cur:
            # 策略 1: 檢查是否在問「大家」或「總結」
            keywords_all = ["大家", "所有", "針對目前", "總結", "分析", "整體"]
            if any(k in user_text for k in keywords_all):
                # 撈取今天(或最近24小時)的所有回報
                target_date = datetime.utcnow().date() # 簡單起見，抓 UTC 日期 (可能需要調整+8)
                # 或是抓最近 5 筆
                cur.execute("""
                    SELECT reporter_name, report_content, report_date 
                    FROM reports 
                    WHERE group_id = %s 
                    ORDER BY created_at DESC LIMIT 10
                """, (group_id,))
                rows = cur.fetchall()
                if rows:
                    context_data += "【參考資料：目前團隊成員的最新回報】\n"
                    for r in rows:
                        context_data += f"- {r[0]} ({r[2]}): {r[1][:200]}...\n" # 截斷過長內容
                else:
                    context_data += "【參考資料】目前還沒有人回報心得。\n"
            
            # 策略 2: 檢查是否在問「特定人」 (比對 VIP 名單)
            else:
                # 先把 VIP 名單撈出來比對
                cur.execute("SELECT vip_name, normalized_name FROM group_vips WHERE group_id = %s", (group_id,))
                vips = cur.fetchall()
                
                found_vip = None
                for v_name, v_norm in vips:
                    # 如果使用者輸入包含某個 VIP 的名字 (例如 "邦妮的心得")
                    if v_norm and v_norm in user_text:
                        found_vip = v_norm
                        break
                    elif v_name and v_name in user_text:
                        found_vip = v_norm # 用 normalized 去查 reports
                        break
                
                if found_vip:
                    # 撈取該員最新的一筆
                    cur.execute("""
                        SELECT reporter_name, report_content, report_date 
                        FROM reports 
                        WHERE group_id = %s AND normalized_name = %s
                        ORDER BY report_date DESC LIMIT 1
                    """, (group_id, found_vip))
                    row = cur.fetchone()
                    if row:
                        context_data += f"【參考資料：{row[0]} 的最新回報】\n內容：{row[1]}\n日期：{row[2]}\n"
                    else:
                        context_data += f"【參考資料】資料庫裡還沒有 {found_vip} 的回報紀錄。\n"

    except Exception as e:
        print(f"Context Error: {e}", file=sys.stderr)
    finally:
        conn.close()
    
    return context_data

def chat_with_ai(text, context=""):
    if not model: return "😵‍💫 AI 暫時無法使用。"
    try:
        # 建構強化的 Prompt
        system_prompt = "你是一個幽默、有點毒舌但很樂於助人的團隊助理 Bot。你的名字叫「摳你錢3000」。"
        
        user_prompt = ""
        if context:
            user_prompt += f"{context}\n\n(以上是真實的資料庫紀錄，請根據這些內容回答使用者的問題。如果資料裡顯示沒人回報，就如實吐槽。)\n\n"
        
        user_prompt += f"使用者問題：{text}\n請用繁體中文簡短回答："

        # 組合 (有些 SDK 支援 system_instruction，這裡用最通用的拼接方式)
        full_prompt = f"{system_prompt}\n\n{user_prompt}"
        
        response = model.generate_content(full_prompt)
        return response.text.strip()
    except Exception as e:
        print(f"AI ERROR: {e}", file=sys.stderr)
        return "😵‍💫 AI 發生錯誤 (請檢查 Log)。"

# --- 資料庫操作：名單管理 & 回報 ---
def manage_vip_list(group_id, vip_name, action):
    conn = get_db_connection()
    if not conn: return "💥 連線失敗。"
    if vip_name and (len(vip_name) < 1 or vip_name in ['(', '（']): return "❓ 請輸入有效的人名。"
    normalized = normalize_name(vip_name) if vip_name else None
    
    try:
        with conn.cursor() as cur:
            if action == 'ADD':
                cur.execute("INSERT INTO group_vips (group_id, vip_name, normalized_name) VALUES (%s, %s, %s) ON CONFLICT (group_id, normalized_name) DO NOTHING", (group_id, vip_name, normalized))
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
                    list_str = "\n".join([f"🔸 {name}" for name in sorted(list(set(valid_vips)))])
                    return f"📋 最新回報觀察名單：\n{list_str}\n\n（嗯，看起來大家都還活著。）"
                return "📭 名單空空如也～"
    finally:
        conn.close()

def log_report(group_id, date_str, reporter_name, content):
    conn = get_db_connection()
    if not conn: return "💥 連線失敗。"
    reporter_name = reporter_name.strip()
    if not reporter_name or reporter_name in ['（', '(']: return "⚠️ 名字解析失敗。"
    normalized = normalize_name(reporter_name)
    
    try:
        r_date = datetime.strptime(date_str, '%Y.%m.%d').date()
        with conn.cursor() as cur:
            cur.execute("INSERT INTO group_vips (group_id, vip_name, normalized_name) VALUES (%s, %s, %s) ON CONFLICT (group_id, normalized_name) DO NOTHING", (group_id, reporter_name, normalized))
            cur.execute("SELECT reporter_name FROM reports WHERE group_id = %s AND report_date = %s AND normalized_name = %s", (group_id, r_date, normalized))
            if cur.fetchone(): return f"⚠️ {reporter_name} 今天已經回報過了！"
            cur.execute("INSERT INTO reports (group_id, reporter_name, normalized_name, report_date, report_content) VALUES (%s, %s, %s, %s, %s)", (group_id, reporter_name, normalized, r_date, content))
            conn.commit()
            return f"👌 收到！{reporter_name} ({date_str}) 的心得已登入。\n（給你的乖寶寶貼紙 ⭐）"
    except ValueError: return "❌ 日期格式錯誤 (YYYY.MM.DD)。"
    except Exception as e:
        print(f"LOG ERROR: {e}", file=sys.stderr)
        return "💥 記錄失敗。"
    finally:
        conn.close()

# --- Webhook ---
@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except (InvalidSignatureError, LineBotApiError): abort(400)
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

    if first_line.lower() in ["指令", "幫助", "help"]:
        reply = "🤖 **功能選單**\n📝 回報: `YYYY.MM.DD [姓名]`\n👥 管理: `新增人名`, `刪除人名`, `名單`\n⚙️ AI: `開啟智能模式`, `關閉智能模式`"
    elif first_line == "開啟智能模式": reply = set_group_mode(group_id, True)
    elif first_line == "關閉智能模式": reply = set_group_mode(group_id, False)

    if not reply:
        if first_line.startswith("新增人名"): 
            name = first_line.replace("新增人名", "").strip()
            if name: reply = manage_vip_list(group_id, name, 'ADD')
        elif first_line.startswith("刪除人名"):
            name = first_line.replace("刪除人名", "").strip()
            if name: reply = manage_vip_list(group_id, name, 'DEL')
        elif first_line in ["查詢名單", "名單", "list"]:
            reply = manage_vip_list(group_id, None, 'LIST')
        
        match_report = re.match(r"^(\d{4}\.\d{2}\.\d{2})\s*(?:[（(].*?[)）])?\s*([^\n]+)([\s\S]*)", text, re.DOTALL)
        if match_report:
            d_str = match_report.group(1)
            name = match_report.group(2).strip()
            content = text
            if name: reply = log_report(group_id, d_str, name, content)

    # --- AI 處理 (含資料庫檢索) ---
    if not reply and get_group_mode(group_id):
        # 1. 先嘗試撈取相關資料 (RAG)
        context_info = get_ai_context(group_id, text)
        # 2. 將資料與問題一起丟給 AI
        reply = chat_with_ai(text, context_info)

    if reply:
        try:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        except Exception as e: print(f"REPLY ERROR: {e}", file=sys.stderr)

# --- 定時排程 ---
def run_daily_check():
    print("⏰ Daily check...", file=sys.stderr)
    subprocess.run(["python", "scheduler.py", "--days-ago", "0"])

def run_makeup_check():
    print("⏰ Makeup check...", file=sys.stderr)
    subprocess.run(["python", "scheduler.py", "--days-ago", "1"])

scheduler = BackgroundScheduler()
scheduler.add_job(run_daily_check, 'cron', hour=14, minute=0) # 台灣 22:00
scheduler.add_job(run_makeup_check, 'cron', hour=5, minute=0) # 台灣 13:00
scheduler.start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)