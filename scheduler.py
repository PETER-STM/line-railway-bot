import os
import sys
import re
from datetime import datetime, timedelta
import psycopg2
import argparse 
from linebot import LineBotApi
from linebot.exceptions import LineBotApiError
from linebot.models import TextSendMessage

# --- 設定 ---
LINE_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
DB_URL = os.environ.get('DATABASE_URL')
EXCLUDE_IDS = set(os.environ.get('EXCLUDE_GROUP_IDS', '').split(','))

if not LINE_TOKEN or not DB_URL:
    print("FATAL: Missing env vars.", file=sys.stderr)
    sys.exit(1)

try:
    line_bot_api = LineBotApi(LINE_TOKEN)
except:
    sys.exit(1)

def normalize_name(name):
    return re.sub(r'^\s*[（(\[【][^()\[\]]{1,10}[)）\]】]\s*', '', name).strip()

def get_db():
    try:
        return psycopg2.connect(DB_URL, sslmode='require')
    except:
        return None

def check_reminders(days_ago=1):
    """
    days_ago=1: 檢查昨天 (補交提醒)
    days_ago=0: 檢查今天 (當日提醒)
    """
    conn = get_db()
    if not conn: return

    try:
        cur = conn.cursor()
        # 1. 檢查全域暫停
        cur.execute("SELECT value FROM settings WHERE key = 'is_paused'")
        res = cur.fetchone()
        if res and res[0] == 'true':
            print("INFO: Scheduler is PAUSED globally.", file=sys.stderr)
            return

        # 計算檢查目標日期 (UTC+8)
        now_tst = datetime.utcnow() + timedelta(hours=8)
        target_date = (now_tst - timedelta(days=days_ago)).date()
        target_str = target_date.strftime('%Y.%m.%d')
        
        day_label = "昨日" if days_ago == 1 else "今日"
        ending_text = "大家快來補交吧～\n不要逼系統變成奧客催款模式 😌" if days_ago == 1 else "提醒各位，記得在期限內提交心得喔！💪"

        print(f"--- Checking reports for {target_str} ({day_label}) ---", file=sys.stderr)

        cur.execute("SELECT DISTINCT group_id FROM reporters")
        groups = [r[0] for r in cur.fetchall()]

        for gid in groups:
            if gid in EXCLUDE_IDS: continue

            # 取得應回報名單 (原始)
            cur.execute("SELECT reporter_name FROM reporters WHERE group_id = %s", (gid,))
            all_raw = [r[0] for r in cur.fetchall()]
            # 正規化名單 (應交)
            all_norm = {normalize_name(n) for n in all_raw}

            # 取得已回報名單 (原始)
            cur.execute("SELECT reporter_name FROM reports WHERE group_id = %s AND report_date = %s", (gid, target_date))
            done_raw = [r[0] for r in cur.fetchall()]
            # 正規化名單 (已交)
            done_norm = {normalize_name(n) for n in done_raw}

            # 找出未交 (正規化後比對)
            missing = sorted(list(all_norm - done_norm))

            if missing:
                names = "\n".join([f"- {n}" for n in missing])
                msg = (
                    f"📢 心得分享催繳大隊報到 📢\n"
                    f"日期: {target_str} ({day_label})\n\n"
                    f"以下 VIP 仍未交心得：\n{names}\n\n"
                    f"{ending_text}"
                )
                try:
                    line_bot_api.push_message(gid, TextSendMessage(text=msg))
                    print(f"Sent reminder to {gid}", file=sys.stderr)
                except:
                    print(f"Failed to send to {gid}", file=sys.stderr)

    finally:
        conn.close()
    print("--- Check finished ---", file=sys.stderr)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--days-ago', type=int, default=1)
    args = parser.parse_args()
    
    check_reminders(args.days_ago)