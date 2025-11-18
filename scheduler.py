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

# NEW: 排除的群組ID列表 (用於跳過特定群組的提醒)
EXCLUDE_GROUP_IDS_STR = os.environ.get('EXCLUDE_GROUP_IDS', '')
EXCLUDE_GROUP_IDS = set(EXCLUDE_GROUP_IDS_STR.split(',')) if EXCLUDE_GROUP_IDS_STR else set()

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

# --- NEW: 全域設定檢查函式 ---
def get_pause_state(conn):
    """從資料庫檢查全域提醒是否已暫停。"""
    is_paused = False
    try:
        with conn.cursor() as cur:
            # 確保資料表中 'is_paused' 鍵存在 (如果不存在，則插入預設值)
            cur.execute("INSERT INTO settings (key, value) VALUES ('is_paused', 'false') ON CONFLICT (key) DO NOTHING;")
            conn.commit()
            
            # 查詢當前狀態
            cur.execute("SELECT value FROM settings WHERE key = 'is_paused';")
            result = cur.fetchone()
            if result and result[0] == 'true':
                is_paused = True
    except Exception as e:
        print(f"DB ERROR (get_pause_state): {e}", file=sys.stderr)
        # 如果資料庫連線失敗，為了安全起見，不暫停提醒 (除非主應用程式已明確暫停)
    return is_paused

# --- 排程任務邏輯 ---
def send_daily_reminder_task():
    """檢查昨天的回報狀態，並對未回報的群組發送催交通知。"""
    
    conn = get_db_connection()
    if conn is None or line_bot_api is None:
        print("Scheduler skipped: DB or Line API initialization failed.", file=sys.stderr)
        return
        
    # --- NEW: 1. 檢查全域暫停狀態 ---
    is_paused = get_pause_state(conn)
    if is_paused:
        print("Scheduler is paused globally. Skipping daily reminder check.", file=sys.stderr)
        if conn: conn.close()
        return

    # 檢查前一天 (昨天) 的回報狀態
    check_date = datetime.now().date() - timedelta(days=1)
    check_date_str = check_date.strftime('%Y.%m.%d')
    
    print(f"--- Scheduler running check for date: {check_date_str} ---", file=sys.stderr)

    try:
        with conn.cursor() as cur:
            # 獲取所有群組的回報者名單
            cur.execute("SELECT group_id, reporter_name FROM group_reporters ORDER BY group_id, reporter_name;")
            all_reporters = cur.fetchall()
            
            if not all_reporters:
                print("No reporters registered across all groups. Skipping.", file=sys.stderr)
                return

            groups_to_check = {}
            for group_id, reporter_name in all_reporters:
                # NEW: 排除特定群組
                if group_id in EXCLUDE_GROUP_IDS:
                    continue 

                if group_id not in groups_to_check:
                    groups_to_check[group_id] = []
                groups_to_check[group_id].append(reporter_name)

            for group_id, reporters in groups_to_check.items():
                missing_reports = []
                
                # 檢查未回報者
                for reporter_name in reporters:
                    cur.execute("SELECT name FROM reports WHERE group_id = %s AND report_date = %s AND name = %s;", 
                                (group_id, check_date, reporter_name))
                    
                    if not cur.fetchone():
                        missing_reports.append(reporter_name)

                # 構造並發送 push 訊息
                if missing_reports:
                    is_singular = len(missing_reports) == 1
                    
                    message_text = f"🚨 心得催交通知 🚨\n\n"
                    message_text += f"大家好～\n"
                    message_text += f"截至 {check_date_str}，以下同學的心得還沒交👇\n\n"
                    
                    missing_list_text = "\n".join([f"👉 {name}" for name in missing_reports])
                    message_text += missing_list_text
                    
                    # --- 催交模板 ---
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

# 啟動排程循環
while True:
    try:
        schedule.run_pending()
        time.sleep(1)
    except Exception as e:
        print(f"Scheduler loop error: {e}", file=sys.stderr)
        time.sleep(5) # 發生錯誤時暫停一下