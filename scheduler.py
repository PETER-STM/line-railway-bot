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

line_bot_api = LineBotApi(LINE_TOKEN)

def get_db():
    try:
        return psycopg2.connect(DB_URL, sslmode='require')
    except Exception as e:
        print(f"DB Error: {e}", file=sys.stderr)
        return None

def check_reminders(days_ago=0):
    conn = get_db()
    if not conn: return

    try:
        cur = conn.cursor()
        
        # 1. 計算日期 (UTC+8)
        now_tst = datetime.utcnow() + timedelta(hours=8)
        target_date = (now_tst - timedelta(days=days_ago)).date()
        target_str = target_date.strftime('%Y.%m.%d')
        
        day_label = "今日" if days_ago == 0 else "昨日"
        ending_msg = "請盡快完成心得回報！💪" if days_ago == 0 else "大家快來補交吧～\n不要逼系統變成奧客催款模式 😌"

        print(f"--- Checking for Date: {target_str} ({day_label}) ---", file=sys.stderr)

        cur.execute("SELECT DISTINCT group_id FROM group_vips")
        groups = [r[0] for r in cur.fetchall()]

        for gid in groups:
            if gid in EXCLUDE_IDS: continue

            # A. 取得該群組的應回報名單
            cur.execute("SELECT vip_name, normalized_name FROM group_vips WHERE group_id = %s", (gid,))
            rows = cur.fetchall()
            vip_map = {row[1]: row[0] for row in rows if row[1]} 

            if not vip_map: continue

            # B. 取得已回報名單
            cur.execute("""
                SELECT normalized_name FROM reports 
                WHERE group_id = %s AND report_date = %s
            """, (gid, target_date))
            submitted_norm = {r[0] for r in cur.fetchall()}

            # C. 比對缺交
            missing_norm = set(vip_map.keys()) - submitted_norm
            missing_names = sorted([vip_map[norm] for norm in missing_norm])

            if missing_names:
                names_str = "\n".join([f"- {n}" for n in missing_names])
                msg = (
                    f"📢 心得催繳大隊 ({target_str})\n"
                    f"----------------------\n"
                    f"尚未回報 ({len(missing_names)}人)：\n"
                    f"{names_str}\n\n"
                    f"{ending_msg}"
                )
                try:
                    line_bot_api.push_message(gid, TextSendMessage(text=msg))
                    print(f"✅ Sent reminder to {gid}", file=sys.stderr)
                except LineBotApiError as e:
                    print(f"❌ Push failed for {gid}: {e}", file=sys.stderr)

    finally:
        conn.close()

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--days-ago', type=int, default=0)
    args = parser.parse_args()
    check_reminders(args.days_ago)