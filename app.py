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
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
DATABASE_URL = os.environ.get('DATABASE_URL')
# NEW: 排除的群組ID列表 (用於測試功能時跳過某些群組)
EXCLUDE_GROUP_IDS_STR = os.environ.get('EXCLUDE_GROUP_IDS', '')
EXCLUDE_GROUP_IDS = set(EXCLUDE_GROUP_IDS_STR.split(',')) if EXCLUDE_GROUP_IDS_STR else set()

# --- 診斷與初始化 ---
if not LINE_CHANNEL_ACCESS_TOKEN:
    sys.exit("LINE_CHANNEL_ACCESS_TOKEN is missing!")
if not LINE_CHANNEL_SECRET:
    sys.exit("LINE_CHANNEL_SECRET is missing!")

app = Flask(__name__)
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# --- 資料庫連線函式 ---
def get_db_connection():
    """建立資料庫連線"""
    try:
        conn = psycopg2.connect(DATABASE_URL, sslmode='require')
        return conn
    except Exception as e:
        print(f"DATABASE CONNECTION ERROR: {e}", file=sys.stderr)
        return None

# --- NEW: 全域設定管理函式 ---

def set_pause_state(state):
    """
    設定或切換全域提醒暫停狀態 (state: 'true' 或 'false')
    這會影響 scheduler.py 是否會發送每日提醒。
    """
    conn = get_db_connection()
    if conn is None:
        return "🚨 資料庫連線失敗，無法切換狀態。"
    
    try:
        with conn.cursor() as cur:
            # 確保資料表中 'is_paused' 鍵存在 (如果不存在，則插入預設值)
            cur.execute("INSERT INTO settings (key, value) VALUES ('is_paused', 'false') ON CONFLICT (key) DO NOTHING;")
            # 更新狀態
            cur.execute("UPDATE settings SET value = %s WHERE key = 'is_paused';", (state,))
            conn.commit()
            
            is_paused = state == 'true'
            
            if is_paused:
                return "⏸️ 全域提醒已暫停！ \n\n✅ 每日定時催交通知將不會發送。您可以安心進行維護或更新作業。 \n\n使用 `恢復回報提醒` 重新啟用。"
            else:
                return "▶️ 全域提醒已恢復！ \n\n✅ 每日定時催交通知將會照常發送。系統已進入正常運作模式！"
    except Exception as e:
        conn.rollback()
        print(f"DB ERROR (set_pause_state): {e}", file=sys.stderr)
        return f"🚨 資料庫操作失敗: {e}"
    finally:
        if conn: conn.close()

# --- 資料庫操作：新增/刪除/查詢回報人/儲存回報 ---

def add_reporter(group_id, reporter_name):
    """新增回報者到群組名單"""
    conn = get_db_connection()
    if conn is None:
        return "Database connection failed."

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT group_id FROM group_reporters WHERE group_id = %s AND reporter_name = %s;", (group_id, reporter_name))
            if cur.fetchone():
                return f"😉 哎呀，{reporter_name} 已經在名單中囉！感謝您的熱情！🔥"

            cur.execute("INSERT INTO group_reporters (group_id, reporter_name) VALUES (%s, %s);", (group_id, reporter_name))
            conn.commit()
            return f"太棒了！歡迎 {reporter_name} 加入回報名單！從今天起一起努力吧！"
    except Exception as e:
        conn.rollback()
        print(f"DB ERROR (add_reporter): {e}", file=sys.stderr)
        return f"🚨 資料庫操作失敗: {e}"
    finally:
        if conn: conn.close()

def delete_reporter(group_id, reporter_name):
    """從群組名單中刪除回報者，並清除其歷史紀錄"""
    conn = get_db_connection()
    if conn is None:
        return "Database connection failed."

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT group_id FROM group_reporters WHERE group_id = %s AND reporter_name = %s;", (group_id, reporter_name))
            if not cur.fetchone():
                return f"🤔 咦？我查了一下，{reporter_name} 不在回報人名單上耶。是不是名字打錯了呢？請再檢查一下喔！"

            cur.execute("DELETE FROM group_reporters WHERE group_id = %s AND reporter_name = %s;", (group_id, reporter_name))
            # 同時刪除該人名在該群組的所有歷史回報紀錄
            cur.execute("DELETE FROM reports WHERE group_id = %s AND name = %s;", (group_id, reporter_name))

            conn.commit()
            return f"👋 好的，我們已經跟 {reporter_name} 說掰掰了，資料庫也順利清空。管理名單完成！🧹"
    except Exception as e:
        conn.rollback()
        print(f"DB ERROR (delete_reporter): {e}", file=sys.stderr)
        return f"🚨 資料庫操作失敗: {e}"
    finally:
        if conn: conn.close()

def get_reporter_list(group_id):
    """獲取單一群組的回報者名單"""
    conn = get_db_connection()
    if conn is None:
        return "Database connection failed."

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT reporter_name FROM group_reporters WHERE group_id = %s ORDER BY reporter_name;", (group_id,))
            reporters = [row[0] for row in cur.fetchall()]
            
            if not reporters:
                return "📋 目前名單空空如也！快來當第一個回報者吧！使用 新增人名 [人名] 啟動您的進度追蹤！🚀"
            
            list_text = "⭐ 本團隊回報名單：\n\n"
            list_text += "\n".join([f"🔸 {name}" for name in reporters])
            
            return list_text
    except Exception as e:
        conn.rollback()
        print(f"DB ERROR (get_reporter_list): {e}", file=sys.stderr)
        return f"🚨 資料庫操作失敗: {e}"
    finally:
        if conn: conn.close()

def get_all_reporters_across_groups():
    """獲取系統中所有群組的總回報者名單"""
    conn = get_db_connection()
    if conn is None:
        return "Database connection failed."

    try:
        with conn.cursor() as cur:
            # 查詢所有群組的回報者，並按群組ID排序
            cur.execute("SELECT group_id, reporter_name FROM group_reporters ORDER BY group_id, reporter_name;")
            results = cur.fetchall()
            
            if not results:
                return "📋 整個系統目前沒有任何回報者紀錄！"
            
            # 將結果按 group_id 分組
            grouped_reporters = {}
            for group_id, reporter_name in results:
                # 為了避免群組ID過長，只顯示前10個字符作為識別
                display_id = group_id[:10] + "..." if len(group_id) > 10 else group_id
                
                if display_id not in grouped_reporters:
                    grouped_reporters[display_id] = []
                grouped_reporters[display_id].append(reporter_name)
            
            list_text = "🌐 跨群組回報總名單：\n\n"
            for display_id, reporters in grouped_reporters.items():
                list_text += f"📦 群組 ID (開頭)：{display_id}\n"
                list_text += "    " + "、".join(reporters) + "\n\n"
            
            return list_text
    except Exception as e:
        conn.rollback()
        print(f"DB ERROR (get_all_reporters_across_groups): {e}", file=sys.stderr)
        return f"🚨 資料庫操作失敗: {e}"
    finally:
        if conn: conn.close()

def save_report(group_id, report_date_str, reporter_name):
    """儲存回報紀錄"""
    conn = get_db_connection()
    if conn is None:
        return "Database connection failed."

    try:
        # 將字串轉換為日期物件
        report_date = datetime.strptime(report_date_str, '%Y.%m.%d').date()
    except ValueError:
        return "📆 日期格式小錯誤！別擔心，請記得使用 YYYY.MM.DD 這種格式喔！例如：2025.11.17。"

    try:
        with conn.cursor() as cur:
            # 檢查回報人是否在名單中
            cur.execute("SELECT group_id FROM group_reporters WHERE group_id = %s AND reporter_name = %s;", (group_id, reporter_name))
            if not cur.fetchone():
                return f"🧐 {reporter_name} 看起來您還沒加入回報名單呢！請先用 新增人名 {reporter_name} 讓我認識您一下喔！😊"

            # 檢查是否重複回報
            cur.execute("SELECT * FROM reports WHERE group_id = %s AND report_date = %s AND name = %s;", (group_id, report_date, reporter_name))
            if cur.fetchone():
                return f"👍 效率超高！{reporter_name} {report_date_str} 的回報狀態早已是 已完成 囉！不用再操作啦，您休息一下吧！☕"

            # 儲存回報
            cur.execute("INSERT INTO reports (group_id, report_date, name) VALUES (%s, %s, %s);", (group_id, report_date, reporter_name))
            conn.commit()
            return f"✨ 成功！{reporter_name} 您今天做得非常棒！{report_date_str} 的進度已完美記錄！💯"
    except Exception as e:
        conn.rollback()
        print(f"DB ERROR (save_report): {e}", file=sys.stderr)
        return f"🚨 資料庫操作失敗: {e}"
    finally:
        if conn: conn.close()

# --- 單一群組測試提醒功能 ---
def test_daily_reminder(group_id):
    """執行定時排程的邏輯，但僅針對單一群組，並立即返回結果 (不發送 PUSH)。"""
    conn = get_db_connection()
    if conn is None:
        return "🚨 資料庫連線失敗，無法執行測試。"

    # 檢查昨天 (測試通常在白天，檢查昨天的進度)
    check_date = datetime.now().date() - timedelta(days=1)
    check_date_str = check_date.strftime('%Y.%m.%d')
    
    try:
        with conn.cursor() as cur:
            # 1. 獲取此群組的回報者名單
            cur.execute("SELECT reporter_name FROM group_reporters WHERE group_id = %s;", (group_id,))
            reporters = [row[0] for row in cur.fetchall()]
            
            if not reporters:
                return f"📋 測試提醒：群組中無回報者名單，無法測試 {check_date_str} 的催交功能。\n\n（此訊息為測試功能，其他群組未收到）"
            
            # 2. 檢查未回報者
            missing_reports = []
            for reporter_name in reporters:
                # 檢查 'reports' 表中是否有昨日的記錄
                cur.execute("SELECT name FROM reports WHERE group_id = %s AND report_date = %s AND name = %s;", 
                            (group_id, check_date, reporter_name))
                
                if not cur.fetchone():
                    missing_reports.append(reporter_name)

            # 3. 構造回覆訊息
            if not missing_reports:
                # 無人未回報，測試成功 (無需催交)
                return f"✅ 測試成功：{check_date_str} 的回報全員已完成！\n\n（此訊息為測試功能，其他群組未收到）"
            
            # 有人未回報，構造催交訊息
            is_singular = len(missing_reports) == 1
            
            message_text = f"🧪 [測試提醒] 心得催交通知\n\n"
            message_text += f"大家好～\n"
            message_text += f"截至 {check_date_str}，以下同學的心得還沒交👇\n\n"
            
            missing_list_text = "\n".join([f"👉 {name}" for name in missing_reports])
            message_text += missing_list_text
            
            # 使用 scheduler.py 的模板邏輯
            if is_singular:
                message_text += "\n\n📌 小提醒：再不交心得，我的 咚錢模式就要開啟啦💸\n"
                message_text += "💡 快交上來吧，別讓我每天都在追著你問～\n\n"
                message_text += "期待看到你的 心得分享，別讓我一直盯著這份名單 😏"
            else:
                message_text += "\n\n📌 小提醒：再不交心得，我的 咚錢模式就要開啟啦💸\n"
                message_text += "💡 快交上來吧，別讓我每天都在追著你們問～\n\n"
                message_text += "期待看到你們的 心得分享，別讓我一直盯著這份名單 😏"
                
            return message_text
            
    except Exception as e:
        print(f"DB ERROR (test_daily_reminder): {e}", file=sys.stderr)
        return f"🚨 測試時發生錯誤: {e}"
    finally:
        if conn: conn.close()

# --- 全群組測試提醒功能 (會發送 PUSH 訊息) ---
def test_all_daily_reminders():
    """模擬定時排程的邏輯，針對所有群組執行並使用 push_message 發送通知。"""
    conn = get_db_connection()
    if conn is None:
        return "🚨 資料庫連線失敗，無法執行全群組測試。"

    if line_bot_api is None:
        return "🚨 LINE Bot API 未初始化，無法發送訊息。"
    
    # 檢查昨天 (測試通常在白天，檢查昨天的進度)
    check_date = datetime.now().date() - timedelta(days=1)
    check_date_str = check_date.strftime('%Y.%m.%d')
    
    print(f"--- Running ALL GROUP test reminder check for date: {check_date_str} ---", file=sys.stderr)

    try:
        with conn.cursor() as cur:
            # 檢查全域暫停狀態
            cur.execute("SELECT value FROM settings WHERE key = 'is_paused';")
            result = cur.fetchone()
            is_paused = result and result[0] == 'true'
            
            if is_paused:
                return "⏸️ **系統目前處於全域暫停狀態。** \n\n全群組測試 PUSH 功能已被鎖定，無法執行。請先使用 `恢復回報提醒` 啟用 Bot。"


            # 1. 獲取所有群組的回報者名單
            cur.execute("SELECT group_id, reporter_name FROM group_reporters ORDER BY group_id, reporter_name;")
            all_reporters = cur.fetchall()
            
            if not all_reporters:
                return f"📋 測試提醒：系統中沒有任何群組或回報者名單，無法測試催交功能。"

            groups_to_check = {}
            for group_id, reporter_name in all_reporters:
                if group_id not in groups_to_check:
                    groups_to_check[group_id] = []
                groups_to_check[group_id].append(reporter_name)

            # 過濾掉被排除的測試群組 (EXCLUDE_GROUP_IDS)
            filtered_groups_to_check = {
                gid: reporters for gid, reporters in groups_to_check.items() 
                if gid not in EXCLUDE_GROUP_IDS
            }

            total_groups = len(filtered_groups_to_check)
            
            if total_groups == 0:
                 # 如果有回報者紀錄，但所有群組都被排除，則進行此回覆
                if groups_to_check:
                    return f"📋 測試提醒：所有設定回報者的群組均在 **排除名單** 中 (EXCLUDE_GROUP_IDS)，故未發送任何 PUSH 通知。"
                
                return f"📋 測試提醒：系統中沒有任何群組或回報者名單，無法測試催交功能。"
            
            affected_groups = 0
            
            for group_id, reporters in filtered_groups_to_check.items():
                missing_reports = []
                
                # 檢查未回報者
                for reporter_name in reporters:
                    cur.execute("SELECT name FROM reports WHERE group_id = %s AND report_date = %s AND name = %s;", 
                                (group_id, check_date, reporter_name))
                    
                    if not cur.fetchone():
                        missing_reports.append(reporter_name)

                # 構造並發送 push 訊息
                if missing_reports:
                    affected_groups += 1
                    is_singular = len(missing_reports) == 1
                    
                    # 採用定時排程的訊息模板，但加上 [全群組測試提醒] 的前綴
                    message_text = f"🧪 [全群組測試提醒] 心得催交通知\n\n"
                    message_text += f"大家好～\n"
                    message_text += f"截至 {check_date_str}，以下同學的心得還沒交👇\n\n"
                    
                    missing_list_text = "\n".join([f"👉 {name}" for name in missing_reports])
                    message_text += missing_list_text
                    
                    if is_singular:
                        message_text += "\n\n📌 小提醒：再不交心得，我的 咚錢模式就要開啟啦💸\n"
                        message_text += "💡 快交上來吧，別讓我每天都在追著你問～\n\n"
                        message_text += "期待看到你的 心得分享，別讓我一直盯著這份名單 😏"
                    else:
                        message_text += "\n\n📌 小提醒：再不交心得，我的 咚錢模式就要開啟啦💸\n"
                        message_text += "💡 快交上來吧，別讓我每天都在追著你們問～\n\n"
                        message_text += "期待看到你們的 心得分享，別讓我一直盯著這份名單 😏"
                    
                    try:
                        # 使用 push_message 發送給目標群組
                        line_bot_api.push_message(group_id, TextSendMessage(text=message_text))
                        print(f"Pushed test reminder to group {group_id} for {len(missing_reports)} missing reports.", file=sys.stderr)
                    except LineBotApiError as e:
                        print(f"LINE API PUSH ERROR to {group_id}: {e}", file=sys.stderr)
            
            if affected_groups > 0:
                return f"📢 已向 {affected_groups} / {total_groups} 個（已排除測試群組）未完成回報的群組發送 **[全群組測試提醒]** PUSH 通知。\n\n**警告：此操作已主動推送訊息至所有受影響群組。**"
            else:
                return f"✅ 全群組測試成功：所有 {total_groups} 個群組（已排除測試群組）在 {check_date_str} 的回報中都已全員完成，無須發送催交通知！"
            
    except Exception as e:
        print(f"DB ERROR (test_all_daily_reminders): {e}", file=sys.stderr)
        return f"🚨 全群組測試時發生錯誤: {e}"
    finally:
        if conn: conn.close()


# --- Webhook 路由 ---
@app.route("/callback", methods=['POST'])
def callback():
    """LINE 平台傳送訊息的入口"""
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

# --- 訊息處理：接收訊息事件 ---
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    """處理收到的文字訊息"""
    full_text = event.message.text
    text_to_match = full_text.split('\n')[0].strip() # 只匹配第一行指令

    # 1. 處理特殊的全域查詢指令 (可在任何地方使用，包含個人聊天)
    if text_to_match in ["查詢所有人員", "all list", "所有名單"]:
        reply_text = get_all_reporters_across_groups()
        try:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
        except Exception as e:
            print(f"LINE REPLY ERROR (all list): {e}", file=sys.stderr)
        return # 執行完畢，跳出函式

    # 2. 處理全域暫停/恢復指令 (可在任何地方使用)
    if text_to_match in ["暫停回報提醒", "pause reminder"]:
        reply_text = set_pause_state('true')
        try:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
        except Exception as e:
            print(f"LINE REPLY ERROR (pause): {e}", file=sys.stderr)
        return
        
    if text_to_match in ["恢復回報提醒", "resume reminder"]:
        reply_text = set_pause_state('false')
        try:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
        except Exception as e:
            print(f"LINE REPLY ERROR (resume): {e}", file=sys.stderr)
        return

    # 3. 處理群組或聊天室內的一般指令
    if isinstance(event.source, SourceGroup) or isinstance(event.source, SourceRoom):
        # 獲取群組 ID 或聊天室 ID
        group_id = event.source.group_id if isinstance(event.source, SourceGroup) else event.source.room_id

        reply_text = None
        
        # 處理全群組測試提醒功能
        if text_to_match in ["/TEST ALL REMINDER", "群組測試提醒", "全群組測試"]:
            reply_text = test_all_daily_reminders()
            
        # 處理單一群組測試提醒功能
        if text_to_match in ["/TEST REMINDER", "測試提醒"]:
            # 確保不會覆蓋掉全群組測試的結果
            if reply_text is None:
                reply_text = test_daily_reminder(group_id)
            
        # 處理管理指令 (新增/刪除人名, 查詢名單)
        match_add = re.match(r"^新增人名[\s　]+(.+)$", text_to_match)
        if match_add:
            reporter_name = match_add.group(1).strip()
            reply_text = add_reporter(group_id, reporter_name)

        match_delete = re.match(r"^刪除人名[\s　]+(.+)$", text_to_match)
        if match_delete:
            reporter_name = match_delete.group(1).strip()
            reply_text = delete_reporter(group_id, reporter_name)

        if text_to_match in ["查詢名單", "查看人員", "名單", "list"]:
            reply_text = get_reporter_list(group_id)

        # 處理「YYYY.MM.DD [星期幾] [人名]」回報指令
        regex_pattern = r"^(\d{4}\.\d{2}\.\d{2})\s*(?:[\s　]*[（(][\s\w\u4e00-\u9fff]+[)）])?\s*(.+)$"
        match_report = re.match(regex_pattern, text_to_match)

        if match_report:
            date_str = match_report.group(1)
            reporter_name = match_report.group(2).strip() 
            reply_text = save_report(group_id, date_str, reporter_name)

        # 統一回覆訊息
        if reply_text:
            try:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
            except Exception as e:
                print(f"LINE REPLY ERROR: {e}", file=sys.stderr)

# --- 啟動 Flask 應用程式 ---
if __name__ == "__main__":
    app.run(debug=True, host='0.0.0.0', port=os.getenv('PORT', 8080))