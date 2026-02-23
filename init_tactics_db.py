import os
import psycopg2
from dotenv import load_dotenv

# 讀取本地的 .env
load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL")

MINDSET_TACTICS = {
    "斯多葛反問": {"desc": "使用向下探究技術，剝開表層藉口，直擊其控制範圍內的責任。", "risk": "low"},
    "認知解構": {"desc": "找出『絕對化謬誤』，用邏輯與證據戳破其自動化思考。", "risk": "low"},
    "行動微光": {"desc": "基於 FBM 模型，給予極低腦力與體力消耗的微型提示任務。", "risk": "low"},
    "留白配速": {"desc": "不給予解決方案，僅作反映(Reflection)，給予其靜默反思的空間。", "risk": "low"},
    "靈魂拷問": {"desc": "ACT 價值觀導向：質問其逃避行為是否對得起最高理想。", "risk": "high"},
    "單點爆破": {"desc": "剝奪選擇權，作為強烈信號(Signal)，強制執行單一標準動作。", "risk": "high"},
    "休克療法": {"desc": "強迫預想最壞情況並接受現實，進行極端的心理止血。", "risk": "high"}
}

print("🔌 連線資料庫建立動態戰術表...")
try:
    conn = psycopg2.connect(DATABASE_URL, sslmode='require')
    cur = conn.cursor()
    # 建立動態戰術表，多了一個 enhancement 欄位用來裝 AI 自己發明的提示詞
    cur.execute("""
        CREATE TABLE IF NOT EXISTS dynamic_tactics (
            tactic_key VARCHAR(50) PRIMARY KEY,
            description TEXT NOT NULL,
            enhancement TEXT DEFAULT '',
            risk_level VARCHAR(10) NOT NULL
        )
    """)
    # 灌入初始資料
    for key, val in MINDSET_TACTICS.items():
        cur.execute("""
            INSERT INTO dynamic_tactics (tactic_key, description, risk_level)
            VALUES (%s, %s, %s)
            ON CONFLICT (tactic_key) DO NOTHING
        """, (key, val["desc"], val["risk"]))
    
    conn.commit()
    print("✅ 動態戰術表 (dynamic_tactics) 建立與初始化成功！")
except Exception as e:
    print(f"❌ 錯誤: {e}")
finally:
    if 'cur' in locals(): cur.close()
    if 'conn' in locals(): conn.close()