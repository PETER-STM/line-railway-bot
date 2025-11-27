import os
import sys
import psycopg2

DATABASE_URL = os.environ.get('DATABASE_URL')

if not DATABASE_URL:
    print("ERROR: DATABASE_URL not found.")
    sys.exit(1)

def fix_database():
    print("Connecting to database...")
    # 啟用 autocommit 避免交易鎖死
    conn = psycopg2.connect(DATABASE_URL, sslmode='require')
    conn.autocommit = True 
    cur = conn.cursor()
    
    try:
        # --- 1. 檢查欄位 ---
        print("🔍 Inspecting group_vips columns...")
        cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'group_vips';")
        columns = [row[0] for row in cur.fetchall()]
        print(f"   Current columns: {columns}")

        # --- 2. 修正 normalized_name ---
        if 'normalized_vip_name' in columns and 'normalized_name' not in columns:
            print("🔄 Renaming 'normalized_vip_name' to 'normalized_name'...")
            cur.execute("ALTER TABLE group_vips RENAME COLUMN normalized_vip_name TO normalized_name;")
        elif 'normalized_name' not in columns:
             print("➕ Creating 'normalized_name' column...")
             cur.execute("ALTER TABLE group_vips ADD COLUMN IF NOT EXISTS normalized_name TEXT DEFAULT '';")

        # --- 3. 填補空值 ---
        print("🔧 Fixing NULL values...")
        cur.execute("UPDATE group_vips SET normalized_name = vip_name WHERE normalized_name IS NULL OR normalized_name = '';")

        # --- 4. 清理重複資料 (關鍵修正：使用 ctid) ---
        print("🧹 Cleaning up duplicates using ctid (skipping id check)...")
        # 這裡不使用 id，改用 ctid (物理位置)，保證不會報錯
        cur.execute("""
            DELETE FROM group_vips a
            WHERE a.ctid <> (
                SELECT min(b.ctid)
                FROM group_vips b
                WHERE a.group_id = b.group_id 
                AND a.normalized_name = b.normalized_name
            );
        """)

        # --- 5. 補上 ID 欄位 (如果缺失) ---
        if 'id' not in columns:
            print("➕ Adding missing 'id' Primary Key...")
            cur.execute("ALTER TABLE group_vips ADD COLUMN id SERIAL PRIMARY KEY;")

        # --- 6. 重建索引 ---
        print("🔒 Applying unique constraints...")
        try:
            cur.execute("DROP INDEX IF EXISTS idx_group_vips_unique;")
            cur.execute("ALTER TABLE group_vips DROP CONSTRAINT IF EXISTS group_vips_group_id_normalized_name_key;")
        except Exception:
            pass # 忽略刪除失敗

        cur.execute("""
            CREATE UNIQUE INDEX idx_group_vips_unique 
            ON group_vips (group_id, normalized_name);
        """)

        # --- 7. 確保其他表格存在 ---
        print("📦 Checking other tables...")
        cur.execute("CREATE TABLE IF NOT EXISTS reports (id SERIAL PRIMARY KEY);")
        cur.execute("ALTER TABLE reports ADD COLUMN IF NOT EXISTS report_content TEXT;")
        cur.execute("ALTER TABLE reports ADD COLUMN IF NOT EXISTS normalized_name VARCHAR(100) NOT NULL DEFAULT '';")
        
        cur.execute("""
            CREATE TABLE IF NOT EXISTS group_configs (
                group_id TEXT PRIMARY KEY,
                ai_mode BOOLEAN DEFAULT FALSE
            );
        """)

        print("✅ Database repair complete! Please restart your app.")
        
    except Exception as e:
        print(f"❌ Error during repair: {e}")
    finally:
        conn.close()

if __name__ == "__main__":
    fix_database()