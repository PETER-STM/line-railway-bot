import os
import sys
import re # 需要正規化函式
from datetime import datetime, timedelta
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
    # 注意: 這個正則表達式假設 app.py 中是使用這個邏輯進行人名正規化的。
    normalized = re.sub(r'^\s*[\(（\[【][^()\[\]]{1,10}[\)）\]】]\s*', '', name).strip()
    
    # 如果正規化結果為空，返回原始名稱
    return normalized if normalized else name

# --- 環境變數設定 ---
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
DATABASE_URL = os.environ.get('DATABASE_URL')
# 從 Railway Cron Job 環境變數讀取工作類型 (MORNING 或 EVENING)
JOB_TYPE = os.environ.get('JOB_TYPE') 

# NEW: 排除的群組ID列表 (用於跳過特定群組的提醒)
EXCLUDE_GROUP_IDS_STR = os.environ.get('EXCLUDE_GROUP_IDS', '')
EXCLUDE_GROUP_IDS = set(EXCLUDE_GROUP_IDS_STR.split(',')) if EXCLUDE_GROUP_IDS_STR else set()

# --- 診斷與初始化 ---
# 檢查三個必要的環境變數，JOB_TYPE 是 Cron Job 必須提供的
if not LINE_CHANNEL_ACCESS_TOKEN or not DATABASE_URL or not JOB_TYPE:
    print("FATAL ERROR: Missing required environment variables (LINE_CHANNEL_ACCESS_TOKEN, DATABASE_URL, or JOB_TYPE). Script exiting.", file=sys.stderr)
    sys.exit(1)

try:
    # 初始化 LINE Bot API
    line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
except Exception as e:
    print(f"Failed to initialize LineBotApi in scheduler: {e}", file=sys.stderr)
    sys.exit(1)

# --- 資料庫連線函式 ---
def get_db_connection():
    """建立資料庫連線"""
    try:
        # 使用sslmode='require'確保連線安全
        conn = psycopg2.connect(DATABASE_URL, sslmode='require') 
        return conn
    except Exception as e:
        print(f"DB CONNECTION ERROR: {e}", file=sys.stderr)
        return None

# --- 核心功能：檢查並發送提醒 (單次執行) ---
def main_check_and_send_reminders(job_type):
    """
    檢查所有群組中，針對指定日期尚未繳交心得的成員，並發送 LINE 提醒。

    Args:
        job_type (str): 'MORNING' (檢查昨日，對應 TST 09:00 Cron) 
                      或 'EVENING' (檢查今日，對應 TST 21:00 Cron)
    """
    print(f"--- Scheduler check started (Job Type: {job_type}). ---", file=sys.stderr)
    
    conn = None
    # 由於 Railway 服務器通常使用 UTC 時間，我們使用 UTC 日期作為資料庫查詢的基準
    # 確保與 app.py 儲存報告時使用的日期邏輯一致
    today_utc = datetime.utcnow().date() 

    if job_type == 'MORNING':
        # 09:00 TST (UTC 01:00) 執行: 檢查【昨天】的打卡
        target_date = today_utc - timedelta(days=1)
        target_day_text = "昨日"
        # 增加提示：早上 9 點是最後期限
        reminder_text_ending = "趕快把昨天的補上！系統會紀錄的喔 👀"
    elif job_type == 'EVENING':
        # 21:00 TST (UTC 13:00) 執行: 檢查【今天】的打卡
        target_date = today_utc
        target_day_text = "今日"
        # 增加提示：晚上 9 點的提醒，還有時間完成今日任務
        reminder_text_ending = "今天還沒結束，快點去完成吧！"
    else:
        print(f"Unknown JOB_TYPE received: {job_type}. Script exiting.", file=sys.stderr)
        return

    try:
        conn = get_db_connection()
        if not conn:
            print("Skipping reminder check due to DB connection failure.", file=sys.stderr)
            return

        cursor = conn.cursor()

        # 1. 取得所有有回報者在名單上的群組 ID
        cursor.execute("SELECT DISTINCT group_id FROM reporters;")
        all_group_ids = [row[0] for row in cursor.fetchall()]

        # 過濾掉被排除的群組 ID
        groups_to_check = [gid for gid in all_group_ids if gid not in EXCLUDE_GROUP_IDS]

        for group_id in groups_to_check:
            # 2. 取得該群組的完整回報者名單 (正規化並去重)
            # reporters 表中儲存的是原始名稱
            cursor.execute(
                "SELECT name FROM reporters WHERE group_id = %s ORDER BY name",
                (group_id,)
            )
            original_names = [row[0] for row in cursor.fetchall()]

            if not original_names:
                continue

            # 這是 VIP 名單 (期望應該回報的人)，且必須是正規化後的名字
            unique_normalized_vips = {name for name in [normalize_name(n) for n in original_names] if name}
            
            if not unique_normalized_vips:
                continue

            # 3. 取得該群組在目標日期已提交報告的【原始】人名
            # daily_reports 表中儲存的是原始名稱
            cursor.execute(
                "SELECT reporter_name FROM daily_reports WHERE group_id = %s AND report_date = %s;",
                (group_id, target_date)
            )
            # 將已提交的原始名稱正規化並去重
            submitted_normalized_names = {name for name in [normalize_name(row[0]) for row in cursor.fetchall()] if name}

            # 4. 找出未交心得的【正規化】人名
            # 只有當正規化後的 VIP 不在正規化後的已提交名單中，才算遺漏
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
    
    print("--- Scheduler check finished. ---", file=sys.stderr)


# --- 主執行區塊 (Cron Job 每次執行只跑一次) ---
if __name__ == "__main__":
    main_check_and_send_reminders(JOB_TYPE)