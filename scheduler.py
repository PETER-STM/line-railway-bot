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
        # 連線到 PostgreSQL
        conn = psycopg2.connect(DATABASE_URL, sslmode='require')
        return conn
    except Exception as e:
        print(f"DATABASE CONNECTION ERROR: {e}", file=sys.stderr)
        return None

# --- 核心排程邏輯 ---
def check_and_send_reminders():
    """
    檢查所有群組中是否有未回報的成員，並發送提醒。
    """
    if line_bot_api is None:
        print("LINE Bot API is not initialized. Skipping reminder check.", file=sys.stderr)
        return

    print("--- Starting scheduler check... ---", file=sys.stderr)

    conn = get_db_connection()
    if conn is None:
        return

    cur = conn.cursor()
    # 提醒日期設定為今天 (UTC time)
    today = datetime.utcnow().date()
    date_str = today.strftime('%Y.%m.%d')

    try:
        # 1. 取得所有有成員的 group_id
        cur.execute("SELECT DISTINCT group_id FROM reporters")
        group_ids = [row[0] for row in cur.fetchall()]

        for group_id in group_ids:
            # 跳過排除名單中的群組 (用於開發測試)
            if group_id in EXCLUDE_GROUP_IDS:
                print(f"Skipping excluded group: {group_id}", file=sys.stderr)
                continue

            # 2. 取得該群組所有成員名單
            cur.execute(
                "SELECT reporter_name FROM reporters WHERE group_id = %s",
                (group_id,)
            )
            all_reporters = [row[0] for row in cur.fetchall()]

            # 3. 取得該群組今天已回報的成員名單
            cur.execute(
                "SELECT reporter_name FROM reports WHERE group_id = %s AND report_date = %s",
                (group_id, today)
            )
            reported_reporters = set(row[0] for row in cur.fetchall())

            # 4. 找出未回報的成員
            missing_reports = [name for name in all_reporters if name not in reported_reporters]
            
            if missing_reports:
                # 5. 準備提醒訊息 (使用活潑幽默模板)
                
                if len(missing_reports) == 1:
                    # 單人未回報
                    reporter_name = missing_reports[0]
                    message_text = (
                        f"🔔 心得分享提醒 🔔\n"
                        f"今天快截止囉～\n\n"
                        f"目前還沒收到 {reporter_name} 的回報 ({date_str})。\n"
                        f"兄弟姊妹，別再拖了，\n\n"
                        f"再不回報我都要先幫你寫一篇了 😏"
                    )
                else:
                    # 多人未回報
                    list_of_names = "\n".join(missing_reports)
                    message_text = (
                        f"📢 心得分享催繳大隊報到 📢\n"
                        f"以下 VIP 仍未交心得：\n\n"
                        f"{list_of_names}\n\n"
                        f"大家快來補交吧～\n\n"
                        f"不要逼系統變成奧客催款模式 😌"
                    )

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
    
    print("--- Scheduler check finished. ---\n", file=sys.stderr)

# --- 排程設定與執行 ---

# 設定每天在 UTC 01:00 執行檢查 (對應台灣時間 TST 09:00)
schedule.every().day.at("01:00").do(check_and_send_reminders)

# 設定每天在 UTC 13:00 執行檢查 (對應台灣時間 TST 21:00，第二次提醒/截止前提醒)
schedule.every().day.at("13:00").do(check_and_send_reminders)


if __name__ == "__main__":
    print("Scheduler worker started.", file=sys.stderr)
    # 首次啟動時先執行一次，避免剛部署時錯過時間
    # 注意：在 Heroku/Railway 這類環境，worker 啟動時間可能不固定，因此首次執行很有用
    check_and_send_reminders() 
    
    while True:
        schedule.run_pending()
        time.sleep(60) # 每 60 秒檢查一次排程