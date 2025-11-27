import os
import sys
import psycopg2

DATABASE_URL = os.environ.get('DATABASE_URL')

if not DATABASE_URL:
    print("ERROR: DATABASE_URL not found.")
    sys.exit(1)

def fix_database():
    print("Connecting to database...")
    # 啟用 autocommit 模式，避免單一錯誤導致 "current transaction is aborted"
    conn = psycopg2.connect(DATABASE_URL, sslmode='require')
    conn.autocommit = True 
    cur = conn.cursor()
    
    try:
        # --- 1. 診斷與修復 group_vips 欄位 ---
        print("Inspecting group_vips columns...")
        
        # 查詢目前的欄位名稱
        cur.execute("""
            SELECT column_name 
            FROM information_schema.columns 
            WHERE table_name = 'group_vips';
        """)
        columns = [row[0] for row in cur.fetchall()]
        print(f"Current columns found: {columns}")

        # 情況 A: 發現舊名稱 normalized_vip_name，將其改名
        if 'normalized_vip_name' in columns and 'normalized_name' not in columns:
            print("🔄 Renaming column 'normalized_vip_name' to 'normalized_name'...")
            cur.execute("ALTER TABLE group_vips RENAME COLUMN normalized_vip_name TO normalized_name;")
        
        # 情況 B: 兩個都存在 (可能是重複建立)，刪除舊的
        elif 'normalized_vip_name' in columns and 'normalized_name' in columns:
            print("🗑️ Dropping redundant column 'normalized_vip_name'...")
            cur.execute("ALTER TABLE group_vips DROP COLUMN normalized_vip_name;")

        # 情況 C: 都不存在，建立新的
        else:
            print("➕ Ensuring 'normalized_name' column exists...")
            cur.execute("ALTER TABLE group_vips ADD COLUMN IF NOT EXISTS normalized_name TEXT DEFAULT '';")

        # --- 2. 填補空值 (避免 NOT NULL 錯誤) ---
        print("🔧 Backfilling empty normalized_name...")
        cur.execute("UPDATE group_vips SET normalized_name = vip_name WHERE normalized_name IS NULL OR normalized_name = '';")

        # --- 3. 清理重複資料 (這是建立唯一索引的前提) ---
        print("🧹 Cleaning up duplicates before creating index...")
        # 保留 ID 最小的那筆，刪除其餘重複 (group_id + normalized_name 相同者)
        cur.execute("""
            DELETE FROM group_vips a USING group_vips b
            WHERE a.id > b.id 
            AND a.group_id = b.group_id 
            AND a.normalized_name = b.normalized_name;
        """)

        # --- 4. 重建索引與約束 ---
        print("🔒 Applying unique constraints...")
        # 先移除舊的以防萬一
        try:
            cur.execute("DROP INDEX IF EXISTS idx_group_vips_unique;")
            cur.execute("ALTER TABLE group_vips DROP CONSTRAINT IF EXISTS group_vips_group_id_normalized_name_key;")
        except Exception as e:
            print(f"   (Ignored minor error dropping constraints: {e})")

        # 建立新的唯一索引
        cur.execute("""
            CREATE UNIQUE INDEX idx_group_vips_unique 
            ON group_vips (group_id, normalized_name);
        """)

        # --- 5. 確保其他表格存在 ---
        print("📦 Checking other tables (reports, group_configs)...")
        
        # Reports
        cur.execute("CREATE TABLE IF NOT EXISTS reports (id SERIAL PRIMARY KEY);")
        cur.execute("ALTER TABLE reports ADD COLUMN IF NOT EXISTS report_content TEXT;")
        cur.execute("ALTER TABLE reports ADD COLUMN IF NOT EXISTS normalized_name VARCHAR(100) NOT NULL DEFAULT '';")
        
        # Group Configs
        cur.execute("""
            CREATE TABLE IF NOT EXISTS group_configs (
                group_id TEXT PRIMARY KEY,
                ai_mode BOOLEAN DEFAULT FALSE
            );
        """)

        print("✅ Database repair complete! You can now start the app.")
        
    except Exception as e:
        print(f"❌ Error during repair: {e}")
        # 因為開啟了 autocommit，不需要 rollback
    finally:
        conn.close()

if __name__ == "__main__":
    fix_database()