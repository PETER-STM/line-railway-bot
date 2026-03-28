import os
import psycopg2
from dotenv import load_dotenv

# 🔥 自動讀取環境變數，安全連線
load_dotenv()
DB_URL = os.getenv("DATABASE_URL")

try:
    print("🔌 正在連線到 Railway 資料庫...")
    conn = psycopg2.connect(DB_URL, sslmode='require')
    cur = conn.cursor()

    # 1. 抓出所有有問題的海豚分身 (帶有 '初' 或括號的)
    print("👻 正在鎖定海豚的幽靈分身...")
    cur.execute("SELECT normalized_name FROM group_vips WHERE normalized_name LIKE '%初%' OR normalized_name LIKE '%（%';")
    ghosts = cur.fetchall()
    
    if ghosts:
        print(f"🎯 發現 {len(ghosts)} 個幽靈帳號：{[g[0] for g in ghosts]}")
        
        # 2. 刪除這些幽靈在 group_vips 的註冊紀錄 (他們就不會再出現在缺交名單上)
        cur.execute("DELETE FROM group_vips WHERE normalized_name LIKE '%初%' OR normalized_name LIKE '%（%';")
        print(f"🔪 成功斬殺了 {cur.rowcount} 個幽靈會員。")
        
        # 3. 把他們曾經發過的日報，全部「認祖歸宗」合併回真正的「海豚」身上
        cur.execute("""
            UPDATE reports 
            SET normalized_name = '海豚' 
            WHERE normalized_name LIKE '%海豚%';
        """)
        print(f"🔄 成功將 {cur.rowcount} 篇散落的日報，重新過戶給真正的【海豚】！")
        
        conn.commit()
        print("✅ 歷史驅魔完畢！資料庫已呈現完美淨化狀態。")
    else:
        print("✨ 資料庫很乾淨，沒有發現海豚的幽靈分身。")

except Exception as e:
    print(f"❌ 執行失敗: {e}")
finally:
    if 'cur' in locals(): cur.close()
    if 'conn' in locals(): conn.close()