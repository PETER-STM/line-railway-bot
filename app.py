import sys
import re
import threading
import pytz
import os
from datetime import datetime
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
init_db()

line_bot_api = LineBotApi(Config.LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(Config.LINE_CHANNEL_SECRET)

# 🔥 [Regex 終極修正版]
REPORT_REGEX = re.compile(r'^(\d{4}[./-]?\d{1,2}[./-]?\d{1,2})\s*(?:[(（][^)）\n]*[)）])?\s*(.+)', re.DOTALL)

def run_async_ai_task(ai_func, *args):
    try:
        with get_db() as conn:
            ai_func(conn, *args)
    except Exception as e:
        print(f"⚠️ Async Task Error: {e}", file=sys.stderr)

def process_report(gid, uid, date_str, raw_name_line, content):
    """
    [核心重構 - 智慧自癒 + 異步存檔版]
    1. 自動修復髒名字 (Auto-Healing)
    2. 分離 DB 寫入與 AI 生成 (Phase 1, 2, 3)
    """
    name = utils.normalize_name(raw_name_line)
    is_admin = uid in Config.ADMIN_USER_IDS
    
    try:
        # 1. 解析日期
        r_date = datetime.strptime(date_str.replace('.', '-').replace('/', '-'), '%Y-%m-%d').date()
        
        # [年份防呆]
        now = datetime.now()
        if now.month <= 3 and r_date.year == (now.year - 1) and r_date.month <= 3:
            r_date = r_date.replace(year=now.year)

        insight = utils.extract_insight(content)
        
        # 變數準備
        history_context = "（無歷史資料）"
        personality = ""
        tr_data = {}
        display_streak = 0

        # 🔥 Phase 1: 資料庫寫入 (快速交易 + 自動修復)
        with get_db() as conn:
            with conn.cursor() as cur:
                # 2. 檢查/建立使用者資料 (智慧搜尋)
                cur.execute("SELECT line_user_id, last_report_date, current_streak, normalized_name FROM group_vips WHERE group_id=%s AND normalized_name=%s", (gid, name))
                row = cur.fetchone()

                # 🌟【自動修復邏輯】找不到時，嘗試找髒名字 (Trim)
                if not row:
                    cur.execute("SELECT line_user_id, last_report_date, current_streak, normalized_name FROM group_vips WHERE group_id=%s AND TRIM(normalized_name)=%s", (gid, name))
                    row = cur.fetchone()
                    
                    if row:
                        dirty_name = row[3]
                        print(f"🧹 Auto-Cleaning: Fixing dirty name '{dirty_name}' to '{name}'", file=sys.stderr)
                        cur.execute("UPDATE group_vips SET normalized_name=%s WHERE group_id=%s AND normalized_name=%s", (name, gid, dirty_name))
                        cur.execute("UPDATE reports SET normalized_name=%s WHERE group_id=%s AND normalized_name=%s", (name, gid, dirty_name))

                last_date = None
                current_streak = 0

                if row:
                    if row[0] and row[0] != uid and not is_admin:
                        return f"⛔ 身份驗證失敗：{name} 已被其他裝置綁定。\n(請管理員使用「阿摩解鎖 {name}」)"
                    last_date = row[1]
                    current_streak = row[2] if row[2] else 0
                    
                    if not row[0]:
                        cur.execute("UPDATE group_vips SET line_user_id=%s WHERE group_id=%s AND normalized_name=%s", (uid, gid, name))
                else:
                    cur.execute("INSERT INTO group_vips (group_id, vip_name, normalized_name, line_user_id, current_streak) VALUES (%s,%s,%s,%s, 0)", (gid, name, name, uid))
                    current_streak = 0

                # 3. [連擊邏輯]
                new_streak = current_streak
                update_vip_stats = False

                if last_date is None:
                    new_streak = 1
                    update_vip_stats = True
                elif r_date > last_date:
                    diff = (r_date - last_date).days
                    if diff == 1:
                        new_streak += 1
                    elif diff > 1:
                        new_streak = 1
                    update_vip_stats = True
                else:
                    update_vip_stats = False
                
                if update_vip_stats:
                    cur.execute("UPDATE group_vips SET last_report_date=%s, current_streak=%s WHERE group_id=%s AND normalized_name=%s", (r_date, new_streak, gid, name))
                    display_streak = new_streak
                else:
                    display_streak = current_streak

                # 4. 寫入日報 (Upsert)
                cur.execute("SELECT report_content FROM reports WHERE group_id=%s AND report_date=%s AND normalized_name=%s", (gid, r_date, name))
                existing_report = cur.fetchone()

                if existing_report:
                    if content.strip() not in existing_report[0]:
                        cur.execute("UPDATE reports SET report_content = report_content || %s WHERE group_id=%s AND report_date=%s AND normalized_name=%s", ("\n\n[補] " + content, gid, r_date, name))
                else:
                    cur.execute("INSERT INTO reports (group_id, reporter_name, normalized_name, report_date, report_content) VALUES (%s,%s,%s,%s,%s)", (gid, name, name, r_date, content))

                # 5. 抓取資料給 AI
                cur.execute("""
                    SELECT report_date, report_content 
                    FROM reports 
                    WHERE group_id=%s AND normalized_name=%s 
                    AND report_date < %s 
                    ORDER BY report_date DESC LIMIT 5
                """, (gid, name, r_date))
                history_rows = cur.fetchall()
                if history_rows:
                    history_context = "\n".join([f"📅 {hr[0]}: {utils.extract_insight(hr[1])}" for hr in history_rows])

                cur.execute("SELECT personality, tr_tag, tr_strategy, tr_incantation, cognitive_tier FROM group_vips WHERE group_id=%s AND normalized_name=%s", (gid, name))
                v = cur.fetchone()
                if v:
                    personality = v[0] or ""
                    tr_data = {'incantation': v[3], 'cognitive_tier': v[4] or 'L1'}
        
        # ❄️ Phase 2: AI 生成 (DB 連線已釋放，不卡頓)
        ai_result = ai_service.generate_ai_reply(
            "report_success", 
            name=name, 
            streak=display_streak, 
            insight=insight,
            full_report=content,
            history_context=history_context, 
            personality=personality,
            tr_data=tr_data
        )

        # 🔥 Phase 3: 補寫入 AI 評分
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE reports 
                    SET cognitive_score=%s, is_fake=%s, is_fragile=%s, distortion=%s
                    WHERE group_id=%s AND normalized_name=%s AND report_date=%s
                """, (ai_result.get('score', 0), ai_result.get('is_fake', False), ai_result.get('is_fragile', False), ai_result.get('distortion', ''), gid, name, r_date))

        # 啟動非同步 AI 分析任務
        threading.Thread(target=run_async_ai_task, args=(ai_service.analyze_peer_interaction, gid, name, content)).start()
        threading.Thread(target=run_async_ai_task, args=(ai_service.evaluate_and_evolve_strategy, gid, name, insight, tr_data, display_streak)).start()
        threading.Thread(target=run_async_ai_task, args=(ai_service.analyze_and_update_personality, gid, name, content, personality)).start()
        
        return f"【 📝 回報確認 】\n📅 {r_date}｜👤 {name}\n✨ 連續 {display_streak} 天\n━━━━━━━━━━━━━━\n{ai_result.get('text')}"

    except Exception as e:
        print(f"❌ Process Report Error: {e}", file=sys.stderr)
        return f"💥 系統忙線：{e}"

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@app.route("/")
def home():
    return "Amor Bot V16.30 (Final Stable)", 200

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    msg = event.message.text.strip()
    uid = event.source.user_id
    gid = event.source.group_id if event.source.type == 'group' else None
    is_admin = uid in Config.ADMIN_USER_IDS

    if not gid and not is_admin:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="🤖 阿摩目前只服務群組喔！請把我加入群組。"))
        return

    # 1. 日報處理
    match = REPORT_REGEX.match(msg)
    if match:
        if not gid and not is_admin: return 
        raw_name_line = match.group(2).split('\n')[0].strip()
        reply_text = process_report(gid, uid, match.group(1), raw_name_line, msg)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
        return

    # 2. 統計缺交 (年份防呆修正)
    if msg.startswith("統計缺交") and gid:
        try:
            parts = msg.split()
            if len(parts) != 3: raise ValueError
            _, start_str, end_str = parts[0], parts[1], parts[2]
            
            start_date = datetime.strptime(start_str.replace('.', '-').replace('/', '-'), '%Y-%m-%d').date()
            end_date = datetime.strptime(end_str.replace('.', '-').replace('/', '-'), '%Y-%m-%d').date()
            
            # 🔥 [修正] 年份自動防呆
            now = datetime.now()
            if now.month <= 3 and start_date.year == (now.year - 1) and start_date.month <= 3:
                start_date = start_date.replace(year=now.year)  
            if now.month <= 3 and end_date.year == (now.year - 1) and end_date.month <= 3:
                end_date = end_date.replace(year=now.year)
            
            # 呼叫 utils
            missing_data, status_msg = utils.calculate_missing_stats(gid, start_date, end_date)
            
            if missing_data:
                total_fine = 0
                detail_msg = ""
                for name, dates in missing_data.items():
                    fine = len(dates) * 100
                    total_fine += fine
                    detail_msg += f"❌ {name} (缺{len(dates)}天): {', '.join(dates)} | 💎 需扣 {fine} 點\n"
                final_reply = f"📊 缺交統計 ({start_date} ~ {end_date})\n━━━━━━━━━━━━━━\n{detail_msg}━━━━━━━━━━━━━━\n💰 本期公費點數總計：{total_fine} 點\n😈 阿摩提示：感謝各位的懶惰，讓我又賺了一筆。"
            else:
                final_reply = status_msg
            
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=final_reply))
        except ValueError:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ 格式錯誤！\n正確格式：`統計缺交 2026-01-01 2026-01-05`"))
        return

    # 3. 其他功能
    if msg.lower() in ["幫助", "阿摩"]:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=Config.HELP_MENU_FULL if is_admin else Config.HELP_MENU_GROWTH))
        return

    if msg.startswith("阿摩切換") and gid:
        mode = 'full' if "全開" in msg else 'growth' if "成長" in msg else 'simple'
        with get_db() as conn:
            with conn.cursor() as cur: 
                cur.execute("INSERT INTO group_configs (group_id, mode_type, ai_mode) VALUES (%s, %s, TRUE) ON CONFLICT (group_id) DO UPDATE SET mode_type = %s, ai_mode = TRUE", (gid, mode, mode))
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"⚙️ 模式切換為：{mode}"))
        return
    
    if is_admin and msg == "阿摩補跑排程":
        threading.Thread(target=task_scheduler.check_reminders, args=('weekday_check',)).start()
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="🚀 強制啟動：【平日結算】檢查中..."))
        return

    if is_admin and msg.startswith("阿摩解鎖"):
        target_name = utils.normalize_name(msg.replace("阿摩解鎖", ""))
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE group_vips SET line_user_id = NULL WHERE group_id=%s AND normalized_name=%s", (gid, target_name))
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"🔓 已解除「{target_name}」鎖定"))
        return
    
    if is_admin and msg.startswith("阿摩刪除成員"):
        target_name = msg.replace("阿摩刪除成員", "").strip()
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM reports WHERE group_id=%s AND reporter_name LIKE %s", (gid, f"%{target_name}%"))
                cur.execute("DELETE FROM group_vips WHERE group_id=%s AND vip_name LIKE %s", (gid, f"%{target_name}%"))
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"🗑️ 已刪除成員「{target_name}」"))
        return

    if is_admin and msg.startswith("阿摩後台列表"):
         with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT group_id, vip_name FROM group_vips ORDER BY group_id")
                rows, g_dict = cur.fetchall(), {}
                for r in rows:
                    if r[0] not in g_dict: g_dict[r[0]] = []
                    if len(g_dict[r[0]]) < 5: g_dict[r[0]].append(r[1])
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text="🌍 阿摩駐點\n" + "\n".join([f"\n📂 {utils.get_group_name(k)} ({k})\n成員: {','.join(v)}..." for k, v in g_dict.items()])))
         return

    if msg == "阿摩查標籤":
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT personality, cognitive_tier FROM group_vips WHERE group_id=%s AND line_user_id=%s", (gid, uid))
                row = cur.fetchone()
                tags = row[0] if row and row[0] else "無標籤"
                tier = row[1] if row and row[1] else "L1"
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"👤 您的側寫：\n標籤：{tags}\n等級：{tier}"))
        return

    if msg.startswith("阿摩教我"):
        threading.Thread(target=lambda: line_bot_api.reply_message(
            event.reply_token, 
            TextSendMessage(text=ai_service.generate_ai_reply("sales_coach", question=msg[4:].strip(), name="夥伴")['text'])
        )).start()
        return

    if "阿摩" in msg:
        threading.Thread(target=lambda: line_bot_api.reply_message(
            event.reply_token, 
            TextSendMessage(text=ai_service.generate_ai_reply("chat_mode", user_msg=msg)['text'])
        )).start()

tw_tz = pytz.timezone('Asia/Taipei')
scheduler = BackgroundScheduler(timezone=tw_tz)
scheduler.add_job(task_scheduler.check_reminders, CronTrigger(day_of_week='sat', hour=4, minute=0), args=['weekday_check'], id='weekday_check', replace_existing=True)
scheduler.add_job(task_scheduler.check_reminders, CronTrigger(day_of_week='mon', hour=4, minute=0), args=['weekend_check'], id='weekend_check', replace_existing=True)

if not scheduler.running:
    scheduler.start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)