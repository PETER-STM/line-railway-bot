import os
import sys
import time
import psycopg2
from dotenv import load_dotenv
from ai_service import evaluate_sdt_state

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL")

def get_fresh_conn():
    return psycopg2.connect(DATABASE_URL, sslmode='require')

def fix_corrupted_data():
    """自動清洗剛才被 Google 限速弄壞的全 0.5 假資料"""
    try:
        with get_fresh_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE reports
                    SET sdt_a = NULL, sdt_c = NULL, sdt_r = NULL
                    WHERE sdt_a = 0.5 AND sdt_c = 0.5 AND sdt_r = 0.5
                """)
                if cur.rowcount > 0:
                    print(f"🧹 [資料庫自動清洗] 成功移除了 {cur.rowcount} 筆因 API 限速損壞的 0.5 分紀錄，準備重跑！")
            conn.commit()
    except Exception as e:
        print(f"⚠️ 清洗資料失敗: {e}")

def backfill_historical_data():
    print("🚀 啟動歷史日報 SDT 批量回溯 (V40 節流防護與自動清洗版)...")
    
    # 啟動前先洗地
    fix_corrupted_data()

    retry_count = 0

    while True:
        conn = None
        cur = None
        try:
            conn = get_fresh_conn()
            cur = conn.cursor()

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

            state, score_str, (sa, sc, sr) = evaluate_sdt_state(content)

            # 🔥 攔截 API 限制產生的無效全 0.5 分數
            if sa == 0.5 and sc == 0.5 and sr == 0.5:
                if retry_count < 2:
                    print(f"⚠️ [API 節流警報] ID:{report_id} 獲得全 0.5 分，疑似觸發 Google 限制，冷卻 30 秒...")
                    time.sleep(30)
                    retry_count += 1
                    continue # 重新跑這一筆
                else:
                    print(f"⚠️ ID:{report_id} 確實內容空洞或無法解析，以 0.5 寫入。")
                    retry_count = 0
            else:
                retry_count = 0

            cur.execute("""
                UPDATE reports
                SET sdt_a = %s, sdt_c = %s, sdt_r = %s
                WHERE id = %s
            """, (sa, sc, sr, report_id))
            conn.commit()

            print(f"✅ ID:{report_id} [{name}] 完成: A:{sa:.2f}|C:{sc:.2f}|R:{sr:.2f} (判定: {state})")
            
            # 🔥 遵守 Google API 15 RPM 限制，強制間隔 4.5 秒 (重要！)
            time.sleep(4.5) 

        except Exception as e:
            print(f"\n🔌 網路瞬斷或例外錯誤: {e}", file=sys.stderr)
            print("⏳ 10秒後自動重連...", file=sys.stderr)
            time.sleep(10)
        finally:
            if cur:
                try: cur.close()
                except: pass
            if conn:
                try: conn.close()
                except: pass

if __name__ == "__main__":
    backfill_historical_data()