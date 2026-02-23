import os
import psycopg2
from dotenv import load_dotenv

# 讀取本地的 .env
load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL")

print("🔌 連線資料庫中...")
try:
    conn = psycopg2.connect(DATABASE_URL, sslmode='require')
    cur = conn.cursor()

    print("🔧 正在為 mab_stats 表格擴充演化神經元 (加入勝敗欄位)...")
    
    # 補上缺少的欄位
    cur.execute("ALTER TABLE mab_stats ADD COLUMN IF NOT EXISTS successes INT DEFAULT 0;")
    cur.execute("ALTER TABLE mab_stats ADD COLUMN IF NOT EXISTS failures INT DEFAULT 0;")
    
    conn.commit()
    print("✅ 完美修復！mab_stats 欄位擴充成功！")

except Exception as e:
    print(f"❌ 發生錯誤: {e}")
finally:
    if 'cur' in locals(): cur.close()
    if 'conn' in locals(): conn.close()