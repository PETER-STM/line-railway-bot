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

def normalize_name(name):
    # 移除開頭被括號包裹的內容
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
        try:
            cur.execute("SELECT value FROM settings WHERE key = 'is_paused'")
            res = cur.fetchone()
            if res and res[0] == 'true':
                print("INFO: Scheduler PAUSED.", file=sys.stderr)
                return
        except: pass # 表格可能不存在，忽略

        # 計算日期 (UTC+8)
        now_tst = datetime.utcnow() + timedelta(hours=8)
        target_date = (now_tst - timedelta(days=days_ago)).date()
        target_str = target_date.strftime('%Y.%m.%d')
        
        day_label = "昨日" if days_ago == 1 else "今日"
        ending = "大家快來補交吧～\n不要逼系統變成奧客催款模式 😌" if days_ago == 1 else "提醒各位，記得在期限內提交心得喔！💪"

        print(f"--- Checking {target_str} ({day_label}) ---", file=sys.stderr)

        # 優先嘗試從 group_vips 獲取群組 ID，如果沒有則從 reporters 獲取
        # 這樣可以確保新舊資料庫結構都能運作
        try:
            cur.execute("SELECT DISTINCT group_id FROM group_vips")
        except psycopg2.errors.UndefinedTable:
            conn.rollback()
            cur.execute("SELECT DISTINCT group_id FROM reporters")
            
        groups = [r[0] for r in cur.fetchall()]

        for gid in groups:
            if gid in EXCLUDE_IDS: continue

            # 2. 取得應回報名單 (建立 正規化名 -> 原始名 的對照表)
            # 優先使用 group_vips
            try:
                cur.execute("SELECT vip_name FROM group_vips WHERE group_id = %s", (gid,))
                all_raw = [r[0] for r in cur.fetchall()]
            except:
                conn.rollback()
                cur.execute("SELECT reporter_name FROM reporters WHERE group_id = %s", (gid,))
                all_raw = [r[0] for r in cur.fetchall()]

            # 對照表：{ '浣熊': '(三) 浣熊', '邦妮': '(三) 邦妮' }
            # 這樣我們比對用 key，顯示用 value
            vip_map = {normalize_name(name): name for name in all_raw}
            
            if not vip_map: continue

            # 3. 取得已回報名單 (正規化)
            # 嘗試使用 normalized_name 欄位
            try:
                cur.execute("SELECT normalized_name FROM reports WHERE group_id = %s AND report_date = %s", (gid, target_date))
                submitted_norm = {r[0] for r in cur.fetchall()}
            except:
                conn.rollback()
                # 回退：手動正規化 reporter_name
                cur.execute("SELECT reporter_name FROM reports WHERE group_id = %s AND report_date = %s", (gid, target_date))
                submitted_norm = {normalize_name(r[0]) for r in cur.fetchall()}

            # 4. 找出未交 (比對正規化名稱)
            # 找出哪些 key (正規化名) 不在 submitted_norm 中
            missing_norm = set(vip_map.keys()) - submitted_norm
            
            # 5. 轉換回原始名稱用於顯示
            # 從 vip_map 中取出對應的原始名稱
            missing_original_names = sorted([vip_map[norm_name] for norm_name in missing_norm])

            if missing_original_names:
                names = "\n".join([f"- {n}" for n in missing_original_names])
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