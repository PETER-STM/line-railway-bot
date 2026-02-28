import psycopg2

# 這是你環境變數裡的 Railway 連線字串
DB_URL = "postgresql://postgres:xYwUUdAgpujXplEGKXtmNsWlREiBnpju@switchyard.proxy.rlwy.net:22646/railway"

try:
    print("🔌 正在連線到 Railway 資料庫...")
    conn = psycopg2.connect(DB_URL, sslmode='require')
    cur = conn.cursor()

    # 1. 抓出所有有問題的海豚分身
    print("👻 正在鎖定海豚的幽靈分身...")
    cur.execute("SELECT normalized_name FROM group_vips WHERE normalized_name LIKE '%初%' OR normalized_name LIKE '%（%';")
    ghosts = cur.fetchall()
    
    if ghosts:
        print(f"🎯 發現 {len(ghosts)} 個幽靈帳號：{[g[0] for g in ghosts]}")
        
        # 2. 刪除這些幽靈在 group_vips 的註冊紀錄
        cur.execute("DELETE FROM group_vips WHERE normalized_name LIKE '%初%' OR normalized_name LIKE '%（%';")
        print(f"🔪 成功斬殺了 {cur.rowcount} 個幽靈會員。")
        
        # 3. 把他們曾經發過的日報，全部「認祖歸宗」合併回真正的「海豚」身上
        cur.execute("""
            UPDATE reports 
            SET normalized_name = '海豚' 
            WHERE normalized_name LIKE '%初%' OR normalized_name LIKE '%（%';
        """)
        print(f"🔄 成功將 {cur.rowcount} 篇過年日報合併回真正的『海豚』名下。")
        
        conn.commit()
    else:
        print("✅ 找不到幽靈分身，資料庫很乾淨！")

except Exception as e:
    print(f"❌ 發生錯誤: {e}")
finally:
    if 'cur' in locals(): cur.close()
    if 'conn' in locals(): conn.close()