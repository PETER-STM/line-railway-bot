import os
import sys
import re
from datetime import datetime, timedelta
import psycopg2
import argparse # 新增：用於處理命令列參數

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
    # 移除開頭被括號 (圓括號、全形括號、方括號、書名號) 包裹的內容
    # 匹配模式: ^(起始) + 任意空白 + 括號開頭 + 非括號內容(1到10個) + 括號結尾 + 任意空白
    normalized = re.sub(r'^\s*[\(（\[【][^()\[\]]{1,10}[\)）\]】]\s*', '', name).strip()
    
    # 如果正規化結果為空，返回原始名稱
    return normalized if normalized else name

# --- 環境變數設定 ---
# 確保環境變數已設置，否則腳本會立即退出
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
        # 由於 Railway 的 DATABASE_URL 已經包含所有連線資訊
        conn = psycopg2.connect(DATABASE_URL)
        return conn
    except Exception as e:
        print(f"Database connection error: {e}", file=sys.stderr)
        return None

# --- 排程任務邏輯 ---
def check_and_send_reminders(days_ago=1):
    """
    檢查指定日期前應提交但未提交心得的 VIP，並發送提醒訊息。
    - days_ago=1 檢查昨日 (補交提醒)
    - days_ago=0 檢查今日 (當日提醒)
    """
    conn = None
    try:
        conn = get_db_connection()
        if not conn: return

        cursor = conn.cursor()

        # 根據 days_ago 計算目標日期 (以 UTC 時間為準，但資料庫和報告日期都是日期格式，所以計算方式一樣)
        target_date = (datetime.utcnow().date() - timedelta(days=days_ago))
        
        # 根據 days_ago 設定訊息文字
        if days_ago == 1:
            target_day_text = "昨日"
            reminder_text_ending = "大家快來補交吧～\\n\\n不要逼系統變成奧客催款模式 😌"
        elif days_ago == 0:
            target_day_text = "今日"
            reminder_text_ending = "請各位 VIP 記得在期限內提交！\\n\\n不然會被補交大隊追殺喔 🔪"
        else:
             # 不應該發生
             print(f"ERROR: Invalid days_ago value: {days_ago}", file=sys.stderr)
             return

        # 1. 取得所有活躍的群組 ID
        cursor.execute("SELECT DISTINCT group_id FROM vips_list;")
        all_group_ids = [row[0] for row in cursor.fetchall()]

        # 2. 針對每個群組檢查
        for group_id in all_group_ids:
            
            if group_id in EXCLUDE_GROUP_IDS:
                print(f"Skipping group {group_id} due to EXCLUDE_GROUP_IDS setting.", file=sys.stderr)
                continue

            # 2a. 取得該群組的 VIP 名單
            cursor.execute(
                "SELECT reporter_name FROM vips_list WHERE group_id = %s;",
                (group_id,)
            )
            all_vips = [row[0] for row in cursor.fetchall()]
            
            # 將 VIP 名單正規化，用於比對
            unique_normalized_vips = sorted(list(set(normalize_name(vip) for vip in all_vips)))
            
            if not unique_normalized_vips:
                print(f"Warning: No VIPs defined for group {group_id}. Skipping.", file=sys.stderr)
                continue
                
            # 2b. 取得目標日期該群組已提交心得的人名 (正規化後)
            cursor.execute(
                "SELECT DISTINCT reporter_name FROM reports WHERE group_id = %s AND report_date = %s;",
                (group_id, target_date)
            )
            submitted_names = [row[0] for row in cursor.fetchall()]
            submitted_normalized_names = {normalize_name(name) for name in submitted_names}

            # 2c. 找出未交心得的人名 (根據正規化後的名稱)
            # 只有當正規化後的 VIP 不在正規化後的已提交名單中，才算遺漏
            missing_normalized_reports = sorted([vip for vip in unique_normalized_vips if vip not in submitted_normalized_names])

            if missing_normalized_reports:
                # 準備發送提醒訊息
                list_of_names = "\\n".join([f"- {name}" for name in missing_normalized_reports])
                
                # 訊息內容根據是檢查昨日還是今日來調整
                message_text = (
                    f"📢 心得分享催繳大隊報到 📢\\n"
                    f"日期: {target_date.strftime('%Y/%m/%d')} ({target_day_text})\\n\\n"
                    f"以下 VIP 仍未交心得：\\n"
                    f"{list_of_names}\\n\\n"
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
    
    print(f"--- Scheduler check for days_ago={days_ago} finished. ---\\n", file=sys.stderr)

# --- 主程式執行區塊 (只執行一次) ---
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Cron-based scheduler for sending reminders.")
    # 定義 --days-ago 參數，預設為 1 (檢查昨日)
    parser.add_argument(
        '--days-ago', 
        type=int, 
        default=1, 
        help='Number of days ago to check (1 for yesterday, 0 for today).'
    )
    args = parser.parse_args()
    
    # 執行一次檢查函式
    check_and_send_reminders(args.days_ago)