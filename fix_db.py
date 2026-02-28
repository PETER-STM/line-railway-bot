import psycopg2
import sys

# 🚨 Railway 公網連線資訊
DATABASE_URL = "postgresql://postgres:xYwUUdAgpujXplEGKXtmNsWlREiBnpju@switchyard.proxy.rlwy.net:22646/railway"

def run_db_fix():
    print("🌐 正在執行資料庫加固程序...")
    conn = None
    try:
        conn = psycopg2.connect(DATABASE_URL, sslmode='require')
        conn.autocommit = True
        cur = conn.cursor()

        # 1. 加固 group_vips
        print("🔧 同步 group_vips 欄位...")
        cur.execute("ALTER TABLE group_vips ADD COLUMN IF NOT EXISTS diagnosis TEXT DEFAULT '';")
        cur.execute("ALTER TABLE group_vips ADD COLUMN IF NOT EXISTS last_tactic TEXT DEFAULT '';")
        cur.execute("ALTER TABLE group_vips ADD COLUMN IF NOT EXISTS meta_patterns TEXT DEFAULT '';")
        cur.execute("ALTER TABLE group_vips ADD COLUMN IF NOT EXISTS cognitive_tier TEXT DEFAULT 'L1';")
        cur.execute("ALTER TABLE group_vips ADD COLUMN IF NOT EXISTS tier_confidence FLOAT DEFAULT 0.0;")

        # 2. 加固 reports
        print("🔧 同步 reports 欄位...")
        cur.execute("ALTER TABLE reports ADD COLUMN IF NOT EXISTS diagnosis TEXT DEFAULT '';")
        cur.execute("ALTER TABLE reports ADD COLUMN IF NOT EXISTS cognitive_score INT DEFAULT 0;")
        cur.execute("ALTER TABLE reports ADD COLUMN IF NOT EXISTS is_fake BOOLEAN DEFAULT FALSE;")
        cur.execute("ALTER TABLE reports ADD COLUMN IF NOT EXISTS is_fragile BOOLEAN DEFAULT FALSE;")
        cur.execute("ALTER TABLE reports ADD COLUMN IF NOT EXISTS distortion TEXT DEFAULT '';")

        print("✅ 【修復完成】資料庫已與 V43 腦核對齊。")
        
    except Exception as e:
        print(f"❌ 執行失敗: {e}")
    finally:
        if conn: conn.close()

if __name__ == "__main__":
    run_db_fix()