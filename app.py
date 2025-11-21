import os
import sys
import re
from datetime import datetime, timedelta
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError, LineBotApiError, LineBotApiError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, SourceGroup, SourceRoom, SourceUser
import psycopg2
# from google import genai # 暫時不需要，保留以備未來擴展 AI 功能

# --- 姓名正規化工具 (用於確保 VIP 記錄唯一性，並解決重複名稱問題) ---
def normalize_name(name):
    """
    對人名進行正規化處理，主要移除開頭的班級或編號標記。
    例如: "(三) 浣熊🦝" -> "浣熊🦝"
    """
    # 移除開頭被括號 (圓括號、全形括號、方括號、書名號) 包裹的內容，例如 (三), (二), 【1】, [A]
    # 匹配模式: ^(起始) + 任意空白 + 括號開頭 + 非括號內容(1到10個) + 括號結尾 + 任意空白
    normalized = re.sub(r'^\s*[（(\[【][^()\[\]]{1,10}[)）\]】]\s*', '', name).strip()
    
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

# --- 活潑・幽默・微毒舌 回覆模板 ---
UNKNOWN_ERROR_TEXT = (
    "💥 發生未知錯誤。\n"
    "可能是宇宙磁場不順，或系統在叛逆。\n"
    "稍後再試，或找管理員用愛（或一包綠色包裝的乖乖）感化它。"
)

# --- 資料庫連線函式 ---
def get_db_connection():
    # 使用 DATABASE_URL 進行連線
    # dsn 格式: postgresql://user:password@host:port/dbname
    conn = None
    try:
        # 使用 DSN 格式連線
        conn = psycopg2.connect(DATABASE_URL)
        return conn
    except Exception as e:
        # 在錯誤發生時印出訊息到標準錯誤，方便日誌追蹤
        print(f"Database connection error: {e}", file=sys.stderr)
        return None

# --- 資料庫操作函式 (新增/刪除/查詢 VIP) ---

def add_vip_to_group(group_id, name):
    """將 VIP 名稱新增到群組的 VIP 名單中。"""
    conn = get_db_connection()
    if not conn: return "❌ 資料庫連線失敗，請稍後再試。"

    try:
        with conn.cursor() as cursor:
            # 檢查 VIP 是否已存在
            cursor.execute(
                "SELECT COUNT(*) FROM group_vips WHERE group_id = %s AND vip_name = %s;",
                (group_id, name)
            )
            if cursor.fetchone()[0] > 0:
                return f"⚠️ {name} 已經在 VIP 名單中了！\\n\\n（不要重複加啦，很佔空間耶。）"

            # 新增 VIP
            cursor.execute(
                "INSERT INTO group_vips (group_id, vip_name, normalized_vip_name) VALUES (%s, %s, %s);",
                (group_id, name, normalize_name(name))
            )
            conn.commit()
            return f"✅ 成功將 {name} 加入 VIP 名單！\\n\\n（恭喜你，現在你有準時交心得的義務了！）"

    except Exception as e:
        print(f"DB Error (add_vip_to_group): {e}", file=sys.stderr)
        return UNKNOWN_ERROR_TEXT  # 使用新的錯誤訊息
    finally:
        if conn: conn.close()

def remove_vip_from_group(group_id, name):
    """從群組的 VIP 名單中移除指定名稱。"""
    conn = get_db_connection()
    if not conn: return "❌ 資料庫連線失敗，請稍後再試。"

    # 必須使用正規化後的名稱來刪除，以匹配潛在的錯誤輸入
    normalized_name_to_remove = normalize_name(name)

    try:
        with conn.cursor() as cursor:
            # 嘗試使用正規化名稱進行刪除，這會刪除所有匹配正規化名稱的原始記錄
            cursor.execute(
                "DELETE FROM group_vips WHERE group_id = %s AND normalized_vip_name = %s;",
                (group_id, normalized_name_to_remove)
            )
            rows_deleted = cursor.rowcount
            conn.commit()

            if rows_deleted > 0:
                return f"🗑️ 成功將 {name} (及其所有變體) 從 VIP 名單中移除！\\n\\n（雖然你逃了，但你的心得債不會消失！）"
            else:
                return f"🧐 名單中找不到 {name} 耶。\\n\\n（確定你打對字了嗎？）"

    except Exception as e:
        print(f"DB Error (remove_vip_from_group): {e}", file=sys.stderr)
        return UNKNOWN_ERROR_TEXT  # 使用新的錯誤訊息
    finally:
        if conn: conn.close()


def list_vips_in_group(group_id):
    """列出群組中的所有 VIP 名稱。"""
    conn = get_db_connection()
    if not conn: return "❌ 資料庫連線失敗，請稍後再試。"

    try:
        with conn.cursor() as cursor:
            # 查詢所有 VIP 名稱，並根據正規化名稱去重，然後按正規化名稱排序
            # 使用 sub-query 找到每個 unique normalized name 對應的一個原始名稱作為代表
            # 但最簡單的做法是直接取出所有原始名稱並在 Python 中處理去重和排序
            cursor.execute(
                "SELECT DISTINCT vip_name, normalized_vip_name FROM group_vips WHERE group_id = %s ORDER BY normalized_vip_name, vip_name;",
                (group_id,)
            )
            # 為了避免顯示重複的 VIP（例如有人用 "(1) 某某" 和 "某某"），
            # 我們應該在 Python 中根據 normalized_vip_name 去重。
            unique_vips = {}
            for vip_name, normalized_name in cursor.fetchall():
                 # 以 normalized_name 為鍵，但顯示時用第一個遇到的 vip_name
                if normalized_name not in unique_vips:
                    unique_vips[normalized_name] = vip_name
            
            vip_list = sorted(list(unique_vips.values()))

            if not vip_list:
                return "😮 VIP 名單目前是空的耶。\\n\\n（快把人加進來啦，不然心得催繳大隊要催誰？）"

            # 格式化輸出
            list_of_names = "\\n".join([f"- {name}" for name in vip_list])
            reply_text = (
                f"🌟 VIP 名單 ({len(vip_list)} 位) 🌟\\n"
                f"{list_of_names}\\n\\n"
                f"（沒在名單上的人，記得找管理員把你加進來喔！）"
            )
            return reply_text

    except Exception as e:
        print(f"DB Error (list_vips_in_group): {e}", file=sys.stderr)
        return UNKNOWN_ERROR_TEXT  # 使用新的錯誤訊息
    finally:
        if conn: conn.close()

def log_report(group_id, report_date, reporter_name):
    """記錄心得分享/打卡資訊。"""
    conn = get_db_connection()
    if not conn: return "❌ 資料庫連線失敗，請稍後再試。"
    
    normalized_name = normalize_name(reporter_name)

    try:
        with conn.cursor() as cursor:
            # 1. 檢查這個正規化後的人名是否在 VIP 名單中
            cursor.execute(
                "SELECT vip_name FROM group_vips WHERE group_id = %s AND normalized_vip_name = %s LIMIT 1;",
                (group_id, normalized_name)
            )
            is_vip = cursor.fetchone()

            if not is_vip:
                # 不在 VIP 名單，提醒使用者
                return (
                    f"🧐 系統找不到 {reporter_name} 在 VIP 名單中。\\n\\n"
                    f"請先請管理員用指令： `加VIP {reporter_name}` 把你加進來喔！\\n"
                    f"（不然系統會假裝沒看到你交的心得... 😏）"
                )

            # 2. 檢查是否已經提交過心得
            cursor.execute(
                "SELECT id FROM reports WHERE group_id = %s AND report_date = %s AND normalized_reporter_name = %s LIMIT 1;",
                (group_id, report_date, normalized_name)
            )
            if cursor.fetchone():
                # 已提交
                return f"🤫 {reporter_name} 你今天的心得 ({report_date}) 已經交過了啦！\\n\\n（系統記性很好的，不用重複提醒。）"

            # 3. 記錄心得
            # 由於 LINE 訊息本身沒有內容，我們只記錄打卡資訊 (日期, 人名, 群組)
            cursor.execute(
                "INSERT INTO reports (group_id, report_date, reporter_name, normalized_reporter_name) VALUES (%s, %s, %s, %s);",
                (group_id, report_date, reporter_name, normalized_name)
            )
            conn.commit()

            # 4. 根據日期判斷回覆訊息
            today = datetime.now().date()
            if report_date == today:
                return f"💯 幹得漂亮！{reporter_name} 成功提交今日心得！\\n\\n（系統為你的自律感到驕傲！）"
            elif report_date == today - timedelta(days=1):
                return f"👍 補交成功！{reporter_name} 補上了昨日 ({report_date}) 的心得！\\n\\n（雖然遲到，但總比沒有好！）"
            elif report_date < today:
                return f"🤔 {reporter_name} 補交了 {report_date} 的心得。\\n\\n（這日子有點久遠了喔...）"
            else: # 未來的日期
                return f"🔮 預知未來嗎？{reporter_name} 提交了 {report_date} 的心得。\\n\\n（時空旅人，請接受系統的膜拜！）"

    except Exception as e:
        print(f"DB Error (log_report): {e}", file=sys.stderr)
        return UNKNOWN_ERROR_TEXT  # 使用新的錯誤訊息
    finally:
        if conn: conn.close()


# --- LINE 事件處理 ---

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    """處理收到的文字訊息事件。"""
    # 僅處理群組/聊天室訊息，且排除設定中列出的群組 (用於測試排除)
    if not isinstance(event.source, (SourceGroup, SourceRoom, SourceUser)):
        return

    group_id = None
    if isinstance(event.source, (SourceGroup, SourceRoom)):
        group_id = event.source.group_id if isinstance(event.source, SourceGroup) else event.source.room_id
    elif isinstance(event.source, SourceUser):
         # 允許在個人聊天中測試，將 group_id 設為 user_id
        group_id = event.source.user_id 
    
    if group_id in EXCLUDE_GROUP_IDS:
        print(f"Ignoring message from excluded group/user: {group_id}", file=sys.stderr)
        return

    text = event.message.text.strip()
    reply_text = None

    # --- 指令處理 ---

    # 1. 查詢 VIP 名單指令
    if text in ["查VIP", "列出VIP", "名單", "誰是VIP"]:
        reply_text = list_vips_in_group(group_id)

    # 2. 新增 VIP 指令 (加VIP 姓名)
    elif text.startswith("加VIP"):
        parts = text.split(maxsplit=1)
        if len(parts) == 2 and parts[1].strip():
            name_to_add = parts[1].strip()
            reply_text = add_vip_to_group(group_id, name_to_add)
        else:
            reply_text = "🤷‍♀️ 請問想加誰進 VIP 名單？\\n\\n請使用格式： `加VIP 姓名`"
    
    # 3. 移除 VIP 指令 (減VIP 姓名)
    elif text.startswith("減VIP") or text.startswith("移除VIP"):
        parts = text.split(maxsplit=1)
        if len(parts) == 2 and parts[1].strip():
            name_to_remove = parts[1].strip()
            reply_text = remove_vip_from_group(group_id, name_to_remove)
        else:
            reply_text = "🤷‍♀️ 請問想移除誰出 VIP 名單？\\n\\n請使用格式： `減VIP 姓名`"
    
    # --- 心得回報/打卡處理 (YYYY.MM.DD 姓名 或 YYYY/MM/DD 姓名) ---
    
    # 正則表達式： (\d{4}[./]\d{2}[./]\d{2})\s+(.+)$
    # 捕獲日期 (允許 . 或 / 作為分隔符) 和後面的所有文字 (作為姓名)
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
                reply_text = "⚠️ 日期後面請記得加上人名，不然我不知道誰交的啊！\\n\\n（你總不會想讓我自己猜吧？）"
            else:
                # 呼叫 log_report，只記錄打卡資訊
                reply_text = log_report(group_id, report_date, reporter_name)
            
        except ValueError:
            # 記錄回報 (日期格式錯誤) 模板
            reply_text = "❌ 日期長得怪怪的。\\n\\n請用標準格式：YYYY.MM.DD 姓名\\n\\n（小數點不是你的自由發揮。）"
        
        # NOTE: 此處不添加通用的 try/except，因為日期和人名錯誤都已有明確的回覆。

    # 發送回覆訊息 (這是對使用者的指令回覆，不是催繳訊息)
    if reply_text:
        try:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=reply_text)
            )
        except LineBotApiError as e:
            # 如果 reply_message 失敗，嘗試 push_message (例如：超過 3 秒回覆期限)
            print(f"LINE API PUSH/REPLY ERROR: {e}", file=sys.stderr)
            # 這裡不發送 UNKNOWN_ERROR_TEXT，因為這通常是 LINE API 限制問題，不是內部邏輯錯誤。

# --- Webhook 主入口 ---
@app.route("/callback", methods=['POST'])
def callback():
    # 獲取 X-Line-Signature header value
    signature = request.headers.get('X-Line-Signature', '')
    # 獲取 request body as text
    body = request.get_data(as_text=True)
    app.logger.info("Request body: " + body)

    # 處理 webhook body
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        print("Invalid signature. Please check your channel access token/secret.", file=sys.stderr)
        abort(400)
    except Exception as e:
        # 捕捉所有未預期的錯誤，僅記錄，不嘗試回覆（因為 reply_token 可能已失效）
        print(f"General Error during webhook handling: {e}", file=sys.stderr)
        pass 

    return 'OK'


# --- 啟動 Flask 應用 (通常用於本地測試) ---
if __name__ == "__main__":
    port = int(os.environ.get('PORT', 8080))
    # 確保在生產環境中不運行此區塊，除非是單獨運行的應用程式
    # 在 Railway/Heroku/Gunicorn 環境中，這段不會執行
    print(f"Starting Flask app on port {port}", file=sys.stderr)
    # app.run(host='0.0.0.0', port=port, debug=False) # 註釋掉，因為通常使用 gunicorn