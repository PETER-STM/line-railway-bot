import os
import sys
import psycopg2

DATABASE_URL = os.environ.get('DATABASE_URL')

if not DATABASE_URL:
    print("ERROR: DATABASE_URL not found.")
    sys.exit(1)

def fix_database():
    print("Connecting to database...")
    # 關鍵：啟用 autocommit，避免單一錯誤導致 "current transaction is aborted"
    conn = psycopg2.connect(DATABASE_URL, sslmode='require')
    conn.autocommit = True 
    cur = conn.cursor()
    
    try:
        # --- 1. 診斷並修正欄位名稱錯亂 ---
        print("🔍 Inspecting group_vips columns...")
        
        # 查詢目前的欄位名稱
        cur.execute("""
            SELECT column_name 
            FROM information_schema.columns 
            WHERE table_name = 'group_vips';
        """)
        columns = [row[0] for row in cur.fetchall()]
        print(f"   Current columns: {columns}")

        # 狀況 A: 資料庫裡有 'normalized_vip_name' (錯誤名稱)，改名為 'normalized_name'
        if 'normalized_vip_name' in columns and 'normalized_name' not in columns:
            print("🔄 Renaming wrong column 'normalized_vip_name' to 'normalized_name'...")
            cur.execute("ALTER TABLE group_vips RENAME COLUMN normalized_vip_name TO normalized_name;")
        
        # 狀況 B: 兩個都存在 (可能是重複建立)，刪除錯誤的那個
        elif 'normalized_vip_name' in columns and 'normalized_name' in columns:
            print("🗑️ Dropping redundant column 'normalized_vip_name'...")
            cur.execute("ALTER TABLE group_vips DROP COLUMN normalized_vip_name;")

        # 狀況 C: 正確欄位不存在，建立它
        if 'normalized_name' not in columns and 'normalized_vip_name' not in columns:
            print("➕ Creating 'normalized_name' column...")
            cur.execute("ALTER TABLE group_vips ADD COLUMN IF NOT EXISTS normalized_name TEXT DEFAULT '';")

        # --- 2. 填補 NULL 值 (修復髒資料) ---
        print("🔧 Fixing NULL values...")
        # 將 NULL 的欄位填入 vip_name，避免 NOT NULL 錯誤
        cur.execute("UPDATE group_vips SET normalized_name = vip_name WHERE normalized_name IS NULL OR normalized_name = '';")

        # --- 3. 清理重複資料 (這是建立唯一索引的前提) ---
        print("🧹 Cleaning up duplicates...")
        # 保留 ID 最小的那筆，刪除其餘重複 (group_id + normalized_name 相同者)
        # 這裡會刪除你的 (test, test, null, test) 重複項
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

        print("✅ Database repair complete! Please restart your app.")
        
    except Exception as e:
        print(f"❌ Error during repair: {e}")
    finally:
        conn.close()

if __name__ == "__main__":
    fix_database()