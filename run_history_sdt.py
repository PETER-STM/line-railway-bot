# run_history_sdt.py (V32.16 不死鳥版)
import os
import sys
import time
import psycopg2
from dotenv import load_dotenv

load_dotenv()

from ai_service import evaluate_sdt_state

# 從 .env 取得連線資訊
DATABASE_URL = os.getenv("DATABASE_URL")

def get_fresh_conn():
    """建立全新的資料庫連線"""
    return psycopg2.connect(DATABASE_URL, sslmode='require')

def backfill_historical_data():
    print("🚀 啟動歷史日報 SDT 批量回溯 (不死鳥重連版)...")
    
    conn = get_fresh_conn()
    
    try:
        while True: # 外層迴圈，支援斷線重跑
            try:
                with conn.cursor() as cur:
                    # 1. 撈出下一批待處理的資料 (每次只取 1 筆，最保險)
                    cur.execute("""
                        SELECT id, report_content 
                        FROM reports 
                        WHERE sdt_c IS NULL AND report_content IS NOT NULL 
                        ORDER BY id DESC LIMIT 1
                    """)
                    record = cur.fetchone()
                    
                    if not record:
                        print("✨ 所有資料都已經量化完畢！")
                        break
                        
                    report_id, text = record
                    clean_text = str(text).strip() if text else ""
                    
                    # 2. 呼叫 AI 打分
                    print(f"🔄 正在量化 ID:{report_id}...", end="\r")
                    try:
                        _, sdt_string, (a, c, r) = evaluate_sdt_state(clean_text)
                        
                        if a == 0.5 and c == 0.5 and r == 0.5:
                            # 為了不卡死，0.5 的爛資料我們標記為 0.51 作為標記，下次跳過
                            cur.execute("UPDATE reports SET sdt_a=0.51, sdt_c=0.51, sdt_r=0.51 WHERE id=%s", (report_id,))
                            conn.commit()
                            print(f"⚠️ ID:{report_id} 掉入陷阱，標記為 0.51 跳過。")
                            continue
                            
                    except Exception as e:
                        print(f"\n❌ AI 呼叫異常: {e}")
                        time.sleep(5)
                        continue

                    # 3. 寫回資料庫
                    cur.execute("UPDATE reports SET sdt_a=%s, sdt_c=%s, sdt_r=%s WHERE id=%s", (a, c, r, report_id))
                    conn.commit()
                    print(f"✅ ID:{report_id} 完成: A:{a}|C:{c}|R:{r}             ")
                    
                    # 緩衝，避免 Google 暴怒
                    time.sleep(2.5)

            except (psycopg2.OperationalError, psycopg2.InterfaceError) as db_err:
                print(f"\n🔌 資料庫斷線，正在嘗試重新連線... ({db_err})")
                time.sleep(5)
                conn = get_fresh_conn() # 重新取得連線
                continue

    except KeyboardInterrupt:
        print("\n👋 使用者手動停止。")
    finally:
        if conn: conn.close()

if __name__ == "__main__":
    backfill_historical_data()