import psycopg2
import sys

# 🔥 請換成你的 Railway 連線字串
DB_URL = "postgresql://postgres:xYwUUdAgpujXplEGKXtmNsWlREiBnpju@switchyard.proxy.rlwy.net:22646/railway"

try:
    print("🔌 連線資料庫中...")
    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor()

    print("🚿 正在執行全伺服器「除垢」作業 (TRIM)...")

    # 1. 清洗會員名單 (group_vips)
    # 邏輯：把 normalized_name 和 vip_name 前後的空白全部砍掉
    cur.execute("""
        UPDATE group_vips 
        SET normalized_name = TRIM(normalized_name),
            vip_name = TRIM(vip_name)
        WHERE normalized_name LIKE '% %' OR vip_name LIKE '% %';
    """)
    rows_vips = cur.rowcount
    print(f"✅ 已修復會員名單：{rows_vips} 筆資料 (去除隱形空白)")

    # 2. 清洗日報紀錄 (reports)
    # 邏輯：把歷史日報裡的名字也都修剪乾淨，這樣統計才對得上
    cur.execute("""
        UPDATE reports 
        SET normalized_name = TRIM(normalized_name),
            reporter_name = TRIM(reporter_name)
        WHERE normalized_name LIKE '% %' OR reporter_name LIKE '% %';
    """)
    rows_reports = cur.rowcount
    print(f"✅ 已修復歷史日報：{rows_reports} 筆資料")

    conn.commit()
    cur.close()
    conn.close()
    
    print("\n✨ 系統淨化完成！所有人的名字都已經乾淨了！")
    print("👉 現在你可以再去跑一次「統計缺交」，準確度應該是 100% 了。")

except Exception as e:
    print(f"💥 錯誤: {e}")