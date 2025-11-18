import os
import sys
import time
from datetime import datetime, timedelta
import schedule 
import psycopg2

# 引入 LINE Bot 相關
from linebot import LineBotApi
from linebot.exceptions import LineBotApiError
from linebot.models import TextSendMessage

# --- 環境變數設定 ---
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
DATABASE_URL = os.environ.get('DATABASE_URL')

# NEW: 排除的群組ID列表 (用於跳過特定群組的提醒)
EXCLUDE_GROUP_IDS_STR = os.environ.get('EXCLUDE_GROUP_IDS', '')
EXCLUDE_GROUP_IDS = set(EXCLUDE_GROUP_IDS_STR.split(',')) if EXCLUDE_GROUP_IDS_STR else set()

# --- 診斷與初始化 ---
if not LINE_CHANNEL_ACCESS_TOKEN or not DATABASE_URL:
    print("ERROR: Missing required environment variables for scheduler! Cannot start worker.", file=sys.stderr)
    line_bot_api = None 
else:
    try:
        # 初始化 LINE Bot API
        line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
    except Exception as e:
        print(f"Failed to initialize LineBotApi in scheduler: {e}", file=sys.stderr)
        line_bot_api = None

# --- 資料庫連線函式 ---
def get_db_connection():
    """建立資料庫連線"""
    try:
        return psycopg2.connect(DATABASE_URL)
    except Exception as e:
        print(f"DATABASE CONNECTION ERROR: {e}", file=sys.stderr)
        return None

# --- 資料庫初始化函式 ---
def ensure_tables_exist():
    """檢查並建立所有必需的資料庫表 (group_reporters, reports, settings)"""
    conn = get_db_connection()
    if conn is None:
        print("ERROR: Failed to establish database connection for table creation in scheduler.", file=sys.stderr)
        return False
    
    cur = conn.cursor()
    success = True
    try:
        # 1. group_reporters 表 (存放群組ID和成員姓名)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS group_reporters (
                group_id VARCHAR(255) NOT NULL,
                reporter_name VARCHAR(255) NOT NULL,
                PRIMARY KEY (group_id, reporter_name)
            );
        """)
        
        # 2. reports 表 (存放每日回報紀錄)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS reports (
                group_id VARCHAR(255) NOT NULL,
                report_date DATE NOT NULL,
                reporter_name VARCHAR(255) NOT NULL,
                PRIMARY KEY (group_id, report_date, reporter_name)
            );
        """)
        
        # 3. settings 表 (存放全域設定，例如提醒是否暫停)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key VARCHAR(255) PRIMARY KEY,
                value VARCHAR(255) NOT NULL
            );
        """)
        
        conn.commit()
        print("INFO: Scheduler DB tables checked/created successfully.", file=sys.stderr)
    except Exception as e:
        print(f"SCHEDULER DATABASE INITIALIZATION ERROR: {e}", file=sys.stderr)
        conn.rollback()
        success = False
    finally:
        if conn: conn.close()
    
    return success

# --- 全域設定函式 ---
def is_global_pause_state(conn) -> bool:
    """檢查全域提醒是否暫停，使用傳入的連線"""
    cur = conn.cursor()
    try:
        cur.execute("SELECT value FROM settings WHERE key = 'is_paused';")
        result = cur.fetchone()
        if result and result[0].lower() == 'true':
            return True
        return False
    except Exception as e:
        # 如果表不存在或發生其他錯誤，會被 ensure_tables_exist() 處理
        print(f"DB CHECK ERROR (is_global_pause_state in scheduler): {e}", file=sys.stderr)
        return False

# --- 每日提醒檢查核心邏輯 ---
def check_daily_reminder():
    """
    主要執行函式，在每日排程時間執行。
    檢查所有群組昨天的心得回報狀態，並對未回報者發送提醒。
    """
    # 確保 Bot API 初始化成功
    if not line_bot_api:
        print("ERROR: LineBotApi is not initialized. Skipping check.", file=sys.stderr)
        return

    # 確保資料庫表存在
    if not ensure_tables_exist():
        print("ERROR: Database tables are not available. Skipping check.", file=sys.stderr)
        return
        
    conn = get_db_connection()
    if conn is None:
        print("ERROR: Database connection failed in scheduler. Skipping check.", file=sys.stderr)
        return

    cur = conn.cursor()

    try:
        # 1. NEW: 檢查全域提醒是否暫停
        if is_global_pause_state(conn):
            print("INFO: Global reminder is PAUSED. Skipping all groups.", file=sys.stderr)
            return
            
        # 2. 確定要檢查的日期 (昨天)
        yesterday_date = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
        yesterday_display = (datetime.now() - timedelta(days=1)).strftime('%Y.%m.%d')
        print(f"INFO: Starting daily check for reports on {yesterday_date}", file=sys.stderr)

        # 3. 取得所有有註冊人名的群組 ID
        cur.execute("SELECT DISTINCT group_id FROM group_reporters;")
        all_group_ids = [r[0] for r in cur.fetchall()]
        
        # 4. 逐一處理每個群組
        for group_id in all_group_ids:
            
            # 排除不提醒的群組
            if group_id in EXCLUDE_GROUP_IDS:
                print(f"INFO: Skipping excluded group: {group_id}", file=sys.stderr)
                continue
            
            # 取得該群組所有應回報的人員
            cur.execute("SELECT reporter_name FROM group_reporters WHERE group_id = %s;", (group_id,))
            all_reporters = {r[0] for r in cur.fetchall()}

            if not all_reporters:
                print(f"INFO: Group {group_id} has no registered reporters. Skipping.", file=sys.stderr)
                continue

            # 取得昨天已回報的人員
            cur.execute("SELECT reporter_name FROM reports WHERE group_id = %s AND report_date = %s;", 
                        (group_id, yesterday_date))
            reported_reporters = {r[0] for r in cur.fetchall()}
            
            # 計算未回報人員
            missing_reports = sorted(list(all_reporters - reported_reporters))

            if missing_reports:
                # --- 建立提醒訊息模板 ---
                missing_names = "\n🔸 ".join(missing_reports)
                
                message_text = f"📢 **昨日心得追蹤提醒 ({yesterday_display})**\n\n"
                
                if len(missing_reports) == 1:
                    # 單人提醒
                    message_text += f"⚠️ **{missing_reports[0]}**，你的心得還沒交喔！\n\n"
                    message_text += "💡 快交上來吧，別讓我每天都在追著你問～\n\n"
                    message_text += "期待看到你的 心得分享，別讓我一直盯著這份名單 😏"
                else:
                    # 多人提醒
                    message_text += f"🚨 以下 {len(missing_reports)} 位成員尚未完成回報：\n\n🔸 {missing_names}\n\n"
                    message_text += "📌 小提醒：再不交心得，我的 咚錢模式就要開啟啦💸\n"
                    message_text += "💡 快交上來吧，別讓我每天都在追著你們問～\n\n"
                    message_text += "期待看到你們的 心得分享，別讓我一直盯著這份名單 😏"
                # --- 模板結束 ---
                
                try:
                    # 使用 PUSH 訊息發送提醒
                    line_bot_api.push_message(group_id, TextSendMessage(text=message_text))
                    print(f"Sent reminder to group {group_id} for {len(missing_reports)} missing reports.", file=sys.stderr)
                except LineBotApiError as e:
                    print(f"LINE API PUSH ERROR to {group_id}: {e}", file=sys.stderr)
                
    except Exception as e:
        print(f"SCHEDULER DB/Logic ERROR: {e}", file=sys.stderr)
    finally:
        if conn: conn.close()
    
    print("--- Scheduler check finished. ---", file=sys.stderr)

# --- 排程設定與執行 ---
if line_bot_api:
    # 確保資料庫在啟動時被初始化
    ensure_tables_exist() 
    
    # 設定每天在 UTC 01:00 執行檢查 (對應台灣時間 TST 09:00)
    schedule.every().day.at("01:00").do(check_daily_reminder)

    print("INFO: Scheduler worker is running. Next check at 01:00 UTC.", file=sys.stderr)
    while True:
        schedule.run_pending()
        time.sleep(1)
else:
    print("WARNING: Scheduler failed to start due to missing config or LineBotApi initialization error.", file=sys.stderr)