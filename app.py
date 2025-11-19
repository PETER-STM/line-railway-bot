import os
import sys
import re
from datetime import datetime, timedelta
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError, LineBotApiError, LineBotApiError
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

# --- 活潑・幽默・微毒舌 回覆模板 ---
UNIVERSAL_ERROR_MSG = (
    "💥 發生未知錯誤。\n\n"
    "可能是宇宙磁場不順，或系統在叛逆。\n\n"
    "稍後再試，或找管理員用愛感化它。"
)

# --- 姓名正規化工具 ---
def normalize_name(name):
    """
    對人名進行正規化處理，主要移除開頭的班級或編號標記。
    例如: "(三) 浣熊🦝" -> "浣熊🦝"
    """
    # 移除開頭被括號 (圓括號、全形括號、方括號、書名號) 包裹的內容，例如 (三), (二), 【1】, [A]
    # 匹配模式: ^(起始) + 任意空白 + 括號開頭 + 非括號內容(1到10個) + 括號結尾 + 任意空白
    normalized = re.sub(r'^\s*[\(（\[【][^()\[\]]{1,10}[\)）\]】]\s*', '', name).strip()
    
    # 如果正規化結果為空，返回原始名稱
    return normalized if normalized else name

# --- 資料庫連線函式 ---
def get_db_connection():
    """建立資料庫連線"""
    conn = None
    try:
        conn = psycopg2.connect(DATABASE_URL, sslmode='require')
        return conn
    except Exception as e:
        print(f"Database connection error: {e}", file=sys.stderr)
        return None

# --- 核心資料庫操作函式 ---

def add_reporter(group_id, name):
    """將新回報者加入名單"""
    conn = get_db_connection()
    if not conn: return UNIVERSAL_ERROR_MSG # 使用通用錯誤模板

    cursor = conn.cursor()
    try:
        # 檢查是否已存在 (使用儲存的原始名稱進行檢查，避免邏輯錯誤)
        cursor.execute(
            "SELECT 1 FROM reporters WHERE group_id = %s AND name = %s",
            (group_id, name)
        )
        if cursor.fetchone():
            # 新增人名 (重複) 模板
            return (
                f"🤨 {name} 早就在名單裡面坐好坐滿了，\n\n"
                f"你該不會…忘記上一次也加過吧？"
            )

        # 插入新回報者 (儲存的是使用者輸入的原始名稱)
        cursor.execute(
            "INSERT INTO reporters (group_id, name) VALUES (%s, %s)",
            (group_id, name)
        )
        conn.commit()
        # 新增人名 (成功) 模板
        return f"🎉 好嘞～ {name} 已成功加入名單！\n\n（逃不掉了，祝他順利回報。）"

    except Exception as e:
        print(f"DB Error (add_reporter): {e}", file=sys.stderr)
        return UNIVERSAL_ERROR_MSG # 使用通用錯誤模板
    finally:
        cursor.close()
        conn.close()

def remove_reporter(group_id, name):
    """從名單中移除回報者"""
    conn = get_db_connection()
    if not conn: return UNIVERSAL_ERROR_MSG # 使用通用錯誤模板

    cursor = conn.cursor()
    try:
        # 刪除時必須使用使用者輸入的精確名稱 (這是目前系統的限制)
        cursor.execute(
            "DELETE FROM reporters WHERE group_id = %s AND name = %s",
            (group_id, name)
        )
        if cursor.rowcount == 0:
            # 刪除人名 (未找到) 模板
            return (
                f"❓名單裡根本沒有 {name} 啊！\n\n"
                f"是不是名字打錯，還是你其實不想他回報？"
            )
        
        conn.commit()
        # 刪除人名 (成功) 模板
        return f"🗑️ {name} 已從名單中被溫柔移除。\n\n（放心，我沒有把人綁走，只是移出名單。）"

def list_reporters(group_id):
    """查詢回報者名單 (會將同名但帶有前綴的名稱合併顯示)"""
    conn = get_db_connection()
    if not conn: return UNIVERSAL_ERROR_MSG # 使用通用錯誤模板
    
    cursor = conn.cursor()
    try:
        # 1. 取得所有儲存的原始名稱
        cursor.execute(
            "SELECT name FROM reporters WHERE group_id = %s ORDER BY name",
            (group_id,)
        )
        original_names = [row[0] for row in cursor.fetchall()]

        if not original_names:
            # 查詢名單 (無成員) 模板
            return "📭 名單空空如也～\n\n快用 新增人名 [姓名] 把第一位勇者召喚進來吧！"
        
        # 2. 進行正規化並取得唯一的名稱集合
        # 這裡會將 (三)浣熊🦝, (二)浣熊🦝, 浣熊🦝 全部正規化為 '浣熊🦝'
        unique_normalized_names = set()
        for name in original_names:
            unique_normalized_names.add(normalize_name(name))

        # 3. 排序後用於顯示
        list_of_names = "\n".join(sorted(unique_normalized_names))
        
        # 查詢名單 (有成員) 模板
        return (
            f"📋 最新回報觀察名單如下：\n"
            f"{list_of_names}\n\n"
            f"（嗯，看起來大家都還活著。）"
        )
    except Exception as e:
        print(f"DB Error (list_reporters): {e}", file=sys.stderr)
        return UNIVERSAL_ERROR_MSG # 使用通用錯誤模板
    finally:
        cursor.close()
        conn.close()

def log_report(group_id, date, reporter_name):
    """記錄每日心得打卡 (使用正規化名稱進行匹配檢查)"""
    conn = get_db_connection()
    if not conn: return UNIVERSAL_ERROR_MSG # 使用通用錯誤模板
    
    cursor = conn.cursor()
    try:
        # 1. 檢查回報者是否在名單上 (使用正規化後的名稱進行匹配檢查)
        
        # 正規化使用者輸入的打卡名稱
        normalized_input_name = normalize_name(reporter_name)

        # 查詢資料庫中所有回報者名單
        cursor.execute(
            "SELECT name FROM reporters WHERE group_id = %s",
            (group_id,)
        )
        
        # 找到所有儲存在資料庫中，但正規化後與使用者輸入的名稱相符的原始名稱
        valid_reporter_names = [
            stored_name for (stored_name,) in cursor.fetchall()
            if normalize_name(stored_name) == normalized_input_name
        ]

        if not valid_reporter_names:
            # 記錄回報 (人名不在名單) - 使用舊有邏輯但調整語氣
            return f"⚠️ {reporter_name} 不在觀察名單上！\n\n（請先輸入「新增人名 {reporter_name}」加入，不然我不能幫你記錄喔。）"

        # 2. 決定要使用哪個名稱進行記錄 (為了系統兼容性，我們使用使用者輸入的名稱)
        name_to_log = reporter_name 
        
        # 檢查是否重複回報 (使用使用者輸入的名稱進行檢查)
        cursor.execute(
            "SELECT 1 FROM daily_reports WHERE group_id = %s AND report_date = %s AND reporter_name = %s",
            (group_id, date, name_to_log)
        )
        if cursor.fetchone():
            # 記錄回報 (重複記錄) 模板
            return f"⚠️ {name_to_log} ({date}) 今天已經回報過了！\n\n別想靠重複交作業刷存在感，我看的很清楚 👀"

        # 3. 記錄回報 (使用使用者輸入的名稱進行記錄)
        cursor.execute(
            "INSERT INTO daily_reports (group_id, report_date, reporter_name) VALUES (%s, %s, %s)",
            (group_id, date, name_to_log)
        )
        conn.commit()
        # 記錄回報 (成功) 模板
        return (
            f"👌 收到！{name_to_log} ({date}) 的心得已成功登入檔案。\n\n"
            f"（今天有乖，給你一個隱形貼紙 ⭐）"
        )

    except Exception as e:
        print(f"DB Error (log_report): {e}", file=sys.stderr)
        return UNIVERSAL_ERROR_MSG # 使用通用錯誤模板
    finally:
        cursor.close()
        conn.close()

# --- 資料庫初始化 (僅在應用程式啟動時執行一次) ---
def init_db():
    conn = get_db_connection()
    if not conn: 
        print("Database initialization failed: No connection.", file=sys.stderr)
        return
    
    cursor = conn.cursor()
    try:
        # 建立 reporters 表 (回報者名單)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS reporters (
                id SERIAL PRIMARY KEY,
                group_id TEXT NOT NULL,
                name TEXT NOT NULL,
                UNIQUE (group_id, name)
            );
        """)
        # 建立 daily_reports 表 (每日打卡記錄)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS daily_reports (
                id SERIAL PRIMARY KEY,
                group_id TEXT NOT NULL,
                report_date DATE NOT NULL,
                reporter_name TEXT NOT NULL,
                reported_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (group_id, report_date, reporter_name)
            );
        """)
        conn.commit()
        print("Database initialized successfully (tables checked/created).", file=sys.stderr)
    except Exception as e:
        print(f"Database initialization error: {e}", file=sys.stderr)
    finally:
        cursor.close()
        conn.close()

# 應用程式啟動時執行資料庫初始化
init_db()

# --- 命令偵測正規表達式 ---

# 偵測心得分享的正規表達式 (日期 [可選的星期幾] 姓名 內容)
# Group 1: 日期 (e.g., 2025.11.18)
# Group 2: 人名 (name) 
# Group 3: 內容 (content)
REPORT_REGEX = re.compile(r'^(\d{4}\.\d{2}\.\d{2})\s*(?:\([一二三四五六日]\))?\s*(.+?)\s*([\s\S]+)$', re.MULTILINE)

# 偵測新增人名的正規表達式 (已移除前綴)
ADD_REGEX = re.compile(r'^新增人名\s*(.+)$')

# 偵測移除人名的正規表達式 (已移除前綴)
REMOVE_REGEX = re.compile(r'^移除人名\s*(.+)$')

# 偵測查詢名單的正規表達式 (已移除前綴)
LIST_REGEX = re.compile(r'^查詢名單$')

# NEW: 偵測測試排程指令
TEST_SCHEDULE_REGEX = re.compile(r'^測試排程$')

# --- LINE Webhook 處理 ---

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    app.logger.info("Request body: " + body)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        print("Invalid signature. Please check your channel access token/secret.", file=sys.stderr)
        abort(400)
    except Exception as e:
        print(f"Error handling webhook: {e}", file=sys.stderr)
        abort(500)

    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    text = event.message.text.strip()
    reply_text = None

    # 必須在群組或聊天室內才能追蹤
    if not isinstance(event.source, (SourceGroup, SourceRoom)):
        reply_text = "請將我加入群組或聊天室才能開始追蹤心得分享喔！"
        try:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
        except LineBotApiError as e:
             print(f"LINE API REPLY ERROR: {e}", file=sys.stderr)
        return

    # 取得群組 ID
    if isinstance(event.source, SourceGroup):
        group_id = event.source.group_id
    elif isinstance(event.source, SourceRoom):
        group_id = event.source.room_id
    else:
        # 理論上不會發生，但作為防護
        return
    
    # --- 1. 處理控制指令 (已移除「施恩澤」前綴) ---
    
    # 偵測新增人名指令: 新增人名 姓名
    match_add = ADD_REGEX.match(text)
    if match_add:
        name_to_add = match_add.group(1).strip()
        if name_to_add:
            reply_text = add_reporter(group_id, name_to_add)
        else:
            reply_text = "⚠️ 請提供要新增的人名，格式：新增人名 [姓名]"

    # 偵測移除人名指令: 移除人名 姓名
    match_remove = REMOVE_REGEX.match(text)
    if match_remove:
        name_to_remove = match_remove.group(1).strip()
        if name_to_remove:
            reply_text = remove_reporter(group_id, name_to_remove)
        else:
            reply_text = "⚠️ 請提供要移除的人名，格式：移除人名 [姓名]"

    # 偵測查詢名單指令: 查詢名單
    match_list = LIST_REGEX.match(text)
    if match_list:
        reply_text = list_reporters(group_id)

    # NEW: 偵測測試排程指令: 測試排程
    match_test_schedule = TEST_SCHEDULE_REGEX.match(text)
    if match_test_schedule:
        if group_id in EXCLUDE_GROUP_IDS:
            # 測試排程 (已排除群組) 模板
            reply_text = "🚫 這個群組在「排除名單」裡，\n\n排程器看到這邊會自動裝死，不會發任何提醒。"
        else:
            # 測試排程 (正常群組) 模板
            reply_text = "🔔 測試指令 OK！\n\n請坐等排程器在設定時間跳出來嚇你，\n\n以確認系統正常運作。"


    # --- 2. 處理心得分享 (打卡) ---
    
    # 偵測心得分享格式: YYYY.MM.DD 姓名 內容...
    match_report = REPORT_REGEX.match(text)
    if match_report and not reply_text: # 如果沒有命中前面的控制指令，才檢查心得
        date_str = match_report.group(1) # 日期是第一個捕獲組
        name_str = match_report.group(2).strip() # 人名是第二個捕獲組

        try:
            report_date = datetime.strptime(date_str, '%Y.%m.%d').date()
            reporter_name = name_str
            
            # 確保人名不為空
            if not reporter_name:
                # 記錄回報 (人名遺失) 模板
                reply_text = "⚠️ 日期後面請記得加上人名，不然我不知道誰交的啊！\n\n（你總不會想讓我自己猜吧？）"
            else:
                # 呼叫 log_report，只記錄打卡資訊
                reply_text = log_report(group_id, report_date, reporter_name)
            
        except ValueError:
            # 記錄回報 (日期格式錯誤) 模板
            reply_text = "❌ 日期長得怪怪的。\n\n請用標準格式：YYYY.MM.DD 姓名\n\n（小數點不是你的自由發揮。）"

    # 發送回覆訊息 (這是對使用者的指令回覆，不是催繳訊息)
    if reply_text:
        try:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=reply_text)
            )
        except LineBotApiError as e:
            # 如果 reply_message 失敗，嘗試 push_message (例如：超過 3 秒回覆期限)
            print(f"LINE API REPLY ERROR: {e}. Trying push message...", file=sys.stderr)
            try:
                # Fallback to push_message
                line_bot_api.push_message(
                    group_id,
                    TextSendMessage(text=reply_text)
                )
            except LineBotApiError as push_e:
                print(f"LINE API PUSH ERROR: {push_e}", file=sys.stderr)
                
# --- Flask 啟動 ---
if __name__ == "__main__":
    app.run(debug=True)