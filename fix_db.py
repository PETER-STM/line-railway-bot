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
        print("🔍 Inspecting reports columns...")
        cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'reports';")
        r_cols = [row[0] for row in cur.fetchall()]

        if 'normalized_reporter_name' in r_cols and 'normalized_name' not in r_cols:
            print("🔄 Renaming 'normalized_reporter_name' to 'normalized_name'...")
            cur.execute("ALTER TABLE reports RENAME COLUMN normalized_reporter_name TO normalized_name;")
        
        elif 'normalized_reporter_name' in r_cols:
            print("🗑️ Dropping legacy column 'normalized_reporter_name'...")
            cur.execute("ALTER TABLE reports DROP COLUMN normalized_reporter_name;")

        if 'normalized_name' not in r_cols:
            print("➕ Creating 'normalized_name' column for reports...")
            cur.execute("ALTER TABLE reports ADD COLUMN normalized_name VARCHAR(100) DEFAULT '';")

        if 'report_content' not in r_cols:
            print("➕ Creating 'report_content' column...")
            cur.execute("ALTER TABLE reports ADD COLUMN report_content TEXT;")

        print("🔧 Backfilling NULLs in reports...")
        cur.execute("UPDATE reports SET normalized_name = reporter_name WHERE normalized_name IS NULL OR normalized_name = '';")

        print("✅ Database check complete!")
        
    except Exception as e:
        print(f"❌ Error: {e}")
    finally:
        conn.close()

if __name__ == "__main__":
    fix_database()