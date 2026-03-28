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
    """自動演化：確保資料庫欄位與最新 Agent 架構對齊"""
    print("🧬 檢查資料庫演化狀態...", file=sys.stderr)
    try:
        with get_db() as conn:
            conn.rollback()
            conn.autocommit = True
            with conn.cursor() as cur:
                cols_to_add = [
                    ('meta_patterns', 'TEXT DEFAULT \'\''),
                    ('diagnosis', 'TEXT DEFAULT \'\''),
                    ('last_tactic', 'TEXT DEFAULT \'\''),
                    ('cognitive_tier', 'TEXT DEFAULT \'L1\''),
                    ('tier_confidence', 'FLOAT DEFAULT 0.0')
                ]
                for col, dtype in cols_to_add:
                    try:
                        cur.execute(f"ALTER TABLE group_vips ADD COLUMN IF NOT EXISTS {col} {dtype}")
                    except Exception as e:
                        pass

                report_cols = [
                    ('diagnosis', 'TEXT DEFAULT \'\''),
                    ('score', 'INT DEFAULT 0'),
                    ('is_fragile', 'BOOLEAN DEFAULT FALSE'),
                    ('cognitive_score', 'INT DEFAULT 0'),
                    ('is_fake', 'BOOLEAN DEFAULT FALSE'),
                    ('distortion', 'TEXT DEFAULT \'\'')
                ]
                for col, dtype in report_cols:
                    try:
                        cur.execute(f"ALTER TABLE reports ADD COLUMN IF NOT EXISTS {col} {dtype}")
                    except Exception as e:
                        pass
                        
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
            print("✅ 演化檢查完成，系統準備就緒。", file=sys.stderr)
    except Exception as e:
        print(f"⚠️ Schema Update Check Failed: {e}", file=sys.stderr)

check_and_update_schema()

line_bot_api = LineBotApi(Config.LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(Config.LINE_CHANNEL_SECRET)
REPORT_HEADER_REGEX = re.compile(r'^(\d{4}[./-]\d{1,2}[./-]\d{1,2})\s*([^\n]+)', re.UNICODE)

@app.route("/", methods=['GET'])
def health_check():
    return "OK V43 Matrix Agent Running (Firewall Active)", 200

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

    # --- 指令模式 ---
    if msg == "阿摩切換全開":
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO group_configs (group_id, ai_mode, mode_type) 
                    VALUES (%s, TRUE, 'full') 
                    ON CONFLICT (group_id) DO UPDATE SET ai_mode = TRUE, mode_type = 'full'
                """, (group_id,))
            conn.commit()
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚙️ 模式：full (V43 矩陣搜救啟動)"))
        return
    elif msg == "阿摩切換精簡":
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO group_configs (group_id, ai_mode, mode_type) 
                    VALUES (%s, TRUE, 'simple') 
                    ON CONFLICT (group_id) DO UPDATE SET ai_mode = TRUE, mode_type = 'simple'
                """, (group_id,))
            conn.commit()
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚙️ 模式：simple (僅紀錄)"))
        return

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT ai_mode, mode_type FROM group_configs WHERE group_id=%s", (group_id,))
            row = cur.fetchone()
            ai_mode, mode_type = row if row else (False, 'simple')

    if not ai_mode:
        return

    # --- 統計缺交 ---
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
            
            def compress_dates(date_strs):
                if not date_strs: return ""
                dates = sorted([datetime.strptime(d, '%Y-%m-%d').date() for d in date_strs])
                ranges, start, prev = [], dates[0], dates[0]
                def fmt(d): return f"{d.month}/{d.day}"
                for d in dates[1:]:
                    if (d - prev).days == 1: prev = d
                    else:
                        ranges.append(fmt(start) if start == prev else f"{fmt(start)}-{fmt(prev)}")
                        start = prev = d
                ranges.append(fmt(start) if start == prev else f"{fmt(start)}-{fmt(prev)}")
                return "、".join(ranges)

            s_fmt, e_fmt = s_date.strftime('%m/%d'), e_date.strftime('%m/%d')
            reply_text = f"📊 缺交統計 ({s_fmt} ~ {e_fmt})\n━━━━━━━━━━━━━━\n"
            
            if completed_list:
                reply_text += f"✅ **全勤戰士 ({len(completed_list)}人)**：\n" + "、".join(completed_list) + "\n━━━━━━━━━━━━━━\n"
                
            total_points = 0
            if missing_report:
                for name, dates in missing_report.items():
                    missing_count = len(dates)
                    points = missing_count * 100
                    total_points += points
                    reply_text += f"❌ {name} (缺{missing_count}次): {compress_dates(dates)} | 💎 需扣 {points} 點\n"
                reply_text += f"━━━━━━━━━━━━━━\n💰 本期公費點數總計：{total_points} 點\n😈 阿摩提示：感謝各位的懶惰，讓我又賺了一筆。"
            else:
                reply_text += "🎉 太神啦！這段時間全員全勤！沒人可以罰。\n😈 阿摩提示：可惡，這次沒賺到半毛錢。"

            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
        return

    # --- 日報處理模組 ---
    match = REPORT_HEADER_REGEX.match(msg)
    if match:
        raw_date_str = match.group(1).replace('/', '-').replace('.', '-')
        raw_name = match.group(2).strip()
        norm_name = utils.normalize_name(raw_name)
        tw_tz = pytz.timezone('Asia/Taipei')
        today_date = datetime.now(tw_tz).date()
        
        try:
            report_date = datetime.strptime(raw_date_str, '%Y-%m-%d').date()
        except: 
            return
            
        if report_date > today_date: 
            return

        # 🛡️ 防火牆第一層：短期記憶防呆 (已應教練要求關閉，允許無限重刷與測試)
        # with get_db() as conn:
        #     with conn.cursor() as cur:
        #         cur.execute("SELECT report_content FROM reports WHERE group_id=%s AND normalized_name=%s AND report_date=%s", (group_id, norm_name, report_date))
        #         existing_report = cur.fetchone()
        #         if existing_report and existing_report[0].strip() == msg.strip():
        #             if mode_type == 'full':
        #                 line_bot_api.reply_message(event.reply_token, TextSendMessage(text="✅ 日報已收錄，無須重複上傳。"))
        #             return

        # 🛡️ 防火牆第二層：優先寫入資料庫
        insight = utils.extract_insight(msg)
        try:
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO reports (group_id, reporter_name, normalized_name, report_date, report_content) 
                        VALUES (%s, %s, %s, %s, %s) ON CONFLICT (group_id, normalized_name, report_date) 
                        DO UPDATE SET report_content = EXCLUDED.report_content
                        RETURNING id
                    """, (group_id, raw_name, norm_name, report_date, msg))
                    
                    if not cur.fetchone(): 
                        return

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
                
                # 🔥 極度關鍵：在這裡先行 Commit！
                conn.commit()
                
        except Exception as e:
            print(f"❌ 資料庫防護線崩潰: {e}", file=sys.stderr)
            return

        # 🛡️ 第三道牆：獨立的大腦沙盒
        if mode_type == 'full':
            try:
                ai_resp = ai_service.generate_ai_reply("report_success", full_report=msg, name=norm_name, personality=meta_patterns, last_diagnosis=diagnosis, group_id=group_id, normalized_name=norm_name)
                
                with get_db() as conn:
                    with conn.cursor() as cur:
                        cur.execute("""
                            UPDATE reports SET diagnosis = %s, score = %s 
                            WHERE group_id=%s AND normalized_name=%s AND report_date=%s
                        """, (ai_resp.get('diagnosis',''), ai_resp.get('score',0), group_id, norm_name, report_date))
                        
                        cur.execute("""
                            UPDATE group_vips SET diagnosis = %s 
                            WHERE group_id=%s AND normalized_name=%s
                        """, (ai_resp.get('diagnosis',''), group_id, norm_name))
                    conn.commit()
                    
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=ai_resp['text']))
                
            except Exception as ai_err:
                print(f"⚠️ AI 引擎發生錯誤，啟動備援回覆: {ai_err}", file=sys.stderr)
                
                # 🔥 修正版戰地醫護模式 (已移除容易誤判的「活著」)
                medical_keywords = ["醫院", "心臟", "生病", "回診", "吃藥", "崩潰", "看病", "掛急診"]
                if any(k in msg for k in medical_keywords):
                    fallback_msg = "✅ 日報已成功紀錄！\n\n🩹 **戰地醫護模式啟動**：\n阿摩偵測到您今日身心負荷較大，已自動關閉教練稽核機制。請將注意力放回自己的身體，健康第一，今晚好好休息。"
                else:
                    fallback_msg = "✅ 日報已成功紀錄！\n\n(阿摩大腦暫時連線異常，但您的全勤紀錄已安全保存。)"
                
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=fallback_msg))

# --- 排程系統 ---
tw_tz = pytz.timezone('Asia/Taipei')
scheduler = BackgroundScheduler(timezone=tw_tz)

scheduler.add_job(task_scheduler.check_reminders, CronTrigger(day_of_week='sat', hour=4, minute=0), args=['weekday_check'], id='weekday_check', replace_existing=True)
scheduler.add_job(task_scheduler.check_reminders, CronTrigger(day_of_week='mon', hour=4, minute=0), args=['weekend_check'], id='weekend_check', replace_existing=True)
scheduler.add_job(task_scheduler.check_curfew_sweeper, CronTrigger(hour=23, minute=0), id='curfew_sweep', replace_existing=True)
scheduler.add_job(task_scheduler.run_system_evolution, CronTrigger(day_of_week='sun', hour=2, minute=0), id='system_evolution', replace_existing=True)

scheduler.start()

if __name__ == "__main__":
    init_db()
    check_and_update_schema()
    app.run(host="0.0.0.0", port=Config.PORT)