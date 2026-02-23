import sys
from database import get_db

print("🧹 正在洗白被 AI 亂打分的歷史紀錄...")
try:
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE reports SET sdt_a=NULL, sdt_c=NULL, sdt_r=NULL")
        conn.commit()
    print("✅ 洗白成功！所有歷史日報已歸零，等待重新打分。")
except Exception as e:
    print(f"❌ 發生錯誤: {e}")