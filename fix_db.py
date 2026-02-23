import psycopg2
import sys

# 🚨 關鍵！請在這裡貼上 Railway 的 【Public Connection URL】
# 格式應該是 postgresql://... @ ... .railway.app: ...
DATABASE_URL = "postgresql://postgres:xYwUUdAgpujXplEGKXtmNsWlREiBnpju@switchyard.proxy.rlwy.net:22646/railway"

def run_db_fix():
    print("🌐 正在透過公網連線至 Railway 資料庫...")
    conn = None
    try:
        # 連線設定
        conn = psycopg2.connect(DATABASE_URL, sslmode='require')
        cur = conn.cursor()

        # 1. 修復 group_vips 表格 (新增診斷與戰術紀錄欄位)
        print("🔧 檢查 group_vips 欄位...")
        cur.execute("ALTER TABLE group_vips ADD COLUMN IF NOT EXISTS diagnosis TEXT DEFAULT '';")
        cur.execute("ALTER TABLE group_vips ADD COLUMN IF NOT EXISTS last_tactic TEXT DEFAULT '';")
        cur.execute("ALTER TABLE group_vips ADD COLUMN IF NOT EXISTS meta_patterns TEXT DEFAULT '';")

        # 2. 修復 reports 表格 (確保日報也有診斷紀錄格)
        print("🔧 檢查 reports 欄位...")
        cur.execute("ALTER TABLE reports ADD COLUMN IF NOT EXISTS diagnosis TEXT DEFAULT '';")

        conn.commit()
        print("✅ 【修復成功】所有欄位已同步完成！")
        
    except Exception as e:
        print(f"❌ 修復失敗！")
        print(f"錯誤細節: {e}")
        if "internal" in str(e):
            print("\n💡 提示：你似乎還在用 .internal 網址。請更換為 .railway.app 結尾的 Public URL！")
    finally:
        if conn:
            conn.close()

if __name__ == "__main__":
    if "在此處貼上" in DATABASE_URL:
        print("🚨 錯誤：你忘記修改程式碼中的 DATABASE_URL 了！")
    else:
        run_db_fix()