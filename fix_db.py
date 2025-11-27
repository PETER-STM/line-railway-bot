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
        print("Adding report_content column...")
        cur.execute("ALTER TABLE reports ADD COLUMN IF NOT EXISTS report_content TEXT;")
        
        print("Adding normalized_name column...")
        cur.execute("ALTER TABLE reports ADD COLUMN IF NOT EXISTS normalized_name VARCHAR(100) NOT NULL DEFAULT '';")
        
        print("Creating group_vips table...")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS group_vips (
                id SERIAL PRIMARY KEY,
                group_id TEXT NOT NULL,
                vip_name TEXT NOT NULL,
                normalized_name TEXT NOT NULL,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (group_id, normalized_name)
            );
        """)
        
        print("Creating group_configs table...")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS group_configs (
                group_id TEXT PRIMARY KEY,
                ai_mode BOOLEAN DEFAULT FALSE
            );
        """)
        
        conn.commit()
        print("Database schema updated successfully!")
        
    except Exception as e:
        conn.rollback()
        print(f"Error updating database: {e}")
    finally:
        conn.close()

if __name__ == "__main__":
    fix_database()