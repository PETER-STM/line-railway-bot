import os
import sys
from datetime import date, timedelta
from flask import Flask
from linebot import LineBotApi
from linebot.exceptions import LineBotApiError
from linebot.models import TextSendMessage
import psycopg2

# --- 環境變數設定 ---
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
DATABASE_URL = os.environ.get('DATABASE_URL')

# 檢查變數
if not LINE_CHANNEL_ACCESS_TOKEN or not DATABASE_URL:
    print("ERROR: Missing required environment variables for scheduler!", file=sys.stderr)
    # 允許 scheduler 繼續運行，但推送會失敗
else:
    # 初始化 LINE Bot API
    line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)

app = Flask(__name__)

# --- 資料庫連線函式 ---
def get_db_connection():
    try:
        conn = psycopg2.connect(DATABASE_URL)
        return conn
    except Exception as e:
        print(f"DATABASE CONNECTION ERROR in scheduler: {e}", file=sys.stderr)
        return None

# --- 核心邏輯：發送每日提醒 ---
def send_daily_reminder():
    if not LINE_CHANNEL_ACCESS_TOKEN:
        print("Scheduler skipped: LINE_CHANNEL_ACCESS_TOKEN is missing.", file=sys.stderr)
        return "Error: Missing LINE_CHANNEL_ACCESS_TOKEN"

    conn = get_db_connection()
    if conn is None:
        return "Error: Database connection failed."

    # 設定要檢查的日期 (通常是昨天)
    check_date = date.today() - timedelta(days=1)
    check_date_str = check_date.strftime('%Y.%m.%d')
    
    print(f"Scheduler running for date: {check_date_str}", file=sys.stderr)

    try:
        with conn.cursor() as cur:
            # 1. 獲取所有群組及其所有回報人
            cur.execute("SELECT group_id, reporter_name FROM group_reporters ORDER BY group_id;")
            all_reporters = cur.fetchall()

            groups_to_check = {}
            for group_id, reporter_name in all_reporters:
                if group_id not in groups_to_check:
                    groups_to_check[group_id] = []
                groups_to_check[group_id].append(reporter_name)

            # 2. 針對每個群組檢查未回報的人
            for group_id, reporters in groups_to_check.items():
                missing_reports = []
                
                for reporter_name in reporters:
                    # 檢查該回報人在該日期是否有報告記錄
                    cur.execute("SELECT name FROM reports WHERE group_id = %s AND report_date = %s AND name = %s;", 
                                (group_id, check_date, reporter_name))
                    
                    if not cur.fetchone():
                        missing_reports.append(reporter_name)

                # 3. 如果有未回報的人，則發送提醒
                if missing_reports:
                    message_text = f"🚨 **{check_date_str}** 回報提醒！以下成員尚未回報：\n\n"
                    message_text += "\n".join([f"👉 {name}" for name in missing_reports])
                    message_text += "\n\n請儘快回報！"
                    
                    try:
                        line_bot_api.push_message(group_id, TextSendMessage(text=message_text))
                        print(f"Sent reminder to group {group_id} for {len(missing_reports)} missing reports.", file=sys.stderr)
                    except LineBotApiError as e:
                        # 如果 Bot 不在群組中，會引發錯誤
                        print(f"LINE API PUSH ERROR to {group_id}: {e}", file=sys.stderr)
                        
    except Exception as e:
        print(f"SCHEDULER DB ERROR: {e}", file=sys.stderr)
        return f"Error during schedule processing: {e}"
    finally:
        conn.close()
    
    return "Scheduler execution finished successfully."

# --- 觸發路由 (供 Railway Cron Job 調用) ---
@app.route("/run_scheduler")
def run_scheduler_endpoint():
    result = send_daily_reminder()
    return result

# --- Worker 啟動 (不需要監聽端口，但需要啟動 Flask 應用程序以供 Cron Job 訪問) ---
if __name__ == "__main__":
    # Worker 通常不需要運行在 Web Server 模式，但在 Railway 中，我們用它來提供 Cron 訪問
    app.run(debug=True, host='0.0.0.0', port=os.getenv('PORT', 8080))