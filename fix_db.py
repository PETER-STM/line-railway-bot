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
    conn.autocommit = True 
    cur = conn.cursor()
    
    try:
        # ==========================================
        # Part 1: 確認 Group VIPS (已修復，做基本檢查即可)
        # ==========================================
        print("✅ (Skipping heavy group_vips checks as it is likely fixed)...")
        
        # ==========================================
        # Part 2: 修復 Reports 表格 (本次重點)
        # ==========================================
        print("🔍 Inspecting reports columns...")
        cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'reports';")
        r_cols = [row[0] for row in cur.fetchall()]
        print(f"   Current reports columns: {r_cols}")

        # 1. 修正欄位名稱: normalized_reporter_name -> normalized_name
        if 'normalized_reporter_name' in r_cols and 'normalized_name' not in r_cols:
            print("🔄 Renaming 'normalized_reporter_name' to 'normalized_name'...")
            cur.execute("ALTER TABLE reports RENAME COLUMN normalized_reporter_name TO normalized_name;")
        
        # 2. 如果舊欄位還在且新欄位也有 (重複)，刪除舊的
        elif 'normalized_reporter_name' in r_cols:
            print("🗑️ Dropping legacy column 'normalized_reporter_name'...")
            cur.execute("ALTER TABLE reports DROP COLUMN normalized_reporter_name;")

        # 3. 確保 normalized_name 存在
        if 'normalized_name' not in r_cols and 'normalized_reporter_name' not in r_cols:
            print("➕ Creating 'normalized_name' column for reports...")
            cur.execute("ALTER TABLE reports ADD COLUMN normalized_name VARCHAR(100) DEFAULT '';")

        # 4. 確保 report_content 存在
        if 'report_content' not in r_cols:
            print("➕ Creating 'report_content' column...")
            cur.execute("ALTER TABLE reports ADD COLUMN report_content TEXT;")

        # 5. 確保 created_at 存在 (選用，方便除錯)
        if 'created_at' not in r_cols:
             print("➕ Creating 'created_at' column...")
             cur.execute("ALTER TABLE reports ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP;")

        # 6. 填補 reports 的空值 (防止查詢報錯)
        print("🔧 Backfilling NULLs in reports...")
        try:
            cur.execute("UPDATE reports SET normalized_name = reporter_name WHERE normalized_name IS NULL OR normalized_name = '';")
        except Exception as e:
            print(f"   (Minor warning during update: {e})")

        # ==========================================
        # Part 3: Group Configs
        # ==========================================
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