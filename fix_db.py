import os
import sys
import psycopg2

DATABASE_URL = os.environ.get('DATABASE_URL')

if not DATABASE_URL:
    print("ERROR: DATABASE_URL not found.")
    sys.exit(1)

def fix_database():
    print("Connecting to database...")
    # 啟用 autocommit
    conn = psycopg2.connect(DATABASE_URL, sslmode='require')
    conn.autocommit = True 
    cur = conn.cursor()
    
    try:
        # --- 1. 檢查目前欄位 ---
        print("🔍 Inspecting group_vips columns...")
        cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'group_vips';")
        columns = [row[0] for row in cur.fetchall()]
        print(f"   Current columns: {columns}")

        # --- 2. 修正 normalized_name (欄位名稱) ---
        if 'normalized_vip_name' in columns and 'normalized_name' not in columns:
            print("🔄 Renaming 'normalized_vip_name' to 'normalized_name'...")
            cur.execute("ALTER TABLE group_vips RENAME COLUMN normalized_vip_name TO normalized_name;")
        elif 'normalized_name' not in columns:
             print("➕ Creating 'normalized_name' column...")
             cur.execute("ALTER TABLE group_vips ADD COLUMN IF NOT EXISTS normalized_name TEXT DEFAULT '';")

        # --- 3. 填補空值 ---
        print("🔧 Fixing NULL values...")
        cur.execute("UPDATE group_vips SET normalized_name = vip_name WHERE normalized_name IS NULL OR normalized_name = '';")

        # --- 4. 清理重複資料 (使用 ctid) ---
        print("🧹 Cleaning up duplicates using ctid...")
        cur.execute("""
            DELETE FROM group_vips a
            WHERE a.ctid <> (
                SELECT min(b.ctid)
                FROM group_vips b
                WHERE a.group_id = b.group_id 
                AND a.normalized_name = b.normalized_name
            );
        """)

        # --- 5. 處理 ID 與 Primary Key 衝突 (關鍵修正) ---
        
        # A. 如果沒有 id 欄位，先加進去 (但先不設 PK)
        if 'id' not in columns:
            print("➕ Adding 'id' column (without PK first)...")
            cur.execute("ALTER TABLE group_vips ADD COLUMN id SERIAL;")

        # B. 強制移除現有的任何 Primary Key 約束
        print("🔓 Removing old Primary Key constraints...")
        cur.execute("""
            DO $$
            DECLARE r RECORD;
            BEGIN
                FOR r IN (
                    SELECT constraint_name 
                    FROM information_schema.table_constraints 
                    WHERE table_name = 'group_vips' AND constraint_type = 'PRIMARY KEY'
                ) LOOP
                    EXECUTE 'ALTER TABLE group_vips DROP CONSTRAINT ' || quote_ident(r.constraint_name);
                END LOOP;
            END $$;
        """)

        # C. 將 id 設定為新的 Primary Key
        print("🔑 Setting 'id' as the new Primary Key...")
        try:
            cur.execute("ALTER TABLE group_vips ADD PRIMARY KEY (id);")
        except Exception as e:
            print(f"   (Info: id might already be PK, skipping: {e})")

        # --- 6. 重建唯一索引 ---
        print("🔒 Applying unique constraints...")
        try:
            cur.execute("DROP INDEX IF EXISTS idx_group_vips_unique;")
            cur.execute("ALTER TABLE group_vips DROP CONSTRAINT IF EXISTS group_vips_group_id_normalized_name_key;")
        except Exception:
            pass

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