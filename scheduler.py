import os
import sys
import re
# 移除對 time 和 schedule 的依賴
from datetime import datetime, timedelta
# import schedule # <--- 移除
import psycopg2

# 引入 LINE Bot 相關
from linebot import LineBotApi
from linebot.exceptions import LineBotApiError
from linebot.models import TextSendMessage

# --- 姓名正規化工具 (從 app.py 複製過來，確保邏輯一致) ---
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
DATABASE_URL = os.environ.get('DATABASE_URL')
# NEW: 排除的群組ID列表 (用於跳過特定群組的提醒)
EXCLUDE_GROUP_IDS_STR = os.environ.get('EXCLUDE_GROUP_IDS', '')
EXCLUDE_GROUP_IDS = set(EXCLUDE_GROUP_IDS_STR.split(',')) if EXCLUDE_GROUP_IDS_STR else set()

# --- 診斷與初始化 ---
if not LINE_CHANNEL_ACCESS_TOKEN or not DATABASE_URL:
    # 這是 cron job 執行時的重要訊息
    print("FATAL ERROR: Missing required environment variables (LINE_CHANNEL_ACCESS_TOKEN or DATABASE_URL). Script exiting.", file=sys.stderr)
    line_bot_api = None
    # 這裡直接退出，避免後續程式碼執行
    sys.exit(1)
else:
    try:
        # 初始化 LINE Bot API
        line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
    except Exception as e:
        print(f"Failed to initialize LineBotApi in scheduler: {e}", file=sys.stderr)
        line_bot_api = None
        sys.exit(1)

# --- 資料庫連線函式 ---
def get_db_connection():
    """建立資料庫連線並返回連線物件。"""
    try:
        # 為了與 app.py 一致並確保安全連線
        conn = psycopg2.connect(DATABASE_URL, sslmode='require')
        return conn
    except Exception as e:
        print(f"DATABASE CONNECTION ERROR: {e}", file=sys.stderr)
        return None

# --- 排程核心函式 ---
def check_and_send_reminders(days_ago):
    """檢查指定日期的心得提交情況並發送催繳提醒。"""
    if days_ago not in (0, 1):
        print(f"Invalid days_ago parameter: {days_ago}. Must be 0 or 1.", file=sys.stderr)
        sys.exit(1)
        
    print(f"--- Scheduler check started for {days_ago} days ago. ---", file=sys.stderr)
    
    conn = get_db_connection()
    if not conn:
        print("Skipping reminder check due to database connection failure.", file=sys.stderr)
        return 

    try:
        with conn.cursor() as cursor:
            # 1. 取得所有需要提醒的群組 ID 及其 VIP 名單 (注意：這裡的資料庫查詢邏輯應該是從 reports 撈群組)
            # 由於 app.py 使用 reports 和 vips 兩個表，而 scheduler.py 使用 group_vips，這裡為了簡潔，
            # 假設您的資料庫有一個名為 group_vips 的 VIEW 或 TABLE 包含了 group_id 和 vip_list。
            # 為了和 app.py 的資料模型匹配 (vips 表)，我們應該改用 vips 表來獲取群組和 VIP。
            
            # (A) 查詢所有有 VIP 的群組 ID
            cursor.execute("SELECT DISTINCT group_id FROM vips;")
            group_ids = [row[0] for row in cursor.fetchall()]
            
            # 計算目標日期
            target_date = (datetime.utcnow() - timedelta(days=days_ago)).date()
            target_day_text = "昨日" if days_ago == 1 else "今日"
            
            # 提醒訊息結尾
            reminder_text_ending = "大家快來補交吧～\n\n不要逼系統變成奧客催款模式 😌"
            if days_ago == 0:
                 # 當天檢查可以給予更友善的提醒
                reminder_text_ending = "提醒各位貴賓，別忘了今日也要提交心得喔！\n\n（你的心得會讓我們更美好。）"


            for group_id in group_ids:
                if group_id in EXCLUDE_GROUP_IDS:
                    print(f"Skipping excluded group: {group_id}", file=sys.stderr)
                    continue

                # (B) 獲取該群組所有 VIP 名單
                cursor.execute(
                    "SELECT vip_name FROM vips WHERE group_id = %s;",
                    (group_id,)
                )
                # 這裡不再需要 normalize_name，因為它只在 log_report 和 scheduler 內部做比對。
                # VIP 名單應存儲正規化後的名稱。
                unique_normalized_vips = set(row[0] for row in cursor.fetchall())
                
                if not unique_normalized_vips:
                    print(f"Group {group_id} has no VIPs set. Skipping.", file=sys.stderr)
                    continue

                # (C) 取得目標日期該群組已提交心得的人名
                # 注意：reports 表中的 reporter_name 應儲存未正規化的名稱，但因為 log_report 允許非正規化名稱，
                # 因此這裡必須使用正規化後的名稱進行比對。
                cursor.execute(
                    "SELECT DISTINCT reporter_name FROM reports WHERE group_id = %s AND report_date = %s;",
                    (group_id, target_date)
                )
                submitted_names = {row[0] for row in cursor.fetchall()}
                submitted_normalized_names = {normalize_name(name) for name in submitted_names}

                # (D) 找出未交心得的人名 (使用正規化後的名稱進行比對)
                missing_normalized_reports = sorted([vip for vip in unique_normalized_vips if vip not in submitted_normalized_names])

                if missing_normalized_reports:
                    # 準備發送提醒訊息
                    list_of_names = "\n".join([f"- {name}" for name in missing_normalized_reports])
                    
                    # 訊息內容根據是檢查昨日還是今日來調整
                    message_text = (
                        f"📢 心得分享催繳大隊報到 📢\n"
                        f"日期: {target_date.strftime('%Y/%m/%d')} ({target_day_text})\n\n"
                        f"以下 VIP 仍未交心得：\n"
                        f"{list_of_names}\n\n"
                        f"{reminder_text_ending}"
                    )

                    try:
                        # 使用 PUSH 訊息發送提醒
                        line_bot_api.push_message(group_id, TextSendMessage(text=message_text))
                        print(f"Sent reminder to group {group_id} for {len(missing_normalized_reports)} missing reports for date {target_date}.", file=sys.stderr)
                    except LineBotApiError as e:
                        print(f"LINE API PUSH ERROR to {group_id}: {e}", file=sys.stderr)
                        
    except Exception as e:
        print(f"SCHEDULER DB/Logic ERROR: {e}", file=sys.stderr)
    finally:
        if conn: conn.close()
    
    print("--- Scheduler check finished. ---\n", file=sys.stderr)


# --- 執行排程主入口 (依賴 Cron Job 傳入的參數) ---
if __name__ == "__main__":
    # 預期 Cron Job 執行時傳入一個參數: 0 (檢查當日) 或 1 (檢查前一日)
    if len(sys.argv) != 2:
        print("Usage: python scheduler.py <days_ago: 0 or 1>", file=sys.stderr)
        sys.exit(1)
        
    try:
        days_ago = int(sys.argv[1])
        if days_ago not in (0, 1):
             raise ValueError("days_ago must be 0 or 1.")
        
        # 執行排程檢查
        check_and_send_reminders(days_ago)
        
    except ValueError as e:
        print(f"Invalid argument: {e}", file=sys.stderr)
        sys.exit(1)

    # 執行完畢，程序退出
    sys.exit(0)