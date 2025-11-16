# app.py - Line Bot Webhook 處理和資料庫互動 (LINE SDK V2 最終穩定版)

import os
import re
import psycopg2
from datetime import datetime
from flask import Flask, request, abort 

# =========================================================
# 【V2 核心】導入 Line SDK V2 類別
# =========================================================
from linebot import LineBotApi, WebhookHandler
# 修正：V2 只導入 TextMessage (V2 中沒有 TextMessageContent)
from linebot.exceptions import InvalidSignatureError, LineBotApiError 
from linebot.models import MessageEvent, TextMessage 

# --- Line Bot Setup ---
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET")

# V2: 建立客戶端和 Handler
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET) 

# Flask 應用初始化
app = Flask(__name__)

# --- 資料庫連線函式 (保持不變) ---
def get_db_connection():
    """使用環境變數連線到 PostgreSQL (優先使用 DATABASE_URL)"""
    conn_url = os.environ.get("DATABASE_URL")
    if conn_url:
        try:
            return psycopg2.connect(conn_url)
        except Exception as e:
            print(f"Database connection via DATABASE_URL failed: {e}")
            return None
    
    try:
        conn = psycopg2.connect(
            host=os.environ.get('PGHOST'), 
            database=os.environ.get('PGDATABASE'),
            user=os.environ.get('PGUSER'),
            password=os.environ.get('PGPASSWORD'),
            port=os.environ.get('PGPORT')
        )
        return conn
    except Exception as e:
        print(f"Database connection failed: {e}")
        return None

# --- 資料庫操作：新增人名 ---
def add_reporter(source_id, name):
    conn = get_db_connection()
    if not conn:
        return False, "❌ 資料庫儲存失敗，請聯繫管理員檢查 DB 連線。"

    try:
        cur = conn.cursor()
        sql = "INSERT INTO group_reporters (group_id, reporter_name) VALUES (%s, %s)"
        cur.execute(sql, (source_id, name))
        conn.commit()
        cur.close()
        return True, f"✅ 已成功新增：**{name}** 為回報人！"
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        return False, f"⚠️ **{name}** 已經是本群組的回報人了！"
    except Exception as e:
        conn.rollback()
        print(f"Error adding reporter: {e}")
        return False, "❌ 資料庫儲存失敗，請聯繫管理員檢查 DB 連線。"
    finally:
        if conn: conn.close()

# --- 資料庫操作：刪除人名 ---
def delete_reporter(source_id, name):
    conn = get_db_connection()
    if not conn:
        return False, "❌ 資料庫連線失敗，無法執行刪除。"

    try:
        cur = conn.cursor()
        sql = "DELETE FROM group_reporters WHERE group_id = %s AND reporter_name = %s"
        cur.execute(sql, (source_id, name))
        
        if cur.rowcount > 0:
            conn.commit()
            cur.close()
            return True, f"🗑️ 已成功刪除：**{name}**。"
        else:
            conn.rollback()
            cur.close()
            return False, f"⚠️ 查無此人：**{name}** 不在本群組的回報人名單中。"
            
    except Exception as e:
        conn.rollback()
        print(f"Error deleting reporter: {e}")
        return False, "❌ 資料庫操作失敗，請聯繫管理員。"
    finally:
        if conn: conn.close()

# --- 資料庫操作：儲存回報 ---
def save_report(report_date, name, source_id):
    conn = get_db_connection()
    if not conn:
        return False

    try:
        cur = conn.cursor()
        sql = "INSERT INTO reports (report_date, name, source_id) VALUES (%s, %s, %s)"
        cur.execute(sql, (report_date, name, source_id))
        conn.commit()
        cur.close()
        return True
    except Exception as e:
        conn.rollback()
        print(f"Error saving report: {e}")
        return False
    finally:
        if conn: conn.close()

# -----------------------------------------------------------
# Flask Webhook 路由 (使用 V2 WebhookHandler 處理請求)

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    
    try:
        # V2: 使用 handler.handle 呼叫被裝飾的函式
        handler.handle(body, signature)
    except InvalidSignatureError:
        print("Invalid signature. Check your channel secret.")
        abort(400)
    except Exception as e:
        print(f"Webhook handling error: {e}")
        return 'OK' 

    return 'OK'

# -----------------------------------------------------------
# Line 訊息處理邏輯 (使用 WebhookHandler Decorator)

@handler.add(MessageEvent, message=TextMessage) # <-- V2 修正
def handle_text_message(event):
    """處理接收到的 Line 文本訊息事件 (由 WebhookHandler 自動觸發)"""
    
    text = event.message.text.strip()
    # 統一獲取來源 ID (群組或用戶) - V2 獲取方式
    source_id = event.source.group_id if hasattr(event.source, 'group_id') else \
                (event.source.room_id if hasattr(event.source, 'room_id') else event.source.user_id) 

    reply_message = None

    # 1. 處理「新增人名」指令
    match_add = re.match(r'^\s*新增人名\s+([^\n\r]+)', text)
    if match_add:
        name_to_add = match_add.group(1).strip()
        success, message = add_reporter(source_id, name_to_add)
        reply_message = TextMessage(text=message)
        
    # 2. 處理「刪除人名」指令
    elif match_delete := re.match(r'^\s*刪除人名\s+([^\n\r]+)', text):
        name_to_delete = match_delete.group(1).strip()
        success, message = delete_reporter(source_id, name_to_delete)
        reply_message = TextMessage(text=message)
    
    # 3. 處理「回報」指令
    elif match_report := re.match(r'^\s*(\d{4}[./]\d{1,2}[./]\d{1,2})\s*（[^）]+）?\s*([^\n\r]+)', text):
        date_str = match_report.group(1).replace('/', '.')
        name = match_report.group(2).strip()
        
        try:
            report_date = datetime.strptime(date_str, '%Y.%m.%d').date()
        except ValueError:
            reply_message = TextMessage(text="❌ 日期格式錯誤。請使用 YYYY.MM.DD 格式。")
        else:
            if save_report(report_date, name, source_id):
                reply_message = TextMessage(text=f"✅ 紀錄成功！\n回報者: **{name}**\n日期: **{report_date.strftime('%Y/%m/%d')}**\n\n感謝您的回報！")
            else:
                reply_message = TextMessage(text="❌ 資料庫儲存失敗，請聯繫管理員檢查 DB 連線。")
        
    # 4. 如果有需要回覆的訊息，嘗試回覆
    if reply_message:
        try:
            # V2 API Call: line_bot_api.reply_message
            line_bot_api.reply_message(
                event.reply_token,
                reply_message
            )
        # 捕捉 V2 API 錯誤，並打印詳細資訊到日誌
        except LineBotApiError as e: 
            print(f"============================================================")
            print(f"🚨 LINE API 回覆失敗！請檢查 Channel Access Token 和 Secret！")
            print(f"LINE API Error: {e.status_code} - {e.error.message}")
            print(f"============================================================")
        except Exception as e:
            print(f"🚨 意外錯誤：回覆訊息時發生例外：{e}")
    
    # 5. 處理「雜訊」（非指令訊息）
    return 

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)