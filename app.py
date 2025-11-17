import os
import sys
import re
from datetime import datetime, timedelta
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError, LineBotApiError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, SourceGroup, SourceRoom, SourceUser
import psycopg2

# --- 環境變數設定 ---
# 確保這些變數存在於 Railway 環境變數中
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
DATABASE_URL = os.environ.get('DATABASE_URL')

# --- 診斷程式碼 (確認環境變數載入成功) ---
try:
    if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_CHANNEL_SECRET or not DATABASE_URL:
        print("ERROR: Missing required environment variables!T", file=sys.stderr)
    else:
        # 打印這些變數的長度 (確認它們不為空)
        print(f"LINE_SECRET length: {len(LINE_CHANNEL_SECRET)}", file=sys.stderr)
        print(f"LINE_TOKEN length: {len(LINE_CHANNEL_ACCESS_TOKEN)}", file=sys.stderr)
        print(f"DB_URL length: {len(DATABASE_URL)}", file=sys.stderr)
except Exception as e:
    print(f"FATAL INIT ERROR during variable check: {e}", file=sys.stderr)
# --- 診斷程式碼結束 ---

if not LINE_CHANNEL_ACCESS_TOKEN:
    sys.exit("LINE_CHANNEL_ACCESS_TOKEN is missing!")
if not LINE_CHANNEL_SECRET:
    sys.exit("LINE_CHANNEL_SECRET is missing!")

app = Flask(__name__)

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# --- 資料庫連線函式 ---
def get_db_connection():
    try:
        # 修正：強制使用 SSL mode='require'，確保連線穩定
        conn = psycopg2.connect(DATABASE_URL, sslmode='require')
        return conn
    except Exception as e:
        print(f"DATABASE CONNECTION ERROR: {e}", file=sys.stderr)
        return None

# --- 資料庫操作：新增回報人 (情緒價值優化) ---
def add_reporter(group_id, reporter_name):
    conn = get_db_connection()
    if conn is None:
        return "Database connection failed."

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT group_id FROM group_reporters WHERE group_id = %s AND reporter_name = %s;", (group_id, reporter_name))
            if cur.fetchone():
                return f"😉 哎呀，**{reporter_name}** 已經在名單中囉！感謝您的熱情！🔥"

            cur.execute("INSERT INTO group_reporters (group_id, reporter_name) VALUES (%s, %s);", (group_id, reporter_name))
            conn.commit()
            return f"🥳 太棒了！歡迎 **{reporter_name}** 加入回報名單！從今天起一起努力吧！💪"
    except Exception as e:
        conn.rollback()
        print(f"DB ERROR (add_reporter): {e}", file=sys.stderr)
        return f"🚨 資料庫操作失敗: {e}"
    finally:
        conn.close()

# --- 資料庫操作：刪除回報人 (情緒價值優化) ---
def delete_reporter(group_id, reporter_name):
    conn = get_db_connection()
    if conn is None:
        return "Database connection failed."

    try:
        with conn.cursor() as cur:
            # 檢查是否存在
            cur.execute("SELECT group_id FROM group_reporters WHERE group_id = %s AND reporter_name = %s;", (group_id, reporter_name))
            if not cur.fetchone():
                return f"🤔 咦？我查了一下，**{reporter_name}** 不在回報人名單上耶。是不是名字打錯了呢？請再檢查一下喔！"

            # 刪除回報人
            cur.execute("DELETE FROM group_reporters WHERE group_id = %s AND reporter_name = %s;", (group_id, reporter_name))
            
            # 順便刪除該回報人的歷史記錄 (reports 表欄位使用 source_id)
            cur.execute("DELETE FROM reports WHERE source_id = %s AND name = %s;", (group_id, reporter_name))

            conn.commit()
            return f"👋 好的，我們已經跟 **{reporter_name}** 說掰掰了，資料庫也順利清空。管理名單完成！🧹"
    except Exception as e:
        conn.rollback()
        print(f"DB ERROR (delete_reporter): {e}", file=sys.stderr)
        return f"🚨 資料庫操作失敗: {e}"
    finally:
        conn.close()

# --- 資料庫操作：獲取回報人名單 (標題簡化) ---
def get_reporter_list(group_id):
    conn = get_db_connection()
    if conn is None:
        return "Database connection failed."

    try:
        with conn.cursor() as cur:
            # 查詢該群組/房間的所有回報人
            cur.execute("SELECT reporter_name FROM group_reporters WHERE group_id = %s ORDER BY reporter_name;", (group_id,))
            reporters = [row[0] for row in cur.fetchall()]
            
            if not reporters:
                return "📋 目前名單空空如也！快來當第一個回報者吧！使用 **新增人名 [人名]** 啟動您的進度追蹤！🚀"
            
            # 格式化輸出
            list_text = "⭐ 本團隊回報名單：\n\n"
            list_text += "\n".join([f"🔸 {name}" for name in reporters])
            
            return list_text
    except Exception as e:
        conn.rollback()
        print(f"DB ERROR (get_reporter_list): {e}", file=sys.stderr)
        return f"🚨 資料庫操作失敗: {e}"
    finally:
        conn.close()

# --- 資料庫操作：儲存回報 (情緒價值優化) ---
def save_report(group_id, report_date_str, reporter_name):
    conn = get_db_connection()
    if conn is None:
        return "Database connection failed."

    try:
        report_date = datetime.strptime(report_date_str, '%Y.%m.%d').date()
    except ValueError:
        return "📆 日期格式小錯誤！別擔心，請記得使用 **YYYY.MM.DD** 這種格式喔！例如：2025.11.17。"

    try:
        with conn.cursor() as cur:
            # 檢查回報人是否在名單中
            cur.execute("SELECT group_id FROM group_reporters WHERE group_id = %s AND reporter_name = %s;", (group_id, reporter_name))
            if not cur.fetchone():
                return f"🧐 **{reporter_name}** 看起來您還沒加入回報名單呢！請先用 **新增人名 {reporter_name}** 讓我認識您一下喔！😊"

            # 檢查當天是否已回報過
            cur.execute("SELECT * FROM reports WHERE source_id = %s AND report_date = %s AND name = %s;", (group_id, report_date, reporter_name))
            if cur.fetchone():
                # UX 修正：使用中性確認語氣，避免給人「登記」的僥倖心態
                return f"👍 效率超高！**{reporter_name}** {report_date_str} 的回報狀態早已是 **已完成** 囉！不用再操作啦，您休息一下吧！☕"

            # 儲存回報
            cur.execute("INSERT INTO reports (source_id, report_date, name) VALUES (%s, %s, %s);", (group_id, report_date, reporter_name))
            conn.commit()
            return f"✨ 成功！**{reporter_name}** 您今天做得非常棒！{report_date_str} 的進度已完美記錄！💯"
    except Exception as e:
        conn.rollback()
        print(f"DB ERROR (save_report): {e}", file=sys.stderr)
        return f"🚨 資料庫操作失敗: {e}"
    finally:
        conn.close()

# --- Webhook 路由 ---
@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    app.logger.info("Request body: " + body)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        print("Invalid signature. Check your channel secret/token.", file=sys.stderr)
        abort(400)
    except LineBotApiError as e:
        print(f"LINE API Error: {e}", file=sys.stderr)
        abort(500)
    
    return 'OK'

# --- 訊息處理：接收訊息事件 (Regex 修正：隔離星期幾) ---
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    # 關鍵修正: 只使用訊息的第一行來匹配指令
    full_text = event.message.text
    first_line = full_text.split('\n')[0].strip()
    text_to_match = first_line

    if isinstance(event.source, SourceGroup) or isinstance(event.source, SourceRoom):
        group_id = event.source.group_id if isinstance(event.source, SourceGroup) else event.source.room_id

        reply_text = None

        # 1. 處理「新增人名 [人名]」指令 (修復全形/多個空格)
        match_add = re.match(r"^新增人名[\s　]+(.+)$", text_to_match)
        if match_add:
            reporter_name = match_add.group(1).strip()
            reply_text = add_reporter(group_id, reporter_name)

        # 1.5 處理「刪除人名 [人名]」指令 (修復全形/多個空格)
        match_delete = re.match(r"^刪除人名[\s　]+(.+)$", text_to_match)
        if match_delete:
            reporter_name = match_delete.group(1).strip()
            reply_text = delete_reporter(group_id, reporter_name)

        # 1.6 處理「查詢名單 / 查看人員」指令
        if text_to_match in ["查詢名單", "查看人員", "名單", "list"]:
            reply_text = get_reporter_list(group_id)

        # 2. 處理「YYYY.MM.DD [星期幾] [人名]」回報指令
        # 最終修正 Regex: 匹配並拋棄選用的 (一) 到 (日) 標記
        # Group 1: 日期，Group 2: 純粹的人名
        regex_pattern = r"^(\d{4}\.\d{2}\.\d{2})\s*(?:[\s　]*[（(][\s\w\u4e00-\u9fff]+[)）])?\s*(.+)$"
        match_report = re.match(regex_pattern, text_to_match)

        if match_report:
            date_str = match_report.group(1)
            # Group 2 現在只包含名字 (例如 '海豚🐬')
            reporter_name = match_report.group(2).strip() 
            reply_text = save_report(group_id, date_str, reporter_name)

        # 回覆訊息
        if reply_text:
            try:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
            except Exception as e:
                print(f"LINE REPLY ERROR: {e}", file=sys.stderr)


# --- START SCHEDULER LOGIC ---

# 輔助函數：獲取所有回報人名單
def get_all_reporters(conn):
    cur = conn.cursor()
    cur.execute("SELECT group_id, reporter_name FROM group_reporters ORDER BY group_id;")
    all_reporters = cur.fetchall()
    return all_reporters

# --- 核心邏輯：發送每日提醒 (包含情緒價值優化 - 根據使用者提供的模板) ---
def send_daily_reminder(line_bot_api):
    conn = get_db_connection()
    if conn is None:
        return "Error: Database connection failed."

    # 設定要檢查的日期 (昨天)
    check_date = datetime.now().date() - timedelta(days=1)
    check_date_str = check_date.strftime('%Y.%m.%d')
    
    print(f"Scheduler running for date: {check_date_str}", file=sys.stderr)

    try:
        all_reporters = get_all_reporters(conn)
        
        groups_to_check = {}
        for group_id, reporter_name in all_reporters:
            if group_id not in groups_to_check:
                groups_to_check[group_id] = []
            groups_to_check[group_id].append(reporter_name)

        # 針對每個群組檢查未回報的人
        for group_id, reporters in groups_to_check.items():
            missing_reports = []
            
            with conn.cursor() as cur:
                for reporter_name in reporters:
                    # 檢查該回報人在該日期是否有報告記錄
                    cur.execute("SELECT name FROM reports WHERE source_id = %s AND report_date = %s AND name = %s;", 
                                (group_id, check_date, reporter_name))
                    
                    if not cur.fetchone():
                        missing_reports.append(reporter_name)

            # 如果有未回報的人，則發送提醒
            if missing_reports:
                
                # --- 新的情緒化提醒邏輯 ---
                is_singular = len(missing_reports) == 1
                
                # Part 1: Header and Missing List
                message_text = f"⏰ 緊急提醒：{check_date_str} 進度追蹤\n"
                message_text += "以下成員還沒回覆 👇\n\n"
                
                missing_list_text = "\n".join([f"👉 {name}" for name in missing_reports])
                message_text += missing_list_text
                
                if is_singular:
                    # 單人訊息：使用「你」
                    message_text += "\n\n大家都在等你的進度啦～\n"
                    message_text += "\n不著急，但你再不回，我可能就要開始懷疑你是不是打算\n"
                    message_text += "把錢藏起來不讓我們看到 😏\n"
                    message_text += "麻煩儘快補上，\n\n"
                    message_text += "讓我們能安心，也讓你的荷包不會變成大家關注的焦點喔 🙏✨"
                else:
                    # 多人訊息：使用「你們」
                    message_text += "\n\n大家都在等你們的進度啦～\n"
                    message_text += "\n不著急，但你們再不回，我可能就要開始懷疑是不是有人打算\n"
                    message_text += "把錢藏起來不讓我們看到 😏\n"
                    message_text += "麻煩儘快補上，\n\n"
                    message_text += "讓我們能安心，也讓你們的荷包不會變成關注的焦點喔 🙏✨"
                # --- 新的情緒化提醒邏輯結束 ---
                
                try:
                    line_bot_api.push_message(group_id, TextSendMessage(text=message_text))
                    print(f"Sent reminder to group {group_id} for {len(missing_reports)} missing reports.", file=sys.stderr)
                except LineBotApiError as e:
                    # 如果 Bot 不在群組中，會引發錯誤
                    print(f"LINE API PUSH ERROR to {group_id}: {e}", file=sys.stderr)
                    
    except Exception as e:
        print(f"SCHEDULER DB ERROR: {e}", file=sys.stderr)
        return f"Error during schedule processing: {e}"
    finally:
        conn.close()
    
    return "Scheduler execution finished successfully."


# --- 新增的排程觸發路由 ---
@app.route("/run_scheduler")
def run_scheduler_endpoint():
    result = send_daily_reminder(line_bot_api)
    return result

# --- END SCHEDULER LOGIC ---


# --- 啟動 Flask 應用程式 ---
if __name__ == "__main__":
    app.run(debug=True, host='0.0.0.0', port=os.getenv('PORT', 8080))