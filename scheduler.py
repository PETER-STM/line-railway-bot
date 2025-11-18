import os
import sys
import time
from datetime import datetime, timedelta
import schedule 
import psycopg2

# 引入 LINE Bot 相關
from linebot import LineBotApi
from linebot.exceptions import LineBotApiError
from linebot.models import TextSendMessage

# --- 環境變數設定 ---
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
DATABASE_URL = os.environ.get('DATABASE_URL')

# --- 診斷與初始化 ---
if not LINE_CHANNEL_ACCESS_TOKEN or not DATABASE_URL:
    print("ERROR: Missing required environment variables for scheduler! Cannot start worker.", file=sys.stderr)
    line_bot_api = None 
else:
    try:
        # 初始化 LINE Bot API
        line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
    except Exception as e:
        print(f"Failed to initialize LineBotApi in scheduler: {e}", file=sys.stderr)
        line_bot_api = None

# --- 資料庫連線函式 ---
def get_db_connection():
    """建立資料庫連線"""
    try:
        # 使用 sslmode='require' 連接 Railway PostgreSQL
        conn = psycopg2.connect(DATABASE_URL, sslmode='require')
        return conn
    except Exception as e:
        print(f"DATABASE CONNECTION ERROR in scheduler: {e}", file=sys.stderr)
        return None

# --- 資料庫輔助函式 ---
def get_all_reporters(conn):
    """從 group_reporters 表格中獲取所有群組和回報者名稱"""
    cur = conn.cursor()
    # 這裡假設 group_id 是 reports 表格中的 source_id
    cur.execute("SELECT group_id, reporter_name FROM group_reporters ORDER BY group_id;")
    all_reporters = cur.fetchall()
    return all_reporters

# --- 核心邏輯：發送每日提醒 ---
def send_daily_reminder_task():
    """排程工作：檢查前一天的回報並發送 LINE 提醒"""
    if line_bot_api is None:
        print("Scheduler task skipped: LINE API is not initialized.", file=sys.stderr)
        return

    conn = get_db_connection()
    if conn is None:
        print("Scheduler task skipped due to database connection failure.", file=sys.stderr)
        return 

    # 檢查昨天 (今天執行，檢查昨天的進度)
    check_date = datetime.now().date() - timedelta(days=1)
    check_date_str = check_date.strftime('%Y.%m.%d')
    
    print(f"--- Running daily reminder check for date: {check_date_str} ---", file=sys.stderr)

    try:
        all_reporters = get_all_reporters(conn)
        
        # 將回報者按群組 ID 分組
        groups_to_check = {}
        for group_id, reporter_name in all_reporters:
            if group_id not in groups_to_check:
                groups_to_check[group_id] = []
            groups_to_check[group_id].append(reporter_name)

        for group_id, reporters in groups_to_check.items():
            missing_reports = []
            
            with conn.cursor() as cur:
                # 檢查每個回報者是否在 'reports' 表中有昨日的記錄
                for reporter_name in reporters:
                    # 注意：reports 表中的欄位是 group_id, report_date, name
                    cur.execute("SELECT name FROM reports WHERE group_id = %s AND report_date = %s AND name = %s;", 
                                (group_id, check_date, reporter_name))
                    
                    if not cur.fetchone():
                        missing_reports.append(reporter_name)

            if missing_reports:
                is_singular = len(missing_reports) == 1
                
                # --- 心得催交模板 ---
                message_text = f"⏰ 心得催交提醒\n\n"
                message_text += f"大家好～\n"
                message_text += f"截至 {check_date_str}，以下同學的心得還沒交👇\n\n"
                
                missing_list_text = "\n".join([f"👉 {name}" for name in missing_reports])
                message_text += missing_list_text
                
                if is_singular:
                    message_text += "\n\n📌 小提醒：再不交心得，我的 咚錢模式就要開啟啦💸\n"
                    message_text += "💡 快交上來吧，別讓我每天都在追著你問～\n\n"
                    message_text += "期待看到你的 心得分享，別讓我一直盯著這份名單 😏"
                else:
                    message_text += "\n\n📌 小提醒：再不交心得，我的 咚錢模式就要開啟啦💸\n"
                    message_text += "💡 快交上來吧，別讓我每天都在追著你們問～\n\n"
                    message_text += "期待看到你們的 心得分享，別讓我一直盯著這份名單 😏"
                # --- 模板結束 ---
                
                try:
                    # 使用 PUSH 訊息發送提醒
                    line_bot_api.push_message(group_id, TextSendMessage(text=message_text))
                    print(f"Sent reminder to group {group_id} for {len(missing_reports)} missing reports.", file=sys.stderr)
                except LineBotApiError as e:
                    print(f"LINE API PUSH ERROR to {group_id}: {e}", file=sys.stderr)
                    
    except Exception as e:
        print(f"SCHEDULER DB/Logic ERROR: {e}", file=sys.stderr)
    finally:
        if conn: conn.close()
    
    print("--- Scheduler check finished. ---", file=sys.stderr)

# --- 排程設定與執行 ---

# 設定每天在 UTC 01:00 執行檢查 (對應台灣時間 TST/UTC+8 的 09:00 AM)
TARGET_TIME_UTC = "01:00" 

schedule.every().day.at(TARGET_TIME_UTC).do(send_daily_reminder_task)

# Worker 啟動主循環
if __name__ == "__main__":
    print(f"Worker process started. Daily task scheduled for {TARGET_TIME_UTC} UTC.", file=sys.stderr)
    while True:
        try:
            # 運行所有等待執行的排程任務
            schedule.run_pending()
            # 讓 CPU 休息一下，每秒檢查一次
            time.sleep(1) 
        except Exception as e:
            print(f"Error in scheduler loop: {e}", file=sys.stderr)
            time.sleep(5) # 發生錯誤時稍等一下