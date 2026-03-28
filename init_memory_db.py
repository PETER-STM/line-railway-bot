import os
import psycopg2
from dotenv import load_dotenv

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL")

print("🔌 連線資料庫，準備擴建阿摩的記憶海馬迴...")
try:
    conn = psycopg2.connect(DATABASE_URL, sslmode='require')
    cur = conn.cursor()
    
    # 建立阿摩專屬的每週記憶表
    cur.execute("""
        CREATE TABLE IF NOT EXISTS amor_memories (
            id SERIAL PRIMARY KEY,
            week_start_date DATE NOT NULL,
            week_end_date DATE NOT NULL,
            group_id TEXT NOT NULL,
            avg_insight_score FLOAT DEFAULT 0.0,
            amor_reflection TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    conn.commit()
    print("✅ 記憶體擴建成功！『amor_memories』資料表已上線，準備接收靈魂數據。")

except Exception as e:
    print(f"❌ 發生錯誤: {e}")
finally:
    if 'cur' in locals(): cur.close()
    if 'conn' in locals(): conn.close()