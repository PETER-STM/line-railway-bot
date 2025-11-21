import os
import sys
import re
from datetime import datetime, timedelta
import psycopg2
import argparse 

# 引入 LINE Bot 相關
from linebot import LineBotApi
from linebot.exceptions import LineBotApiError
from linebot.models import TextSendMessage

# --- 姓名正規化工具 ---
def normalize_name(name):
    normalized = re.sub(r'^\s*[\(（\[【][^()\[\]]{1,10}[\)）\]】]\s*', '', name).strip()
    return normalized if normalized else name

# --- 環境變數 ---
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
DATABASE_URL = os.environ.get('DATABASE_URL')
EXCLUDE_GROUP_IDS_STR = os.environ.get('EXCLUDE_GROUP_IDS', '')
EXCLUDE_GROUP_IDS = set(EXCLUDE_GROUP_IDS_STR.split(',')) if EXCLUDE_GROUP_IDS_STR else set()

if not LINE_CHANNEL_ACCESS_TOKEN or not DATABASE_URL:
    print("FATAL ERROR: Missing env vars.", file=sys.stderr)
    sys.exit(1)

try:
    line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
except Exception as e:
    sys.exit(1)

def get_db_connection():
    try:
        return psycopg2.connect(DATABASE_URL, sslmode='require')
    except Exception as e:
        print(f"DB CONNECTION ERROR: {e}", file=sys.stderr)
        return None

def check_and_send_reminders(days_ago=1):
    """
    檢查心得提交情況。
    """
    print(f"--- Scheduler check started (days_ago={days_ago}) ---", file=sys.stderr)
    
    conn = get_db_connection()
    if not conn: return

    try:
        # 檢查是否全域暫停
        cur = conn.cursor()
        cur.execute("SELECT value FROM settings WHERE key = 'is_paused'")
        res = cur.fetchone()
        if res and res[0] == 'true':
            print("INFO: Scheduler is PAUSED globally.", file=sys.stderr)
            return

        # 設定日期
        target_date = (datetime.utcnow() - timedelta(days=days_ago)).date()
        
        reminder_text_ending = "不要逼系統變成奧客催款模式 😌"
        if days_ago == 0:
            reminder_text_ending = "大家加油，不要忘了完成任務喔！💪"

        # 1. 取得群組
        cur.execute("SELECT DISTINCT group_id FROM reporters")
        group_ids = [row[0] for row in cur.fetchall()]

        for group_id in group_ids:
            if group_id in EXCLUDE_GROUP_IDS:
                continue

            # 2. 取得該群組所有成員 (正規化後去重)
            cur.execute("SELECT reporter_name FROM reporters WHERE group_id = %s", (group_id,))
            all_names = [row[0] for row in cur.fetchall()]
            unique_vips = {normalize_name(n) for n in all_names}

            if not unique_vips: continue

            # 3. 取得已提交名單 (正規化後去重)
            cur.execute("SELECT reporter_name FROM reports WHERE group_id = %s AND report_date = %s", (group_id, target_date))
            submitted_names = [row[0] for row in cursor.fetchall()]
            submitted_vips = {normalize_name(n) for n in submitted_names}

            # 4. 找出未交
            missing = sorted(list(unique_vips - submitted_vips))

            if missing:
                list_names = "\n".join([f"- {n}" for n in missing])
                msg = (
                    f"📢 心得分享催繳大隊報到 📢\n"
                    f"日期: {target_date.strftime('%Y/%m/%d')}\n\n"
                    f"以下 VIP 仍未交心得：\n{list_names}\n\n"
                    f"{reminder_text_ending}"
                )
                try:
                    line_bot_api.push_message(group_id, TextSendMessage(text=msg))
                    print(f"Sent reminder to {group_id}", file=sys.stderr)
                except LineBotApiError as e:
                    print(f"PUSH ERROR {group_id}: {e}", file=sys.stderr)

    finally:
        conn.close()
    print("--- Scheduler check finished ---", file=sys.stderr)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--days-ago', type=int, default=1)
    args = parser.parse_args()
    check_and_send_reminders(args.days_ago)