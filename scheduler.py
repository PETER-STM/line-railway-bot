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

# --- 姓名正規化工具 (與 app.py 保持一致) ---
def normalize_name(name):
    # 移除開頭括號內容 (如：(三) 浣熊 -> 浣熊)
    normalized = re.sub(r'^\s*[（(\[【][^()\[\]]{1,10}[)）\\]】]\s*', '', name).strip()
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
    print(f"LINE BOT API INIT ERROR: {e}", file=sys.stderr)
    sys.exit(1)

def get_db_connection():
    try:
        # 使用 sslmode='require' 以確保安全連線
        return psycopg2.connect(DATABASE_URL, sslmode='require')
    except Exception as e:
        print(f"DB CONNECTION ERROR: {e}", file=sys.stderr)
        return None

def check_and_send_reminders(days_ago=1):
    """
    檢查心得提交情況並發送提醒。
    """
    print(f"--- Scheduler check started (days_ago={days_ago}) ---", file=sys.stderr)
    
    conn = get_db_connection()
    if not conn: return

    try:
        cur = conn.cursor()

        # 1. 檢查是否全域暫停
        cur.execute("SELECT value FROM settings WHERE key = 'is_paused'")
        res = cur.fetchone()
        if res and res[0] == 'true':
            print("INFO: Scheduler is PAUSED globally.", file=sys.stderr)
            return

        # 設定日期
        target_date = (datetime.utcnow() - timedelta(days=days_ago)).date()
        
        header = "📢 心得分享催繳大隊報到 📢"
        reminder_text_ending = "不要逼系統變成奧客催款模式 😌"
        if days_ago == 0:
            header = "🔔 今日提醒 (打卡開始了喔)"
            reminder_text_ending = "大家加油，不要忘了完成任務喔！💪"

        # 2. 取得群組 (修正: reporters -> group_vips)
        cur.execute("SELECT DISTINCT group_id FROM group_vips")
        group_ids = [row[0] for row in cur.fetchall()]

        for group_id in group_ids:
            if group_id in EXCLUDE_GROUP_IDS:
                continue

            # 3. 取得該群組所有成員 (修正: reporters -> group_vips, 並取得 normalized_vip_name)
            cur.execute("SELECT normalized_vip_name FROM group_vips WHERE group_id = %s", (group_id,))
            all_normalized_names = {row[0] for row in cur.fetchall()} # 使用 set 避免重複

            if not all_normalized_names: continue

            # 4. 取得已提交名單 (reports 表中存的是 normalized_reporter_name)
            cur.execute(
                "SELECT normalized_reporter_name FROM reports WHERE group_id = %s AND report_date = %s", 
                (group_id, target_date)
            )
            submitted_normalized_names = {row[0] for row in cur.fetchall()}

            # 5. 找出未交 (使用正規化名稱比較)
            missing_normalized = sorted(list(all_normalized_names - submitted_normalized_names))

            if missing_normalized:
                # 這裡直接列出 normalized name (通常也是乾淨的姓名)
                list_names = "\n".join([f"- {n}" for n in missing_normalized])
                
                msg = (
                    f"{header}\n"
                    f"日期: {target_date.strftime('%Y/%m/%d')}\n\n"
                    f"以下 VIP 仍未交心得：\n{list_names}\n\n"
                    f"{reminder_text_ending}"
                )
                try:
                    line_bot_api.push_message(group_id, TextSendMessage(text=msg))
                    print(f"Sent reminder to {group_id}", file=sys.stderr)
                except LineBotApiError as e:
                    print(f"PUSH ERROR {group_id}: {e}", file=sys.stderr)

    except Exception as e:
        print(f"SCHEDULER FATAL ERROR: {e}", file=sys.stderr)
    finally:
        conn.close()
    print("--- Scheduler check finished ---", file=sys.stderr)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    # 允許從 CLI 指定要檢查前幾天的回報
    parser.add_argument('--days-ago', type=int, default=1) 
    args = parser.parse_args()
    check_and_send_reminders(args.days_ago)