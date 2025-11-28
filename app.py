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

# --- 🧠 AI 初始化 ---
model = None
if GOOGLE_API_KEY:
    try:
        genai.configure(api_key=GOOGLE_API_KEY)
        # 優先使用 2.0 Flash
        priority_list = [
            'models/gemini-2.0-flash',       
            'models/gemini-2.0-flash-lite',  
            'models/gemini-2.5-pro-preview-03-25', 
            'models/gemini-1.5-flash',
            'models/gemini-pro'
        ]
        
        available_models = []
        try:
            for m in genai.list_models():
                if 'generateContent' in m.supported_generation_methods:
                    available_models.append(m.name)
        except Exception:
            pass 

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
    """RAG: 根據問題撈取資料庫心得"""
    conn = get_db_connection()
    if not conn: return ""
    
    context_data = ""
    try:
        with conn.cursor() as cur:
            target_date = None
            current_time = datetime.utcnow() + timedelta(hours=8)
            
            if "昨天" in user_text:
                target_date = (current_time - timedelta(days=1)).date()
            elif "今天" in user_text:
                target_date = current_time.date()
            elif "前天" in user_text:
                target_date = (current_time - timedelta(days=2)).date()
            else:
                match_full = re.search(r'(\d{4})[./-](\d{1,2})[./-](\d{1,2})', user_text)
                if match_full:
                    target_date = f"{match_full.group(1)}-{match_full.group(2)}-{match_full.group(3)}"
                else:
                    match_short = re.search(r'(\d{1,2})[./月-](\d{1,2})', user_text)
                    if match_short:
                        target_date = f"{current_time.year}-{match_short.group(1)}-{match_short.group(2)}"
                    else:
                        match_day = re.search(r'(\d{1,2})號', user_text)
                        if match_day:
                            day = int(match_day.group(1))
                            target_date = f"{current_time.year}-{current_time.month}-{day}"

            keywords_all = ["大家", "所有", "針對目前", "總結", "分析", "整體", "整理", "彙整", "狀況", "狀態"]
            
            if any(k in user_text for k in keywords_all) or target_date:
                sql = "SELECT reporter_name, report_content, report_date FROM reports WHERE group_id = %s"
                params = [group_id]
                
                if target_date:
                    sql += " AND report_date = %s"
                    params.append(target_date)
                    period_desc = str(target_date)
                else:
                    sql += " ORDER BY created_at DESC LIMIT 10" 
                    period_desc = "最近"

                cur.execute(sql, tuple(params))
                rows = cur.fetchall()
                
                if rows:
                    context_data += f"【參考資料：{period_desc} 的團隊回報紀錄】\n"
                    for r in rows:
                        d_str = r[2].strftime('%Y-%m-%d') if r[2] else "未知日期"
                        context_data += f"- {r[0]} ({d_str}): {r[1][:500]}\n"
                else:
                    context_data += f"【參考資料】{period_desc} 沒有找到任何回報紀錄。\n"
            
            elif not target_date:
                cur.execute("SELECT vip_name, normalized_name FROM group_vips WHERE group_id = %s", (group_id,))
                vips = cur.fetchall()
                
                found_vip = None
                for v_name, v_norm in vips:
                    if v_norm and v_norm in user_text:
                        found_vip = v_norm
                        break
                    elif v_name and v_name in user_text:
                        found_vip = v_norm
                        break
                
                if found_vip:
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
        system_prompt = "你是一個幽默、有點毒舌但很樂於助人的團隊助理 Bot。你的名字叫「摳你錢3000」。"
        user_prompt = ""
        if context:
            user_prompt += f"{context}\n\n(以上是真實的資料庫紀錄，請根據這些內容回答使用者的問題。)\n\n"
        
        user_prompt += f"使用者問題：{text}\n請用繁體中文簡短回答(若是在做總結，請條列式呈現)："
        full_prompt = f"{system_prompt}\n\n{user_prompt}"
        response = model.generate_content(full_prompt)
        return response.text.strip()
    except Exception as e:
        print(f"AI ERROR: {e}", file=sys.stderr)
        return "😵‍💫 AI 發生錯誤 (請檢查 Log)。"

# --- 每日總結 (AI Summary) 核心邏輯 ---
def generate_daily_summary(group_id, date_str, target_name=None):
    """
    產生指定日期、指定群組的總結報告。
    支援指定人名過濾。
    """
    conn = get_db_connection()
    if not conn: return "💥 資料庫連線失敗。"
    
    report_text = ""
    try:
        with conn.cursor() as cur:
            sql = "SELECT reporter_name, report_content FROM reports WHERE group_id = %s AND report_date = %s"
            params = [group_id, date_str]
            
            # 如果有指定人名，加入過濾條件
            if target_name:
                sql += " AND (reporter_name ILIKE %s OR normalized_name ILIKE %s)"
                params.extend([f"%{target_name}%", f"%{target_name}%"])
            
            sql += " ORDER BY created_at ASC"
            
            cur.execute(sql, tuple(params))
            rows = cur.fetchall()
            
            if not rows:
                if target_name:
                    return f"📭 {date_str} 找不到「{target_name}」的回報紀錄。"
                return f"📭 {date_str} 找不到任何回報紀錄。"

            # 構建報告
            title = f"📊 【{date_str}】"
            title += f"{target_name} 的回報總結" if target_name else "團隊回報總結"
            
            lines = [title, "---------------------------"]
            
            # 使用 AI 進行單篇摘要
            for name, content in rows:
                try:
                    # 簡單摘要 Prompt
                    p = f"請將以下這份工作日報/心得，總結為一句話(包含重點進度與情緒狀態)，語氣請保持專業客觀，不要使用第一人稱，不要超過50個字：\n\n{content}"
                    res = model.generate_content(p)
                    summary = res.text.strip()
                except:
                    summary = "(AI摘要失敗)"
                
                lines.append(f"👤 **{name}**：\n{summary}")
            
            lines.append("---------------------------")
            lines.append(f"(共 {len(rows)} 筆紀錄)")
            report_text = "\n".join(lines)

    except Exception as e:
        print(f"Summary Error: {e}", file=sys.stderr)
        return "💥 產生總結報告時發生錯誤。"
    finally:
        conn.close()
        
    return report_text

# --- 資料庫操作：名單管理 & 回報 ---
def manage_vip_list(group_id, vip_name, action):
    conn = get_db_connection()
    if not conn: return "💥 連線失敗。"
    
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
        reply = "🤖 **功能選單**\n📝 回報: `YYYY.MM.DD [姓名]`\n👥 管理: `新增人名`, `刪除人名`, `名單`\n📊 總結: `總結回報 [日期] [姓名(選)]`\n⚙️ AI: `開啟智能模式`, `關閉智能模式`"
    
    elif first_line == "查詢群組ID":
        reply = f"🆔 本群組 ID 為：\n`{group_id}`\n(請複製起來用於測試指令)"

    elif first_line == "開啟智能模式": reply = set_group_mode(group_id, True)
    elif first_line == "關閉智能模式": reply = set_group_mode(group_id, False)

    # 2. 總結回報指令 (整合功能)
    elif first_line.startswith("總結回報"):
        # 解析指令: "總結回報 昨天", "總結回報 2025-11-27", "總結回報 27號"
        cmd_parts = first_line.split()
        target_str = cmd_parts[1] if len(cmd_parts) > 1 else "昨天"
        target_name = cmd_parts[2] if len(cmd_parts) > 2 else None # 支援 "總結回報 昨天 彼得"

        # 日期解析
        date_obj = None
        current_time = datetime.utcnow() + timedelta(hours=8)
        
        if "昨天" in target_str:
            date_obj = (current_time - timedelta(days=1)).date()
        elif "今天" in target_str:
            date_obj = current_time.date()
        elif "前天" in target_str:
            date_obj = (current_time - timedelta(days=2)).date()
        else:
            # 嘗試解析 YYYY.MM.DD 或 MM/DD
            try:
                # 簡單正規化
                t = target_str.replace('/', '-').replace('.', '-')
                if len(t.split('-')) == 2: # MM-DD
                    t = f"{current_time.year}-{t}"
                elif "號" in t: # 27號
                    d = re.search(r'(\d+)', t).group(1)
                    t = f"{current_time.year}-{current_time.month}-{d}"
                
                date_obj = datetime.strptime(t, '%Y-%m-%d').date()
            except:
                reply = "❌ 日期格式錯誤，請使用：總結回報 昨天 / 總結回報 2025-11-27"

        if date_obj:
            d_str = date_obj.strftime('%Y-%m-%d')
            # 呼叫總結函式 (傳入群組ID以確保隔離)
            reply = generate_daily_summary(group_id, d_str, target_name)

    # 3. 回報與管理
    if not reply:
        if first_line.startswith("新增人名"): 
            name = first_line.replace("新增人名", "").strip()
            if name: reply = manage_vip_list(group_id, name, 'ADD')
        
        elif first_line.startswith("刪除人名"):
            name = first_line.replace("刪除人名", "").strip()
            if name: reply = manage_vip_list(group_id, name, 'DEL')

        elif first_line in ["查詢名單", "名單", "list"]:
            reply = manage_vip_list(group_id, None, 'LIST')

        # 4. 回報匹配 (日期 + 姓名 + 任意內容)
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
        except Exception as e:
            print(f"REPLY ERROR: {e}", file=sys.stderr)

# --- 定時排程 ---
def run_daily_check():
    # 任務 1: 每天晚上 10 點檢查「今天」的進度 (溫柔提醒)
    print("⏰ Daily check...", file=sys.stderr)
    subprocess.run(["python", "scheduler.py", "--days-ago", "0"])

def run_makeup_check():
    # 任務 2: 每天下午 1 點檢查「昨天」的缺交 (奧客模式)
    print("⏰ Makeup check...", file=sys.stderr)
    subprocess.run(["python", "scheduler.py", "--days-ago", "1"])

scheduler = BackgroundScheduler()
# 設定 1: 台灣時間 22:00 (UTC 14:00) -> 檢查當日
scheduler.add_job(run_daily_check, 'cron', hour=14, minute=0)
# 設定 2: 台灣時間 13:00 (UTC 05:00) -> 補繳昨天的
scheduler.add_job(run_makeup_check, 'cron', hour=5, minute=0)
scheduler.start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)



