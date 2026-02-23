import sys
import pytz
from datetime import datetime, timedelta
from linebot import LineBotApi
from linebot.models import TextSendMessage
from config import Config
from database import get_db
import utils 
import ai_service
from evolution_core import EvolutionManager

line_bot_api = LineBotApi(Config.LINE_CHANNEL_ACCESS_TOKEN) if Config.LINE_CHANNEL_ACCESS_TOKEN else None

def check_reminders(mode):
    # (保留原有的統計邏輯，不做變動以確保穩定)
    print(f"⏰ [Scheduler] Running check: {mode}", file=sys.stderr)
    try:
        tw_tz = pytz.timezone('Asia/Taipei')
        today = datetime.now(tw_tz).date()
        if mode == 'weekday_check':
            end_date = today - timedelta(days=1); start_date = today - timedelta(days=5)
            period_label = "🔥 平日結算 (一~五) 🔥"
        elif mode == 'weekend_check':
            end_date = today - timedelta(days=1); start_date = today - timedelta(days=2)
            period_label = "🔥 假日結算 (六~日) 🔥"
        else: return

        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT DISTINCT group_id FROM group_vips")
                for (gid,) in cur.fetchall():
                    if Config.EXCLUDE_GROUP_IDS and gid in Config.EXCLUDE_GROUP_IDS: continue
                    
                    missing_data, completed_list, status_msg = utils.calculate_missing_stats(gid, start_date, end_date)
                    if missing_data:
                        sinners = []; total_fine = 0
                        for n, d in missing_data.items():
                            total_fine += len(d)*100; sinners.append(n)
                        ai_cmt = ai_service.generate_ai_reply("fine_settlement", sinners=",".join(sinners), total_fine=total_fine)['text']
                        msg = f"📊 **統計**\n{period_label}\n💰 罰金：{total_fine}\n{ai_cmt}"
                        if line_bot_api: line_bot_api.push_message(gid, TextSendMessage(text=msg))
                    elif completed_list and line_bot_api:
                        line_bot_api.push_message(gid, TextSendMessage(text=f"🎉 {period_label} 全員全勤！"))
    except Exception as e: print(f"❌ Reminder Error: {e}", file=sys.stderr)

# 🔥 V23.0 靜默演化
def run_system_evolution():
    print(f"🧬 [Scheduler] Silent Evolution...", file=sys.stderr)
    try:
        manager = EvolutionManager()
        result_msg = manager.run_evolution()
        print(f"✅ Evolution: {result_msg}", file=sys.stderr)
    except Exception as e: print(f"❌ Evolution Failed: {e}", file=sys.stderr)

# 🔥 V23.0 宵禁掃蕩
def check_curfew_sweeper():
    print(f"⏰ [Scheduler] Curfew Sweep...", file=sys.stderr)
    try:
        tw_tz = pytz.timezone('Asia/Taipei')
        today_str = datetime.now(tw_tz).strftime('%Y-%m-%d')
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT group_id FROM group_configs WHERE ai_mode = TRUE")
                for (gid,) in cur.fetchall():
                    cur.execute("SELECT normalized_name, last_report_date, vip_name FROM group_vips WHERE group_id=%s", (gid,))
                    missing = [vip or norm for norm, last, vip in cur.fetchall() if str(last) != today_str]
                    if missing and line_bot_api:
                        line_bot_api.push_message(gid, TextSendMessage(text=ai_service.generate_ghost_sweeper("、".join(missing))))
    except Exception as e: print(f"❌ Curfew Error: {e}", file=sys.stderr)