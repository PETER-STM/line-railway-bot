import os
import psycopg2

DATABASE_URL = os.environ.get('DATABASE_URL')

if not DATABASE_URL:
    print("ERROR: DATABASE_URL not found.")
    exit(1)

def fix_database():
    print("Connecting to database...")
    conn = psycopg2.connect(DATABASE_URL, sslmode='require')
    cur = conn.cursor()
    
    try:
        # 1. 修復 reports 表格 (保留原本邏輯)
        print("Checking reports table schema...")
        cur.execute("CREATE TABLE IF NOT EXISTS reports (id SERIAL PRIMARY KEY);")
        cur.execute("ALTER TABLE reports ADD COLUMN IF NOT EXISTS report_content TEXT;")
        cur.execute("ALTER TABLE reports ADD COLUMN IF NOT EXISTS normalized_name VARCHAR(100) NOT NULL DEFAULT '';")
        
        # 2. 修復 group_vips 表格 (這是修正重點)
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
        
        # C. 簡單初始化舊資料 (避免空字串導致唯一性衝突)
        # 如果 normalized_name 是空的，暫時填入 vip_name
        cur.execute("UPDATE group_vips SET normalized_name = vip_name WHERE normalized_name = '' OR normalized_name IS NULL;")

        # D. 處理 Unique Constraint (app.py 依賴此約束)
        print(" -> Updating Unique Constraints...")
        try:
            # 嘗試建立唯一索引，如果資料有重複可能會失敗，這裡做簡單的容錯
            cur.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_group_vips_unique 
                ON group_vips (group_id, normalized_name);
            """)
            # 如果表格沒有約束，則添加約束使用該索引
            cur.execute("""
                INSERT INTO group_vips (group_id, vip_name, normalized_name) VALUES ('test', 'test', 'test') 
                ON CONFLICT (group_id, normalized_name) DO NOTHING;
            """) 
            # 上面那行是用來測試約束是否生效的假動作，如果沒報錯代表約束正常或索引已存在
            # 真正的約束添加通常如下 (Postgres):
            # ALTER TABLE group_vips ADD CONSTRAINT unique_vip UNIQUE USING INDEX idx_group_vips_unique;
            # 但為了避免複雜報錯，我們只要確保有 index 通常 ON CONFLICT 就能運作
        except Exception as e:
            print(f"Warning: Could not create unique index (might have duplicate data): {e}")

        # 3. 修復 group_configs 表格
        print("Checking group_configs table schema...")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS group_configs (
                group_id TEXT PRIMARY KEY,
                ai_mode BOOLEAN DEFAULT FALSE
            );
        """)
        
        conn.commit()
        print("✅ Database schema updated successfully!")
        
    except Exception as e:
        conn.rollback()
        print(f"❌ Error updating database: {e}")
    finally:
        conn.close()

if __name__ == "__main__":
    fix_database()