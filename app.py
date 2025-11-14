# app.py

import os
import re
from datetime import datetime
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import psycopg2

# -----------------
# 1. 初始化設定與環境變數讀取
# -----------------

app = Flask(__name__)

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET")

if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_CHANNEL_SECRET:
    raise ValueError("Line API 相關環境變數 (ACCESS_TOKEN / SECRET) 未設置！")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# -----------------
# 2. 資料庫連線函式
# -----------------

def get_db_connection():
    """使用環境變數連線到 PostgreSQL"""
    conn_url = os.environ.get("DATABASE_URL")
    
    if not conn_url:
        try:
            conn_url = (
                f"postgresql://{os.environ.get('PGUSER')}:"
                f"{os.environ.get('PGPASSWORD')}@"
                f"{os.environ.get('PGHOST')}:"
                f"{os.environ.get('PGPORT')}/"
                f"{os.environ.get('PGDATABASE')}"
            )
        except Exception:
            raise ValueError("資料庫連線環境變數未設置！")
        
    conn = psycopg2.connect(conn_url)
    return conn

# -----------------
# 3. 資料庫操作：儲存回報紀錄 (包含 source_id)
# -----------------

def save_report(report_date, name, user_id, source_id):
    """將回報紀錄存入 PostgreSQL 資料庫"""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # 插入資料，新增 source_id 欄位
        sql = """
        INSERT INTO reports (report_date, name, line_user_id, source_id)
        VALUES (%s, %s, %s, %s)
        """
        cur.execute(sql, (report_date, name, user_id, source_id))
        
        conn.commit()
        cur.close()
        return True
    except Exception as e:
        app.logger.error(f"資料庫儲存錯誤: {e}")
        return False
    finally:
        if conn:
            conn.close()

# -----------------
# 4. Webhook 接收與處理
# -----------------

@app.route("/callback", methods=['POST'])
def callback():
    """接收 Line Webhook 請求"""
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    app.logger.info("Request body: " + body)
    
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        app.logger.error("Invalid signature. 請檢查您的 Channel Access Token/Secret.")
        abort(400)
    
    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    """處理 Line 傳來的文字訊息"""
    text = event.message.text.strip()
    user_id = event.source.user_id 
    
    # 🌟 獲取 source_id：判斷訊息來源是 Group, Room 還是 User
    source_id = None
    if event.source.type == 'group':
        source_id = event.source.group_id
    elif event.source.type == 'room':
        source_id = event.source.room_id
    else:
        source_id = user_id
    
    # 判斷是否為回報指令: "railway YYYY.MM.DD 人名"
    if text.lower().startswith('railway'):
        
        # 使用正規表達式匹配日期和人名 (支持 YYYY.MM.DD 或 YYYY/MM/DD)
        match = re.search(r'railway\s+(\d{4}[./]\d{1,2}[./]\d{1,2})\s+(.+)', text, re.IGNORECASE)
        
        if match:
            date_str = match.group(1).replace('/', '.')
            name = match.group(2).strip()
            
            try:
                report_date = datetime.strptime(date_str, '%Y.%m.%d').date()
                
                # 執行儲存，傳入 source_id
                success = save_report(report_date, name, user_id, source_id)
                
                if success:
                    reply_text = f"✅ 紀錄成功！\n回報者: **{name}**\n日期: **{report_date.strftime('%Y/%m/%d')}**\n\n感謝您的回報！"
                else:
                    reply_text = "❌ 資料庫儲存失敗，請聯繫管理員檢查 DB 連線。"
                
            except ValueError:
                reply_text = "❌ 日期格式錯誤！請使用 YYYY.MM.DD 或 YYYY/MM/DD 格式。\n\n範例：`railway 2025.11.09 伊森`"
            
        else:
            reply_text = "⚠️ 回報格式不正確。\n\n正確格式：`railway YYYY.MM.DD 人名`\n範例：`railway 2025.11.09 伊森`"
    
    else:
        # 非回報指令
        reply_text = "我是鐵路回報紀錄 Bot。\n\n請使用以下格式紀錄：\n`railway YYYY.MM.DD 人名`\n\n範例：`railway 2025.11.09 伊森`"

    # 回覆訊息給使用者
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=reply_text)
    )

# -----------------
# 5. 應用程式啟動
# -----------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)