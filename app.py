import os
import sys
import re
from datetime import datetime, timedelta
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError, LineBotApiError, LineBotApiError
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

# 初始化 AI (使用 gemini-1.5-flash)
model = None
if GOOGLE_API_KEY:
    try:
        genai.configure(api_key=GOOGLE_API_KEY)
        model = genai.GenerativeModel('gemini-1.5-flash')
        print("INFO: Gemini AI initialized.", file=sys.stderr)
    except Exception as e:
        print(f"WARNING: Gemini AI init failed: {e}", file=sys.stderr)
else:
    print("WARNING: GOOGLE_API_KEY not found. AI features disabled.", file=sys.stderr)

app = Flask(__name__)
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# --- 工具函式：姓名正規化 ---
def normalize_name(name):
    """
    移除姓名中的前綴括號，例如 '(三) 浣熊' -> '浣熊'
    """
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
    if not conn: return False # 預設關閉 AI
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
        return "😵‍💫 AI 腦袋打結了，請稍後再試。"

# --- 資料庫操作：名單管理 ---
def manage_vip_list(group_id, vip_name, action):
    conn = get_db_connection()
    if not conn: return "💥 連線失敗。"
    
    normalized = normalize_name(vip_name) if vip_name else None
    
    try:
        with conn.cursor() as cur:
            if action == 'ADD':
                # 新增 VIP (儲存原始名與正規化名)
                cur.execute("""
                    INSERT INTO group_vips (group_id, vip_name, normalized_name) 
                    VALUES (%s, %s, %s) 
                    ON CONFLICT (group_id, normalized_name) DO NOTHING
                """, (group_id, vip_name, normalized))
                if cur.rowcount > 0:
                    conn.commit()
                    return f"🎉 好嘞～ {vip_name} 已成功加入名單！"
                return f"🤨 {vip_name} 早就在名單裡面了。"
            
            elif action == 'DEL':
                # 刪除 VIP (依據正規化名稱)
                cur.execute("DELETE FROM group_vips WHERE group_id = %s AND normalized_name = %s", (group_id, normalized))
                if cur.rowcount > 0:
                    # 同步刪除歷史紀錄 (可選)
                    # cur.execute("DELETE FROM reports WHERE group_id = %s AND normalized_name = %s", (group_id, normalized))
                    conn.commit()
                    return f"🗑️ {vip_name} 已從名單中移除。"
                return f"❓ 名單裡根本沒有 {vip_name} 啊！"

            elif action == 'LIST':
                # 列出名單
                cur.execute("SELECT vip_name FROM group_vips WHERE group_id = %s ORDER BY vip_name", (group_id,))
                vips = [row[0] for row in cur.fetchall()]
                if vips:
                    # 為了美觀，可以在這裡做去重顯示
                    display_list = sorted(list(set(vips)))
                    list_str = "\n".join([f"🔸 {name}" for name in display_list])
                    return f"📋 最新回報觀察名單如下：\n{list_str}\n\n（嗯，看起來大家都還活著。）"
                return "📭 名單空空如也～"
    finally:
        conn.close()

# --- 資料庫操作：紀錄心得 ---
def log_report(group_id, date_str, reporter_name, content):
    conn = get_db_connection()
    if not conn: return "💥 連線失敗。"
    
    normalized = normalize_name(reporter_name)
    
    try:
        r_date = datetime.strptime(date_str, '%Y.%m.%d').date()
        with conn.cursor() as cur:
            # 1. 自動補名單 (如果不在 VIP 名單中，自動加入)
            cur.execute("""
                INSERT INTO group_vips (group_id, vip_name, normalized_name) 
                VALUES (%s, %s, %s) 
                ON CONFLICT (group_id, normalized_name) DO NOTHING
            """, (group_id, reporter_name, normalized))
            
            # 2. 檢查是否重複 (使用正規化名稱比對當天紀錄)
            cur.execute("""
                SELECT reporter_name FROM reports 
                WHERE group_id = %s AND report_date = %s AND normalized_name = %s
            """, (group_id, r_date, normalized))
            
            if cur.fetchone():
                 return f"⚠️ {reporter_name} ({date_str}) 今天已經回報過了！\n\n別想靠重複交作業刷存在感，我看的很清楚 👀"

            # 3. 寫入紀錄 (包含完整心得內容)
            cur.execute("""
                INSERT INTO reports (group_id, reporter_name, normalized_name, report_date, report_content) 
                VALUES (%s, %s, %s, %s, %s)
            """, (group_id, reporter_name, normalized, r_date, content))
            
            conn.commit()
            return f"👌 收到！{reporter_name} ({date_str}) 的心得已成功登入檔案。\n\n（今天有乖，給你一個隱形貼紙 ⭐）"
            
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
    
    if not group_id or group_id in EXCLUDE_GROUP_IDS: return

    # 預處理：全形轉半形
    processed_text = text.strip().replace('（', '(').replace('）', ')')
    first_line = processed_text.split('\n')[0].strip()
    reply = None

    # 1. 系統指令
    if first_line in ["指令", "幫助", "help"]:
        reply = "🤖 **功能選單**\n\n📝 回報: `YYYY.MM.DD [姓名]`\n👥 管理: `新增人名`, `刪除人名`, `查詢名單`\n⚙️ AI: `開啟智能模式`, `關閉智能模式`"
    elif first_line == "開啟智能模式": reply = set_group_mode(group_id, True)
    elif first_line == "關閉智能模式": reply = set_group_mode(group_id, False)

    # 2. 回報與管理 (優先處理)
    if not reply:
        match_add = re.match(r"^新增人名[\s　]+(.+)$", first_line)
        if match_add: reply = manage_vip_list(group_id, match_add.group(1).strip(), 'ADD')

        match_del = re.match(r"^刪除人名[\s　]+(.+)$", first_line)
        if match_del: reply = manage_vip_list(group_id, match_del.group(1).strip(), 'DEL')

        if first_line in ["查詢名單", "查看人員", "名單", "list"]:
            reply = manage_vip_list(group_id, None, 'LIST')

        # 回報匹配 (日期 + 姓名 + 任意內容)
        match_report = re.match(r"^(\d{4}\.\d{2}\.\d{2})\s*(?:\(.*\))?\s*(.+?)\s*([\s\S]*)", text, re.DOTALL)
        if match_report:
            d_str, name = match_report.group(1), match_report.group(2).strip()
            content = text # 使用完整訊息作為心得內容
            if name: reply = log_report(group_id, d_str, name, content)

    # 3. AI 閒聊 (最後)
    if not reply and get_group_mode(group_id):
        reply = chat_with_ai(text)

    if reply:
        try:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        except Exception as e:
            print(f"REPLY ERROR: {e}", file=sys.stderr)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)


