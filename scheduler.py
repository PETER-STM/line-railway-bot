import os
import sys
import re
from datetime import datetime, timedelta
import psycopg2
import argparse
from linebot import LineBotApi
from linebot.exceptions import LineBotApiError
from linebot.models import TextSendMessage

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

# 姓名正規化 (與 app.py 一致)
def normalize_name(name):
    return re.sub(r'^\s*[（(\[【][^()\[\]]{1,10}[)）\]】]\s*', '', name).strip()

def get_db():
    try:
        return psycopg2.connect(DB_URL, sslmode='require')
    except:
        return None

def check_reminders(days_ago=1):
    conn = get_db()
    if not conn: return

    try:
        cur = conn.cursor()
        # 設定日期 (UTC+8)
        now_tst = datetime.utcnow() + timedelta(hours=8)
        target_date = (now_tst - timedelta(days=days_ago)).date()
        target_str = target_date.strftime('%Y.%m.%d')
        
        day_label = "昨日" if days_ago == 1 else "今日"
        ending = "大家快來補交吧～\n不要逼系統變成奧客催款模式 😌" if days_ago == 1 else "提醒各位，記得在期限內提交心得喔！💪"

        print(f"--- Checking {target_str} ({day_label}) ---", file=sys.stderr)

        # 取得所有群組
        cur.execute("SELECT DISTINCT group_id FROM group_vips")
        groups = [r[0] for r in cur.fetchall()]

        for gid in groups:
            if gid in EXCLUDE_IDS: continue

            # 應交名單 (正規化後)
            cur.execute("SELECT normalized_name FROM group_vips WHERE group_id = %s", (gid,))
            all_norm = {row[0] for row in cur.fetchall()}

            # 已交名單 (正規化後)
            cur.execute("SELECT normalized_name FROM reports WHERE group_id = %s AND report_date = %s", (gid, target_date))
            done_norm = {row[0] for row in cur.fetchall()}

            # 找出未交 (比對正規化名稱)
            missing_norm = sorted(list(all_norm - done_norm))

            if missing_norm:
                # 為了顯示友善，我們嘗試找回原始名稱 (可選，或直接顯示正規化名稱)
                # 這裡簡單直接顯示正規化名稱，通常足夠辨識
                names = "\n".join([f"- {n}" for n in missing_norm])
                msg = (
                    f"📢 心得分享催繳大隊報到 📢\n"
                    f"日期: {target_str} ({day_label})\n\n"
                    f"以下 VIP 仍未交心得：\n{names}\n\n"
                    f"{ending}"
                )
                try:
                    line_bot_api.push_message(gid, TextSendMessage(text=msg))
                    print(f"Sent reminder to {gid}", file=sys.stderr)
                except:
                    print(f"Push failed for {gid}", file=sys.stderr)
    finally:
        conn.close()
    print("--- Finished ---", file=sys.stderr)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--days-ago', type=int, default=1)
    args = parser.parse_args()
    check_reminders(args.days_ago)


