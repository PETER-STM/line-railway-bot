import os
import sys
import re
from datetime import datetime, timedelta
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError, LineBotApiError, LineBotApiError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, SourceGroup, SourceRoom, SourceUser
import psycopg2

# --- 姓名正規化工具 (用於確保 VIP 記錄唯一性，並解決重複名稱問題) ---
def normalize_name(name):
    """
    對人名進行正規化處理，主要移除開頭的班級或編號標記。
    例如: "(三) 浣熊🦝" -> "浣熊🦝"
    """
    # 移除開頭被括號 (圓括號、全形括號、方括號、書名號) 包裹的內容，例如 (三), (二), 【1】, [A]
    # 匹配模式: ^(起始) + 任意空白 + 括號開頭 + 非括號內容(1到10個) + 括號結尾 + 任意空白
    normalized = re.sub(r'^\s*[\(（\[【][^()\\[\]]{1,10}[\)）\]】]\s*', '', name).strip()
    
    # 如果正規化結果為空，返回原始名稱
    return normalized if normalized else name

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
    conn = None
    try:
        # 在連線時加入 sslmode='require' 以確保與 Railway 的 PostgreSQL 連線安全
        conn = psycopg2.connect(DATABASE_URL, sslmode='require')
        return conn
    except Exception as e:
        print(f"DB CONNECTION ERROR: {e}", file=sys.stderr)
        return None

# --- 資料庫操作函式 ---

def log_report(group_id, report_date, reporter_name):
    """將心得報告打卡記錄到資料庫，並檢查是否為 VIP 名單中的人"""
    conn = get_db_connection()
    if not conn:
        return "❌ 資料庫連線失敗，請稍後再試。"

    # 對 incoming name 進行正規化，以便比對 VIP 名單和檢查重複提交
    normalized_reporter_name = normalize_name(reporter_name)

    try:
        with conn.cursor() as cursor:
            # 1. 檢查是否為 VIP 成員 (VIP 名單 now stores normalized names)
            cursor.execute(
                "SELECT COUNT(*) FROM vips WHERE group_id = %s AND vip_name = %s;",
                (group_id, normalized_reporter_name)
            )
            is_vip = cursor.fetchone()[0] > 0
            
            if not is_vip:
                # 注意：這裡回覆時使用原始名稱，避免使用者困惑
                return f"⚠️ 咦？「{reporter_name}」不是本群組的 VIP 成員喔！\n\n請先用「!VIP 姓名」指令將他/她加入 VIP 名單。"

            # 2. 檢查是否重複打卡 (使用正規化名稱來確認該人是否已交)
            cursor.execute(
                "SELECT reporter_name FROM reports WHERE group_id = %s AND report_date = %s;",
                (group_id, report_date)
            )
            submitted_names = {row[0] for row in cursor.fetchall()}
            submitted_normalized_names = {normalize_name(name) for name in submitted_names}

            if normalized_reporter_name in submitted_normalized_names:
                # 重複打卡回覆模板
                return f"👀 你確定你不是在鬧？「{reporter_name}」在 {report_date.strftime('%Y/%m/%d')} 已經交過心得啦！\n\n別偷懶，去交新的！"
            
            # 3. 執行打卡記錄 (reports 表儲存原始名稱，以利追溯)
            cursor.execute(
                "INSERT INTO reports (group_id, report_date, reporter_name) VALUES (%s, %s, %s);",
                (group_id, report_date, reporter_name)
            )
            conn.commit()

            # 成功打卡回覆模板
            return f"✅ 打卡成功！\n\nVIP：{reporter_name}\n日期：{report_date.strftime('%Y/%m/%d')}\n\n系統已收錄您的心得，感謝您的分享！"

    except Exception as e:
        conn.rollback()
        print(f"DB log_report ERROR: {e}", file=sys.stderr)
        return f"🚨 伺服器記錄時發生錯誤：{e}"
    finally:
        if conn: conn.close()

def log_vip(group_id, vip_name):
    """將新的 VIP 成員記錄到資料庫 (使用正規化後的名稱)"""
    conn = get_db_connection()
    if not conn:
        return "❌ 資料庫連線失敗，請稍後再試。"

    # 對輸入名稱進行正規化，並以正規化後的名稱作為資料庫記錄的唯一識別
    normalized_vip_name = normalize_name(vip_name)

    try:
        with conn.cursor() as cursor:
            # 檢查是否已存在 (使用正規化後的名稱檢查)
            cursor.execute(
                "SELECT COUNT(*) FROM vips WHERE group_id = %s AND vip_name = %s;",
                (group_id, normalized_vip_name)
            )
            if cursor.fetchone()[0] > 0:
                # 回覆時使用正規化後的名稱，因為這是資料庫中的儲存名稱
                return f"💡 「{normalized_vip_name}」已經是本群組的 VIP 啦！不用重複加入喔。"

            # 執行新增 VIP (儲存正規化後的名稱)
            cursor.execute(
                "INSERT INTO vips (group_id, vip_name) VALUES (%s, %s);",
                (group_id, normalized_vip_name)
            )
            conn.commit()
            return f"🎉 恭喜！「{normalized_vip_name}」已成功加入 VIP 名單！\n\n歡迎進入心得分享的行列！"
    except Exception as e:
        conn.rollback()
        print(f"DB log_vip ERROR: {e}", file=sys.stderr)
        return f"🚨 伺服器記錄時發生錯誤：{e}"
    finally:
        if conn: conn.close()

def remove_vip(group_id, vip_name):
    """從資料庫中移除 VIP 成員 (使用正規化後的名稱)"""
    conn = get_db_connection()
    if not conn:
        return "❌ 資料庫連線失敗，請稍後再試。"
    
    # 對輸入名稱進行正規化
    normalized_vip_name = normalize_name(vip_name)

    try:
        with conn.cursor() as cursor:
            # 檢查是否仍存在 (使用正規化後的名稱檢查)
            cursor.execute(
                "SELECT COUNT(*) FROM vips WHERE group_id = %s AND vip_name = %s;",
                (group_id, normalized_vip_name)
            )
            if cursor.fetchone()[0] == 0:
                # 回覆時使用正規化後的名稱
                return f"💡 「{normalized_vip_name}」本來就不在本群組的 VIP 名單中喔。"

            # 執行移除 VIP (使用正規化後的名稱)
            cursor.execute(
                "DELETE FROM vips WHERE group_id = %s AND vip_name = %s;",
                (group_id, normalized_vip_name)
            )
            conn.commit()
            return f"🗑️ 「{normalized_vip_name}」已從 VIP 名單中移除。\n\n感謝這位 VIP 過去的貢獻！"
    except Exception as e:
        conn.rollback()
        print(f"DB remove_vip ERROR: {e}", file=sys.stderr)
        return f"🚨 伺服器移除時發生錯誤：{e}"
    finally:
        if conn: conn.close()
        
def list_vips(group_id):
    """列出群組的所有 VIP 成員 (資料庫中儲存的即為正規化後的名稱)"""
    conn = get_db_connection()
    if not conn:
        return "❌ 資料庫連線失敗，無法查詢。"
    
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT vip_name FROM vips WHERE group_id = %s ORDER BY vip_name;",
                (group_id,)
            )
            vips = [row[0] for row in cursor.fetchall()]
            
            if not vips:
                return "📌 本群組目前沒有任何 VIP 成員！\n\n快使用「!VIP 姓名」來加入第一個 VIP 吧！"
            
            vip_list = "\n".join([f"- {name}" for name in vips])
            return f"👑 本群組的 VIP 成員名單 👑\n\n{vip_list}\n\n(總人數: {len(vips)})"
    except Exception as e:
        print(f"DB list_vips ERROR: {e}", file=sys.stderr)
        return f"🚨 伺服器查詢時發生錯誤：{e}"
    finally:
        if conn: conn.close()

def list_reporters(group_id):
    """
    列出所有 VIP 在最近 N 天（例如 7 天）內的打卡記錄，
    並顯示在指定日期 (通常是昨天) 誰未交心得。
    """
    # 這裡的邏輯比較複雜，主要是檢查前一天的完成情況
    # 因為這個功能主要由排程腳本 `scheduler.py` 處理，
    # 為了簡潔，我們只讓這個指令列出 VIP 名單
    return list_vips(group_id)

# --- Flask 路由與 LINE Webhook 處理 ---

@app.route("/", methods=['GET'])
def home():
    """健康檢查路由，回應 200 OK 確保服務持續運行"""
    return "Line Bot Reminder Service is Running!", 200

@app.route("/callback", methods=['POST'])
def callback():
    """LINE 平台呼叫的 Webhook 接口"""
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    app.logger.info("Request body: " + body)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        print("Invalid signature. Please check your channel access token/secret.", file=sys.stderr)
        abort(400)
    except Exception as e:
        print(f"Webhook handling error: {e}", file=sys.stderr)
        abort(500)

    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    """處理接收到的訊息"""
    # 確保訊息來自群組或房間
    if not (isinstance(event.source, SourceGroup) or isinstance(event.source, SourceRoom)):
        if isinstance(event.source, SourceUser):
            # 私訊回覆模板
            reply_text = "👋 嗨！我是心得分享催繳小幫手！\n\n但我只為群組/房間服務喔，請把我加到您的群組中！"
            try:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
            except LineBotApiError as e:
                print(f"LINE API reply failed for user: {e}", file=sys.stderr)
        return

    # 取得群組或房間 ID
    group_id = None
    if isinstance(event.source, SourceGroup):
        group_id = event.source.group_id
    elif isinstance(event.source, SourceRoom):
        group_id = event.source.room_id
    
    # 如果群組 ID 在排除列表中，則忽略
    if group_id in EXCLUDE_GROUP_IDS:
        return
        
    text = event.message.text.strip()
    reply_text = None
    
    # 1. 處理 VIP 相關指令
    match_vip_add = re.match(r"^!VIP\s+(.+)$", text, re.IGNORECASE)
    match_vip_remove = re.match(r"^!RMVIP\s+(.+)$", text, re.IGNORECASE)
    
    if match_vip_add:
        vip_name = match_vip_add.group(1).strip()
        reply_text = log_vip(group_id, vip_name)
        
    elif match_vip_remove:
        vip_name = match_vip_remove.group(1).strip()
        reply_text = remove_vip(group_id, vip_name)
        
    elif re.match(r"^!LIST\s*VIP$", text, re.IGNORECASE) or re.match(r"^!VIP\s*LIST$", text, re.IGNORECASE):
        reply_text = list_vips(group_id)
        
    # 2. 處理報告/打卡指令
    # 格式：YYYY.MM.DD 姓名 (或 YYYY/MM/DD 姓名)
    # 正則表達式： (\d{4}[./]\d{2}[./]\d{2})\s+(.+)
    match_report = re.match(r"^(\d{4}[./]\d{2}[./]\d{2})\s+(.+)$", text)
    
    if match_report:
        date_str = match_report.group(1) # 日期是第一個捕獲組
        name_str = match_report.group(2).strip() # 人名是第二個捕獲組

        try:
            # 轉換分隔符號為點號，以便統一解析
            date_str = date_str.replace('/', '.') 
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
            print(f"LINE API reply failed (e.g., reply window expired). Error: {e}", file=sys.stderr)

# --- 啟動 Flask 應用 ---
if __name__ == "__main__":
    app.run(host='0.0.0.0', port=os.environ.get('PORT', 8080))