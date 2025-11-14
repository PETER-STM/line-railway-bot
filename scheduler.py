import os
from datetime import datetime, timedelta
import psycopg2
from linebot import LineBotApi
from linebot.models import TextSendMessage
from dateutil.relativedelta import relativedelta, MO # 需要安裝 python-dateutil

# -----------------
# 1. 初始化設定與連線
# -----------------
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
TARGET_LINE_ID = os.environ.get("TARGET_LINE_ID") # 管理者或群組 ID，需要設定！

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)

# 假設所有要回報的人名清單 (請根據您的實際需求調整，或從資料庫/設定檔讀取)
ALL_REPORTERS = ["伊森", "小明", "陳先生"] 


def get_db_connection():
    """使用環境變數連線到 PostgreSQL"""
    conn_url = os.environ.get("DATABASE_URL")
    if not conn_url:
        # 使用 PG* 變數組合成連線字串
        conn_url = (
            f"postgresql://{os.environ.get('PGUSER')}:"
            f"{os.environ.get('PGPASSWORD')}@"
            f"{os.environ.get('PGHOST')}:"
            f"{os.environ.get('PGPORT')}/"
            f"{os.environ.get('PGDATABASE')}"
        )
    return psycopg2.connect(conn_url)


# -----------------
# 2. 結算邏輯
# -----------------

def calculate_last_week_period():
    """計算上一個完整的自然週 (上週一 ~ 上週日)"""
    today = datetime.now().date()
    # 上週日 (Last Sunday)
    last_sunday = today + relativedelta(weekday=MO(-1), days=-1)
    # 上週一 (Last Monday)
    last_monday = last_sunday + timedelta(days=-6)
    return last_monday, last_sunday

def get_reported_names(start_date, end_date):
    """查詢資料庫，獲取在指定日期範圍內有回報的人名"""
    conn = None
    reported_names = set()
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        sql = """
        SELECT DISTINCT name FROM reports 
        WHERE report_date BETWEEN %s AND %s
        """
        cur.execute(sql, (start_date, end_date))
        
        # 將結果轉換為人名集合
        reported_names = {row[0] for row in cur.fetchall()}
        
        cur.close()
    except Exception as e:
        print(f"查詢資料庫錯誤: {e}")
    finally:
        if conn:
            conn.close()
            
    return reported_names

def run_weekly_settlement():
    """執行每週結算並推送 Line 通知"""
    start_date, end_date = calculate_last_week_period()
    print(f"--- 結算週期: {start_date} ~ {end_date} ---")

    reported_names = get_reported_names(start_date, end_date)
    
    # 計算未回報名單
    missing_reporters = set(ALL_REPORTERS) - reported_names
    
    # 建立 Line 通知訊息
    if missing_reporters:
        missing_list = '\n'.join([f"- {name}" for name in sorted(list(missing_reporters))])
        
        message = (
            f"📢 **週期回報結算通知** ({start_date} ~ {end_date})\n\n"
            f"❌ **未回報名單 ({len(missing_reporters)}/{len(ALL_REPORTERS)} 人)：**\n"
            f"{missing_list}\n\n"
            "請以上人員盡快補交回報紀錄！"
        )
    else:
        message = f"🎉 **週期回報結算通知** ({start_date} ~ {end_date})\n\n✅ 所有 {len(ALL_REPORTERS)} 位成員均已完成回報！太棒了！"

    # 推送 Line 通知
    try:
        if TARGET_LINE_ID:
            line_bot_api.push_message(
                to=TARGET_LINE_ID,
                messages=TextSendMessage(text=message)
            )
            print("Line 通知已成功發送。")
        else:
            print("錯誤：TARGET_LINE_ID 未設定，無法發送 Line 通知。")
    except Exception as e:
        print(f"發送 Line 通知失敗: {e}")


if __name__ == "__main__":
    run_weekly_settlement()