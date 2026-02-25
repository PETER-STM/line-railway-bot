import sys
import re
import threading
import pytz
import os
import random 
from datetime import datetime, timedelta
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from apscheduler.schedulers.background import BackgroundScheduler 
from apscheduler.triggers.cron import CronTrigger
from config import Config
from database import init_db, get_db
import ai_service
import tasks as task_scheduler
import utils 

app = Flask(__name__)

# 初始化資料庫結構
init_db()

def check_and_update_schema():
    """
    🔥 自動演化：確保資料庫欄位與最新 Agent 架構對齊
    """
    print("🧬 檢查資料庫演化狀態...", file=sys.stderr)
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                # 檢查 group_vips 表格
                cols_to_add = [
                    ('meta_patterns', 'TEXT DEFAULT \'\''),
                    ('diagnosis', 'TEXT DEFAULT \'\''),
                    ('last_tactic', 'TEXT DEFAULT \'\''),
                    ('cognitive_tier', 'TEXT DEFAULT \'L1\''),
                    ('tier_confidence', 'FLOAT DEFAULT 0.0')
                ]
                for col, dtype in cols_to_add:
                    cur.execute(f"SELECT column_name FROM information_schema.columns WHERE table_name='group_vips' AND column_name='{col}'")
                    if not cur.fetchone():
                        cur.execute(f"ALTER TABLE group_vips ADD COLUMN {col} {dtype}")
                        print(f"✅ [Schema Update] Added {col} to group_vips", file=sys.stderr)

                # 檢查 reports 表格
                report_cols = [
                    ('diagnosis', 'TEXT DEFAULT \'\''),
                    ('score', 'INT DEFAULT 0'),
                    ('is_fragile', 'BOOLEAN DEFAULT FALSE'),
                    ('cognitive_score', 'INT DEFAULT 0'),
                    ('is_fake', 'BOOLEAN DEFAULT FALSE'),
                    ('distortion', 'TEXT DEFAULT \'\'')
                ]
                for col, dtype in report_cols:
                    cur.execute(f"SELECT column_name FROM information_schema.columns WHERE table_name='reports' AND column_name='{col}'")
                    if not cur.fetchone():
                        cur.execute(f"ALTER TABLE reports ADD COLUMN {col} {dtype}")
                        print(f"✅ [Schema Update] Added {col} to reports", file=sys.stderr)
                        
                # 建立貝氏推論戰術統計表
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS mab_stats (
                        id SERIAL PRIMARY KEY,
                        normalized_name VARCHAR(50) NOT NULL,
                        tactic_key VARCHAR(50) NOT NULL,
                        uses INT DEFAULT 0,
                        successes INT DEFAULT 0,
                        failures INT DEFAULT 0,
                        UNIQUE(normalized_name, tactic_key)
                    )
                """)
            conn.commit()
    except Exception as e:
        print(f"⚠️ Schema Update Check Failed: {e}", file=sys.stderr)

# 執行自動遷移
check_and_update_schema()

line_bot_api = LineBotApi(Config.LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(Config.LINE_CHANNEL_SECRET)

# 解析日報標題的 Regex
REPORT_HEADER_REGEX = re.compile(r'^(\d{4}[./-]\d{1,2}[./-]\d{1,2})\s*([^\n]+)', re.UNICODE)

# 👇 這是唯一新增的段落，用來應付 Railway 的健康檢查 👇
@app.route("/", methods=['GET'])
def health_check():
    return "OK V32.10 Matrix Agent Running", 200

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    except Exception as e:
        print(f"Callback Error: {e}", file=sys.stderr)
        abort(500)
    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    msg = event.message.text.strip()
    group_id = event.source.group_id if hasattr(event.source, 'group_id') else event.source.user_id

    # --- 1. 指令模式 ---
    if msg == "阿摩切換全開":
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO group_configs (group_id, ai_mode, mode_type) VALUES (%s, TRUE, 'full') ON CONFLICT (group_id) DO UPDATE SET ai_mode = TRUE, mode_type = 'full'", (group_id,))
            conn.commit()
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚙️ 模式：full (V32.10 矩陣搜救啟動)"))
        return
    elif msg == "阿摩切換精簡":
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO group_configs (group_id, ai_mode, mode_type) VALUES (%s, TRUE, 'simple') ON CONFLICT (group_id) DO UPDATE SET ai_mode = TRUE, mode_type = 'simple'", (group_id,))
            conn.commit()
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚙️ 模式：simple (僅紀錄)"))
        return

    # 檢查群組設定
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT ai_mode, mode_type FROM group_configs WHERE group_id=%s", (group_id,))
            row = cur.fetchone()
            ai_mode, mode_type = row if row else (False, 'simple')

    if not ai_mode: return

    # --- 2. 統計指令 (🔥 資本家收割排版與日期壓縮) ---
    if msg.startswith("統計缺交"):
        parts = msg.split()
        if len(parts) >= 3:
            try:
                s_date = datetime.strptime(parts[1], '%Y-%m-%d').date()
                e_date = datetime.strptime(parts[2], '%Y-%m-%d').date()
            except ValueError:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text="❌ 日期格式錯誤，請使用 YYYY-MM-DD"))
                return
                
            missing_report, completed_list, _ = utils.calculate_missing_stats(group_id, s_date, e_date)
            
            # 核心：連續日期壓縮演算法 (將 2/2, 2/3, 2/4 壓縮成 2/2-2/4)
            def compress_dates(date_strs):
                if not date_strs: return ""
                dates = sorted([datetime.strptime(d, '%Y-%m-%d').date() for d in date_strs])
                ranges = []
                start = dates[0]
                prev = dates[0]
                
                def fmt(d): return f"{d.month}/{d.day}"

                for d in dates[1:]:
                    if (d - prev).days == 1:
                        prev = d
                    else:
                        if start == prev: ranges.append(fmt(start))
                        else: ranges.append(f"{fmt(start)}-{fmt(prev)}")
                        start = d
                        prev = d
                if start == prev: ranges.append(fmt(start))
                else: ranges.append(f"{fmt(start)}-{fmt(prev)}")
                return "、".join(ranges)

            # 標題日期格式：保留 02/02 雙位數格式
            s_fmt = s_date.strftime('%m/%d')
            e_fmt = e_date.strftime('%m/%d')
            
            reply_text = f"📊 缺交統計 ({s_fmt} ~ {e_fmt})\n━━━━━━━━━━━━━━\n"
            
            # 處理全勤名單
            if completed_list:
                reply_text += f"✅ **全勤戰士 ({len(completed_list)}人)**：\n"
                reply_text += "、".join(completed_list) + "\n"
                reply_text += "━━━━━━━━━━━━━━\n"
                
            total_points = 0
            # 處理缺交罰款名單
            if missing_report:
                for name, dates in missing_report.items():
                    missing_count = len(dates)
                    points = missing_count * 100
                    total_points += points
                    compressed = compress_dates(dates)
                    reply_text += f"❌ {name} (缺{missing_count}次): {compressed} | 💎 需扣 {points} 點\n"
                
                reply_text += "━━━━━━━━━━━━━━\n"
                reply_text += f"💰 本期公費點數總計：{total_points} 點\n"
                reply_text += "😈 阿摩提示：感謝各位的懶惰，讓我又賺了一筆。"
            else:
                reply_text += "🎉 太神啦！這段時間全員全勤！沒人可以罰。\n"
                reply_text += "😈 阿摩提示：可惡，這次沒賺到半毛錢。"

            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
        return

    # --- 3. 日報處理邏輯 ---
    match = REPORT_HEADER_REGEX.match(msg)
    if match:
        raw_date_str = match.group(1).replace('/', '-').replace('.', '-')
        raw_name = match.group(2).strip()
        norm_name = utils.normalize_name(raw_name)
        tw_tz = pytz.timezone('Asia/Taipei')
        today_date = datetime.now(tw_tz).date()
        
        try:
            report_date = datetime.strptime(raw_date_str, '%Y-%m-%d').date()
        except: return
        if report_date > today_date: return

        insight = utils.extract_insight(msg)
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO reports (group_id, reporter_name, normalized_name, report_date, report_content) 
                    VALUES (%s, %s, %s, %s, %s) ON CONFLICT (group_id, normalized_name, report_date) 
                    DO UPDATE SET report_content = EXCLUDED.report_content
                    RETURNING id
                """, (group_id, raw_name, norm_name, report_date, msg))
                if not cur.fetchone(): return

                cur.execute("SELECT last_report_date, current_streak, meta_patterns, diagnosis FROM group_vips WHERE group_id=%s AND normalized_name=%s", (group_id, norm_name))
                vip_row = cur.fetchone()
                if vip_row:
                    last_date, streak, meta_patterns, diagnosis = vip_row
                    new_streak = streak + 1 if last_date == report_date - timedelta(days=1) else (streak if last_date >= report_date else 1)
                else:
                    meta_patterns, diagnosis, new_streak = '', '', 1

                cur.execute("""
                    INSERT INTO group_vips (group_id, vip_name, normalized_name, last_report_date, current_streak)
                    VALUES (%s, %s, %s, %s, %s) ON CONFLICT (group_id, normalized_name) 
                    DO UPDATE SET last_report_date = GREATEST(group_vips.last_report_date, EXCLUDED.last_report_date), current_streak = EXCLUDED.current_streak
                """, (group_id, raw_name, norm_name, report_date, new_streak))
            conn.commit()

        if mode_type == 'full':
            ai_resp = ai_service.generate_ai_reply("report_success", full_report=msg, name=norm_name, personality=meta_patterns, last_diagnosis=diagnosis, group_id=group_id, normalized_name=norm_name)
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute("UPDATE reports SET diagnosis = %s, score = %s WHERE group_id=%s AND normalized_name=%s AND report_date=%s", (ai_resp.get('diagnosis',''), ai_resp.get('score',0), group_id, norm_name, report_date))
                    cur.execute("UPDATE group_vips SET diagnosis = %s WHERE group_id=%s AND normalized_name=%s", (ai_resp.get('diagnosis',''), group_id, norm_name))
                conn.commit()
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=ai_resp['text']))

# --- 4. 排程系統 ---
tw_tz = pytz.timezone('Asia/Taipei')
scheduler = BackgroundScheduler(timezone=tw_tz)
scheduler.add_job(task_scheduler.check_reminders, CronTrigger(day_of_week='sat', hour=4, minute=0), args=['weekday_check'], id='weekday_check', replace_existing=True)
scheduler.add_job(task_scheduler.check_reminders, CronTrigger(day_of_week='mon', hour=4, minute=0), args=['weekend_check'], id='weekend_check', replace_existing=True)
scheduler.add_job(task_scheduler.check_curfew_sweeper, CronTrigger(hour=23, minute=0), id='curfew_sweep', replace_existing=True)
scheduler.add_job(task_scheduler.run_system_evolution, CronTrigger(day_of_week='sun', hour=2, minute=0), id='system_evolution', replace_existing=True)
scheduler.start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=Config.PORT)