# scheduler.py - 排程應用程式 (LINE SDK V2 最終穩定版)

import os
import re
import psycopg2
from datetime import datetime
from flask import Flask, request, abort 
# =========================================================
# 【V2 核心】導入 Line SDK V2 類別
# =========================================================
from linebot import LineBotApi
# V2 例外名稱不同，直接從 linebot.exceptions 導入
from linebot.exceptions import LineBotApiError as ApiException 
from linebot.models import TextMessage

# --- Line Bot Setup ---
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")

# V2: 建立客戶端
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)

# Flask 應用初始化 (這裡只需要一個簡單的 Flask 應用來啟動 Worker)
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

# --- 資料庫操作：獲取群組列表 (保持不變) ---
def get_groups_with_missing_reports():
    conn = get_db_connection()
    if not conn:
        print("Scheduler: DB connection failed.")
        return {}

    try:
        cur = conn.cursor()
        
        # 1. 獲取所有群組和回報人
        cur.execute("SELECT group_id, reporter_name FROM group_reporters")
        all_reporters = cur.fetchall()
        
        # 2. 獲取今天已經回報的名單
        today_date = datetime.now().date()
        sql_today = "SELECT source_id, name FROM reports WHERE report_date = %s"
        cur.execute(sql_today, (today_date,))
        reported_today = cur.fetchall()
        
        reported_set = set((source_id, name) for source_id, name in reported_today)
        
        # 3. 找出所有未回報的名單
        missing_reports = {}
        for group_id, reporter_name in all_reporters:
            if (group_id, reporter_name) not in reported_set:
                if group_id not in missing_reports:
                    missing_reports[group_id] = []
                missing_reports[group_id].append(reporter_name)
                
        cur.close()
        return missing_reports
    except Exception as e:
        print(f"Scheduler DB error: {e}")
        return {}
    finally:
        if conn: conn.close()

# --- 排程任務邏輯 ---
def send_daily_reminder():
    """發送每日未回報提醒到各群組"""
    
    missing_data = get_groups_with_missing_reports()
    
    if not missing_data:
        print("Scheduler: No missing reports found today.")
        return
        
    for group_id, reporters in missing_data.items():
        if reporters:
            reporters_list = "、".join(reporters)
            message = f"🔔 **每日回報提醒**\n\n今天 (**{datetime.now().strftime('%Y/%m/%d')}**) 尚未回報的成員有：\n\n{reporters_list}\n\n請記得在 LINE 群組中輸入：\n`YYYY.MM.DD 您的名字` 進行回報！"
            
            print(f"Sending reminder to group {group_id} for: {reporters_list}")
            
            try:
                # V2: 使用 line_bot_api.push_message
                line_bot_api.push_message(
                    to=group_id,
                    messages=TextMessage(text=message) # V2 的 messages 參數可以是單一物件
                )
            except ApiException as e:
                print(f"Failed to send message to {group_id}: {e}")
            except Exception as e:
                print(f"Unexpected error when pushing message: {e}")

# --- Worker 啟動點 (用於 Procfile 中的 worker: 命令) ---
@app.route("/run_scheduler")
def run_scheduler():
    """手動觸發排程（可作為 Cron Job Endpoint）"""
    print("--- Scheduler Task Started ---\n")
    send_daily_reminder()
    print("\n--- Scheduler Task Finished ---")
    return "Scheduler ran successfully", 200

# -----------------------------------------------------------
# Flask 啟動 (本地測試用)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)