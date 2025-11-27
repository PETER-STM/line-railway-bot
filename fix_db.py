import os
import sys
import psycopg2

DATABASE_URL = os.environ.get('DATABASE_URL')

if not DATABASE_URL:
    print("ERROR: DATABASE_URL not found.")
    sys.exit(1)

def fix_database():
    print("Connecting to database...")
    conn = psycopg2.connect(DATABASE_URL, sslmode='require')
    cur = conn.cursor()
    
    try:
        # --- 1. 修復 reports 表格 ---
        print("Checking reports table schema...")
        cur.execute("CREATE TABLE IF NOT EXISTS reports (id SERIAL PRIMARY KEY);") # 確保表存在
        cur.execute("ALTER TABLE reports ADD COLUMN IF NOT EXISTS report_content TEXT;")
        cur.execute("ALTER TABLE reports ADD COLUMN IF NOT EXISTS normalized_name VARCHAR(100) DEFAULT '';")
        
        # --- 2. 修復 group_configs 表格 ---
        print("Checking group_configs table schema...")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS group_configs (
                group_id TEXT PRIMARY KEY,
                ai_mode BOOLEAN DEFAULT FALSE
            );
        """)

        # --- 3. 修復 group_vips 表格 (最關鍵的部分) ---
        print("Checking group_vips table schema...")
        # A. 確保表格存在
        cur.execute("""
            CREATE TABLE IF NOT EXISTS group_vips (
                id SERIAL PRIMARY KEY,
                group_id TEXT NOT NULL,
                vip_name TEXT NOT NULL,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );
        """)
        
        # B. 強制補上 normalized_name 欄位 (解決你的報錯)
        print(" -> Patching normalized_name column...")
        cur.execute("ALTER TABLE group_vips ADD COLUMN IF NOT EXISTS normalized_name TEXT DEFAULT '';")
        
        # C. 處理 Unqiue Constraint (app.py 依賴此約束)
        # 由於 app.py 使用 ON CONFLICT (group_id, normalized_name)，我們必須確保這個約束存在
        print(" -> Updating Unique Constraints...")
        
        # 嘗試移除舊的約束 (如果名稱不同可能需要手動處理，但這通常能覆蓋大部分情況)
        cur.execute("ALTER TABLE group_vips DROP CONSTRAINT IF EXISTS group_vips_group_id_vip_name_key;")
        cur.execute("ALTER TABLE group_vips DROP CONSTRAINT IF EXISTS group_vips_group_id_normalized_name_key;")
        
        # 建立新的唯一約束 (包含 normalized_name)
        # 注意：如果有重複資料，這一步可能會失敗，需要先清空重複項
        try:
            cur.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_group_vips_unique 
                ON group_vips (group_id, normalized_name);
            """)
            # 或者使用 CONSTRAINT 語法 (Postgres 支援 ON CONFLICT 指定索引)
            cur.execute("""
                ALTER TABLE group_vips 
                ADD CONSTRAINT group_vips_group_id_normalized_name_key 
                UNIQUE USING INDEX idx_group_vips_unique;
            """)
        except psycopg2.errors.DuplicateTable: 
            pass # 約束已存在
        except Exception as e:
            print(f"Warning regarding constraints: {e}")

        conn.commit()
        print("✅ Database schema updated successfully!")
        
    except Exception as e:
        conn.rollback()
        print(f"❌ Error updating database: {e}")
    finally:
        conn.close()

if __name__ == "__main__":
    fix_database()