import os
import sys
import time
import psycopg2
from dotenv import load_dotenv

# 強制讀取環境變數
load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL")

# 🔥 核心關鍵：直接匯入 ai_service 裡最新的「V38 嚴格評分系統」！
from ai_service import evaluate_sdt_state

def get_fresh_conn():
    return psycopg2.connect(DATABASE_URL, sslmode='require')

def backfill_historical_data():
    print("🚀 啟動歷史日報 SDT 批量回溯 (V38 實質證據同步 + 網路防彈版)...")

    while True:
        conn = None
        cur = None
        try:
            conn = get_fresh_conn()
            cur = conn.cursor()

            # 每次只找出一筆還沒打分的日報
            cur.execute("""
                SELECT id, report_content, normalized_name
                FROM reports
                WHERE sdt_c IS NULL OR sdt_c = 0
                ORDER BY id DESC LIMIT 1
            """)
            row = cur.fetchone()

            if not row:
                print("✨ 所有歷史資料都已經嚴格量化完畢！")
                break

            report_id, content, name = row

            # 🔥 呼叫 ai_service 裡最新的嚴格打分邏輯
            state, score_str, (sa, sc, sr) = evaluate_sdt_state(content)

            cur.execute("""
                UPDATE reports
                SET sdt_a = %s, sdt_c = %s, sdt_r = %s
                WHERE id = %s
            """, (sa, sc, sr, report_id))
            conn.commit()

            print(f"✅ ID:{report_id} [{name}] 完成: A:{sa:.2f}|C:{sc:.2f}|R:{sr:.2f} (判定: {state})")
            
            # 休息 1.5 秒，避免被 Google API 阻擋
            time.sleep(1.5) 

        except Exception as e:
            # 🛡️ 遇到網路瞬斷或 DNS 解析失敗，絕對不崩潰，休息 10 秒後自動重試
            print(f"\n🔌 網路或資料庫例外 (Railway 正常現象): {e}", file=sys.stderr)
            print("⏳ 10秒後自動重連...", file=sys.stderr)
            time.sleep(10)
        finally:
            # 確保每次迴圈都乾淨地關閉連線，不佔用資源
            if cur:
                try: cur.close()
                except: pass
            if conn:
                try: conn.close()
                except: pass

if __name__ == "__main__":
    backfill_historical_data()