import psycopg2

# 記得換成你的 Railway 連線字串
DB_URL = "postgresql://postgres:你的密碼@switchyard.proxy.rlwy.net:22646/railway"

conn = psycopg2.connect(DB_URL)
cur = conn.cursor()

# 查查看還有沒有怪名字
print("🔍 正在搜索殘黨...")
cur.execute("SELECT vip_name FROM group_vips WHERE vip_name LIKE '%今天有什麼%'")
ghosts = cur.fetchall()

if not ghosts:
    print("✅ 完美！找不到任何幽靈成員。")
else:
    print(f"⚠️ 警告！還有殘黨：{ghosts}")

conn.close()