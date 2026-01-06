import psycopg2

# 🔥 請把這裡換成你剛剛蒐集到的情報
# Host: switchyard.proxy.rlwy.net
# Port: 22646
# User: postgres (預設)
# Password: (去 Variables 複製)
# Database: railway (預設)

# 👇 把密碼填進去
DB_URL = "postgresql://postgres:xYwUUdAgpujXplEGKXtmNsWlREiBnpju@switchyard.proxy.rlwy.net:22646/railway"

try:
    print("🔌 正在連線到 Railway 資料庫...")
    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor()

    # 1. 刪除幽靈成員
    print("👻 正在抓鬼...")
    cur.execute("DELETE FROM group_vips WHERE vip_name LIKE '%今天有什麼%';")
    deleted_vips = cur.rowcount
    print(f"🔪 斬殺了 {deleted_vips} 個幽靈成員。")

    # 2. 刪除相關日報
    print("📄 正在銷毀錯誤日報...")
    cur.execute("DELETE FROM reports WHERE reporter_name LIKE '%今天有什麼%';")
    deleted_reports = cur.rowcount
    print(f"🔥 燒毀了 {deleted_reports} 篇無效日報。")

    conn.commit()
    cur.close()
    conn.close()
    print("✨ 手術成功！資料庫乾淨了！")

except Exception as e:
    print(f"💥 哎呀，出錯了：{e}")