import os
import sys
import time
from datetime import datetime, timedelta
import schedule 
import psycopg2
from dateutil import tz # 處理時區

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
        # 連線到 PostgreSQL，使用 sslmode='require' 以符合 Heroku/Railway 要求
        conn = psycopg2.connect(DATABASE_URL, sslmode='require')
        return conn
    except Exception as e:
        print(f"DATABASE CONNECTION ERROR in scheduler: {e}", file=sys.stderr)
        return None

# --- 排程任務邏輯 ---

def check_and_remind_reports():
    """檢查所有群組，找出今日尚未回報的成員並發送提醒"""
    if line_bot_api is None:
        print("Scheduler skipped: LineBotApi not initialized.", file=sys.stderr)
        return

    conn = get_db_connection()
    if conn is None:
        return

    cur = conn.cursor()
    try:
        # 今天的日期 (使用 Asia/Taipei 時區)
        date_today = datetime.now(tz=tz.gettz('Asia/Taipei')).date()
        date_today_str = date_today.strftime('%Y-%m-%d')
        date_today_display = date_today.strftime('%Y.%m.%d')
        print(f"--- Running scheduler check for {date_today_str} ---", file=sys.stderr)

        # 1. 取得所有有成員的群組 ID
        cur.execute("SELECT DISTINCT group_id FROM reporters")
        group_ids = [row[0] for row in cur.fetchall()]

        for group_id in group_ids:
            if group_id in EXCLUDE_GROUP_IDS:
                print(f"Skipping excluded group: {group_id}", file=sys.stderr)
                continue

            # 2. 找出該群組中所有成員
            cur.execute(
                "SELECT reporter_name FROM reporters WHERE group_id = %s ORDER BY reporter_name",
                (group_id,)
            )
            all_reporters = [row[0] for row in cur.fetchall()]

            if not all_reporters:
                continue

            # 3. 找出該群組中今日已回報的成員
            cur.execute(
                "SELECT reporter_name FROM reports WHERE group_id = %s AND report_date = %s",
                (group_id, date_today_str)
            )
            reported_reporters = set(row[0] for row in cur.fetchall())

            # 4. 計算尚未回報的成員
            missing_reports = [name for name in all_reporters if name not in reported_reporters]

            if missing_reports:
                missing_list_str = "\n" + "\n".join(missing_reports) # 準備成員列表 (無前面的 - )

                # --- 訊息模板 (活潑風格，已移除粗體和空格) ---
                if len(missing_reports) == 1:
                    reporter_name = missing_reports[0]
                    # 單人未回報 - 移除空格
                    message_text = (
                        f"🔔 心得分享提醒 🔔\n今天快截止囉～\n\n"
                        f"目前還沒收到{reporter_name}的回報 ({date_today_display})。\n"
                        f"兄弟姊妹，別再拖了，\n"
                        f"再不回報我都要先幫你寫一篇了 😏"
                    )
                else:
                    # 多人未回報 (此處無人名變數插入，故無需調整)
                    message_text = (
                        f"📢 心得分享催繳大隊報到 📢\n"
                        f"以下 VIP 仍未交心得：\n"
                        f"{missing_list_str}\n\n"
                        f"大家快來補交吧～\n"
                        f"不要逼系統變成奧客催款模式 😌"
                    )
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

# 設定每天在 UTC 01:00 執行檢查 (對應台北時間 UTC+8 的早上 9:00)
schedule.every().day.at("01:00").do(check_and_remind_reports)

if __name__ == "__main__":
    if LINE_CHANNEL_ACCESS_TOKEN and DATABASE_URL:
        print("Scheduler worker started. Checking reports daily at 01:00 UTC (9:00 AM TST).", file=sys.stderr)
        while True:
            schedule.run_pending()
            time.sleep(1)
    else:
        print("Scheduler is not running due to missing environment variables.", file=sys.stderr)