import sys
import pytz
from datetime import datetime, timedelta
from linebot import LineBotApi
from linebot.models import TextSendMessage
from config import Config
from database import get_db
import utils 
import ai_service

line_bot_api = LineBotApi(Config.LINE_CHANNEL_ACCESS_TOKEN) if Config.LINE_CHANNEL_ACCESS_TOKEN else None

def check_reminders(mode):
    print(f"⏰ [Scheduler] Running check: {mode} at {datetime.now()}", file=sys.stderr)
    try:
        tw_tz = pytz.timezone('Asia/Taipei')
        now_tw = datetime.now(tw_tz)
        today = now_tw.date()

        if mode == 'weekday_check':
            end_date = today - timedelta(days=1)
            start_date = today - timedelta(days=5)
            period_label = "🔥 平日結算 (一~五) 🔥"
        elif mode == 'weekend_check':
            end_date = today - timedelta(days=1)
            start_date = today - timedelta(days=2)
            period_label = "🔥 假日結算 (六~日) 🔥"
        else:
            return

        date_range_str = f"{start_date.strftime('%m/%d')} ~ {end_date.strftime('%m/%d')}"

        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT DISTINCT group_id FROM group_vips")
                group_ids = [row[0] for row in cur.fetchall()]
                
                if Config.EXCLUDE_GROUP_IDS:
                    group_ids = [gid for gid in group_ids if gid not in Config.EXCLUDE_GROUP_IDS]

                for gid in group_ids:
                    group_name = utils.get_group_name(gid)
                    missing_data, status_msg = utils.calculate_missing_stats(gid, start_date, end_date)
                    
                    if missing_data:
                        total_fine = 0
                        detail_list = []
                        sinner_names = []
                        for name, dates in missing_data.items():
                            count = len(dates)
                            fine = count * 100
                            total_fine += fine
                            sinner_names.append(name)
                            detail_list.append(f"❌ {name} (缺{count}天): {', '.join(dates)} | 💎 需扣 {fine} 點")
                        
                        detail_text = "\n".join(detail_list)
                        sinners_str = "、".join(sinner_names)
                        ai_comment = ai_service.generate_ai_reply("fine_settlement", sinners=sinners_str, total_fine=total_fine)['text']

                        msg = (f"📊 **{group_name}**\n{period_label}\n📅 區間：{date_range_str}\n━━━━━━━━━━━━━━\n{detail_text}\n━━━━━━━━━━━━━━\n💰 本期公費點數總計：{total_fine} 點\n━━━━━━━━━━━━━━\n{ai_comment}")
                        if line_bot_api: line_bot_api.push_message(gid, TextSendMessage(text=msg))
                    else:
                        msg = (f"🎉 **{group_name}**\n{period_label}\n📅 區間：{date_range_str}\n━━━━━━━━━━━━━━\n✨ 全員全勤！太神啦！")
                        if line_bot_api: line_bot_api.push_message(gid, TextSendMessage(text=msg))
    except Exception as e:
        print(f"❌ Check Reminders Error: {e}", file=sys.stderr)