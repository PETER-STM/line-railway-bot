# app.py - 最終完整修正版 (加入忽略短訊息邏輯)
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

# 讓程式碼可以接受 'ACCESS_TOKEN' 或 'LINE_CHANNEL_ACCESS_TOKEN'
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN") or os.environ.get("ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET") or os.environ.get("SECRET")

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
    
    # 使用 PG* 變數構建連線
    if not conn_url:
        try:
            conn = psycopg2.connect(
                host=os.environ.get('PGHOST'),
                database=os.environ.get('PGDATABASE'),
                user=os.environ.get('PGUSER'),
                password=os.environ.get('PGPASSWORD'),
                port=os.environ.get('PGPORT')
            )
            return conn
        except Exception:
            raise ValueError("資料庫連線環境變數未設置或連線失敗！")
            
    # 如果 DATABASE_URL 存在
    conn = psycopg2.connect(conn_url)
    return conn

# -----------------
# 3. 資料庫操作函式
# -----------------

def save_report(report_date, name, user_id, source_id):
    """將回報紀錄存入 PostgreSQL 資料庫"""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
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

def add_reporter(group_id, reporter_name):
    """將人名新增到 group_reporters 表，並檢查是否已存在"""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        sql = """
            INSERT INTO group_reporters (group_id, reporter_name)
            VALUES (%s, %s)
            ON CONFLICT (group_id, reporter_name) DO NOTHING
        """
        cur.execute(sql, (group_id, reporter_name))
        
        inserted_rows = cur.rowcount
        conn.commit()
        cur.close()
        
        return inserted_rows > 0
    except Exception as e:
        app.logger.error(f"新增回報人錯誤: {e}")
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
    
    # 🌟 修正點：忽略長度過短或空字串的訊息 (例如單純的數字或表情符號，避免預設回覆)
    if not text or len(text) < 5:
        return # 直接結束函式，不回覆任何訊息

    user_id = event.source.user_id 
    
    # 獲取 source_id：判斷訊息來源是 Group, Room 還是 User
    source_id = None
    if event.source.type == 'group':
        source_id = event.source.group_id
    elif event.source.type == 'room':
        source_id = event.source.room_id
    else:
        source_id = user_id
    
    # --- 指令判斷區塊 ---

    # 1. 處理 "新增人名" 指令 (優先判斷)
    if text.startswith('新增人名'):
        reporter_name = text[len('新增人名'):].strip() 
        
        if not reporter_name:
            reply_text = "⚠️ 請提供人名，例如：新增人名 陳經理"
        else:
            if event.source.type not in ['group', 'room']:
                reply_text = "❌ 只能在群組或聊天室中新增回報人。"
            else:
                success = add_reporter(source_id, reporter_name)
                
                if success:
                    reply_text = f"✅ 已成功新增：**{reporter_name}** 為回報人！"
                elif success is False:
                    reply_text = "❌ 資料庫儲存失敗，請聯繫管理員檢查 DB 連線。"
                else:
                    reply_text = f"ℹ️ **{reporter_name}** 已經是回報人，無需重複新增。"

    # 2. 處理回報指令: "YYYY.MM.DD（X）人名" (只擷取訊息開頭的格式)
    elif re.search(r'^\s*(\d{4}[./]\d{1,2}[./]\d{1,2})\s*（[^）]+）?\s*([^\n\r]+)', text):
        
        match = re.search(r'^\s*(\d{4}[./]\d{1,2}[./]\d{1,2})\s*（[^）]+）?\s*([^\n\r]+)', text)
        
        if match:
            date_str = match.group(1).replace('/', '.') # 取得 YYYY.MM.DD
            name = match.group(2).strip() # 擷取人名，並去除多餘空格
            
            if not name:
                 reply_text = "⚠️ 回報格式不正確。\n\n正確格式：`YYYY.MM.DD（X）人名`\n範例：`2025.11.09（日）伊森`"
            else:
                try:
                    report_date = datetime.strptime(date_str, '%Y.%m.%d').date()
                    
                    success = save_report(report_date, name, user_id, source_id)
                    
                    if success:
                        reply_text = f"✅ 紀錄成功！\n回報者: **{name}**\n日期: **{report_date.strftime('%Y/%m/%d')}**\n\n感謝您的回報！"
                    else:
                        reply_text = "❌ 資料庫儲存失敗，請聯繫管理員檢查 DB 連線。"
                    
                except ValueError:
                    reply_text = "❌ 日期格式錯誤！請使用 YYYY.MM.DD 或 YYYY/MM/DD 格式。\n\n範例：`2025.11.09（日）伊森`"
    
    # 3. 預設回覆
    else:
        reply_text = "我是鐵路回報紀錄 Bot。\n\n請使用以下格式紀錄：\n`YYYY.MM.DD（X）人名`\n`新增人名 [人名]`\n\n範例：`2025.11.09（日）伊森`"
        
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