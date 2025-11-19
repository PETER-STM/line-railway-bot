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

# --- 資料庫連線函式 ---
def get_db_connection():
    """建立資料庫連線並返回連線物件。"""
    try:
        conn = psycopg2.connect(DATABASE_URL)
        return conn
    except Exception as e:
        print(f"Database connection error: {e}", file=sys.stderr)
        return None

# --- 資料庫操作：紀錄心得提交 ---
def log_report(group_id, report_date, reporter_name):
    """
    將心得提交紀錄到資料庫。
    - 確保群組ID和正規化後的人名唯一性。
    """
    conn = get_db_connection()
    if not conn:
        return "資料庫連線失敗，請稍後再試。"

    normalized_name = normalize_name(reporter_name)

    # 檢查正規化後的名字是否為空 (理論上在 handle_message 中已檢查，但再做一層防護)
    if not normalized_name:
         return "你輸入的姓名無法被系統識別，請確認！"

    try:
        with conn.cursor() as cursor:
            # 1. 將群組 ID 和正規化後的 VIP 名稱加入 vip_list (若不存在)
            # 這樣可以收集到所有活躍的 VIP 名單
            cursor.execute(
                """
                INSERT INTO vip_list (group_id, vip_name, normalized_name)
                VALUES (%s, %s, %s)
                ON CONFLICT (group_id, normalized_name) DO NOTHING;
                """,
                (group_id, reporter_name, normalized_name) # 原始名稱和正規化名稱都存
            )
            
            # 2. 紀錄本次心得提交 (使用正規化名稱作為唯一性檢查)
            # 這裡使用 ON CONFLICT (group_id, report_date, normalized_name) DO UPDATE
            # 這樣如果重複提交，會自動更新 report_time 為最新的時間，但不會產生新的紀錄。
            cursor.execute(
                """
                INSERT INTO reports (group_id, report_date, reporter_name, normalized_name, report_time)
                VALUES (%s, %s, %s, %s, NOW())
                ON CONFLICT (group_id, report_date, normalized_name)
                DO UPDATE SET 
                    reporter_name = EXCLUDED.reporter_name, -- 即使名稱帶有前綴，也用最新的名稱更新
                    report_time = NOW();
                """,
                (group_id, report_date, reporter_name, normalized_name)
            )
        
        conn.commit()
        # 根據是否為當天提交來調整回覆訊息
        today = datetime.now().date()
        date_display = report_date.strftime('%Y/%m/%d')
        
        if report_date == today:
            reply_text = f"恭喜 🎉 {reporter_name}！\n{date_display} 的心得已為你閃電登錄完畢！\n\n（你的打卡速度快到連我都嚇了一跳呢。）"
        else:
            # 補交
            reply_text = f"補交成功 👏 {reporter_name}！\n{date_display} 的心得已補登完成！\n\n（雖然遲到，但總比沒有好，給你一個讚！👍）"

        return reply_text
        
    except psycopg2.Error as e:
        conn.rollback()
        print(f"Database operation error during log_report: {e}", file=sys.stderr)
        return "資料庫操作發生錯誤，請通知管理員檢查。"
    finally:
        if conn: conn.close()


# --- LINE Webhook 處理器 ---
@app.route("/callback", methods=['POST'])
def callback():
    """接收來自 LINE 的訊息並分發處理。"""
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    app.logger.info("Request body: " + body)
    
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        print("Invalid signature. Please check your channel access token/secret.")
        abort(400)
    except LineBotApiError as e:
        print(f"LINE API Error: {e}")
        abort(500)

    return 'OK'

# --- 訊息處理邏輯 ---
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    """處理收到的文字訊息。"""
    text = event.message.text.strip()
    reply_text = None
    group_id = None

    # 確保訊息來自群組或聊天室 (或用戶本身)
    if isinstance(event.source, SourceGroup):
        group_id = event.source.group_id
    elif isinstance(event.source, SourceRoom):
        group_id = event.source.room_id
    elif isinstance(event.source, SourceUser):
        # 允許在個人聊天中測試，但使用一個固定的 ID
        group_id = event.source.user_id 
    
    if not group_id:
        reply_text = "⚠️ 系統無法識別聊天來源 ID，請確認是否在群組/聊天室中使用。"
    
    # 檢查是否為要排除的群組
    if group_id in EXCLUDE_GROUP_IDS:
        print(f"Message received from excluded group: {group_id}. Skipping processing.", file=sys.stderr)
        return # 跳過此群組的處理

    # 檢查是否為心得回報格式 (YYYY.MM.DD(週幾) 姓名 或 YYYY.MM.DD 姓名)
    # Group 1: Date. Group 2: Name. (Day part is non-capturing)
    # 新正則表達式允許日期後緊跟 (週幾/星期幾/週日/週天 等)，並將其排除在姓名之外
    match_report = re.match(r"^(\d{4}[./]\d{2}[./]\d{2})\s*(?:[（(][週星]?[一二三四五六日天][)）])?\s*(.+)$", text)
    
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
            print(f"LINE API reply_message failed, attempting push_message: {e}", file=sys.stderr)
            try:
                line_bot_api.push_message(group_id, TextSendMessage(text=reply_text))
            except LineBotApiError as e_push:
                print(f"LINE API PUSH ERROR: {e_push}", file=sys.stderr)


# --- Flask 啟動 ---
if __name__ == "__main__":
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)