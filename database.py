import sys
import time
import psycopg2
from psycopg2 import pool, OperationalError
from contextlib import contextmanager
from config import Config

db_pool = None

def init_pool():
    global db_pool
    if not Config.DATABASE_URL:
        print("❌ Error: DATABASE_URL not set.", file=sys.stderr)
        return False
    
    for attempt in range(5):
        try:
            print(f"🔌 Connecting DB (Attempt {attempt+1})...", file=sys.stderr, flush=True)
            db_pool = psycopg2.pool.ThreadedConnectionPool(
                1, 20, 
                dsn=Config.DATABASE_URL, 
                sslmode='require', 
                connect_timeout=5,
                keepalives=1, 
                keepalives_idle=30,
                keepalives_interval=10,
                keepalives_count=5
            )
            # 測試連線
            with db_pool.getconn() as conn:
                with conn.cursor() as cur: cur.execute("SELECT 1")
                db_pool.putconn(conn)
            print("✅ DB Connected!", file=sys.stderr, flush=True)
            return True
        except Exception as e:
            print(f"⚠️ DB Connection Failed: {e}", file=sys.stderr)
            time.sleep(3)
    return False

@contextmanager
def get_db():
    conn = None
    try:
        if not db_pool: 
            if not init_pool(): raise Exception("DB Pool Init Failed")
        
        conn = db_pool.getconn()
        
        # 🔥 V16.7 核心修復：連線健康檢查 (CPR) + 狀態重置
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
            
            # ✅ [關鍵修正]：CPR 完後，必須強制 rollback 以結束 SELECT 1 開啟的交易。
            # 這樣 init_db 拿到連線後才能設定 autocommit，否則會報錯。
            conn.rollback() 

        except (OperationalError, psycopg2.InterfaceError):
            print("♻️ DB Connection dead, recycling...", file=sys.stderr)
            try:
                db_pool.putconn(conn, close=True) # 丟棄屍體
            except: pass
            conn = db_pool.getconn() # 拿新的

        yield conn
    except Exception as e:
        if conn: 
            try: conn.rollback()
            except: pass
        raise e
    finally:
        if conn: 
            try: db_pool.putconn(conn)
            except: pass

def init_db():
    print("🚀 DB Migration...", file=sys.stderr, flush=True)
    if not init_pool(): return

    with get_db() as conn:
        # V16.7: 這裡現在安全了，因為 get_db 已經幫忙 rollback 乾淨了
        conn.autocommit = True 
        with conn.cursor() as cur:
            # 1. 基礎表格
            cur.execute("""CREATE TABLE IF NOT EXISTS group_vips (
                group_id TEXT NOT NULL, vip_name TEXT NOT NULL, normalized_name TEXT NOT NULL,
                last_report_date DATE, current_streak INT DEFAULT 0, max_streak INT DEFAULT 0,
                total_missed INT DEFAULT 0, personality TEXT DEFAULT '', line_user_id TEXT,
                PRIMARY KEY (group_id, normalized_name))""")
            
            # 2. Reports 表格
            cur.execute("""CREATE TABLE IF NOT EXISTS reports (
                id SERIAL PRIMARY KEY, group_id TEXT NOT NULL, reporter_name TEXT NOT NULL,
                normalized_name TEXT NOT NULL, report_date DATE NOT NULL, report_content TEXT,
                cognitive_score INT DEFAULT 0,
                is_fake BOOLEAN DEFAULT FALSE,
                is_fragile BOOLEAN DEFAULT FALSE,
                distortion TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
            
            # 補丁：確保 Reports 欄位存在
            new_report_cols = [
                ("cognitive_score", "INT DEFAULT 0"),
                ("is_fake", "BOOLEAN DEFAULT FALSE"),
                ("is_fragile", "BOOLEAN DEFAULT FALSE"),
                ("distortion", "TEXT DEFAULT ''")
            ]
            for col, dtype in new_report_cols:
                try:
                    cur.execute(f"ALTER TABLE reports ADD COLUMN {col} {dtype}")
                except psycopg2.errors.DuplicateColumn: pass 
                except Exception: pass

            cur.execute("""CREATE TABLE IF NOT EXISTS group_configs (
                group_id TEXT PRIMARY KEY, ai_mode BOOLEAN DEFAULT FALSE, mode_type TEXT DEFAULT 'simple')""")

            # 3. TR 特訓欄位
            tr_cols = [("tr_tag", "TEXT"), ("tr_strategy", "TEXT"), ("tr_concept", "TEXT"), ("tr_incantation", "TEXT"), ("tr_instruction", "TEXT")]
            for col, dtype in tr_cols:
                try:
                    cur.execute(f"ALTER TABLE group_vips ADD COLUMN {col} {dtype}")
                except psycopg2.errors.DuplicateColumn: pass 
                except Exception: pass

            # 4. V14.0 認知分層補丁
            vip_cols = [("cognitive_tier", "TEXT DEFAULT 'L1'"), ("tier_confidence", "FLOAT DEFAULT 0.0")]
            for col, dtype in vip_cols:
                try:
                    cur.execute(f"ALTER TABLE group_vips ADD COLUMN {col} {dtype}")
                except psycopg2.errors.DuplicateColumn: pass
                except Exception: pass

    print("✅ Schema Up-to-date!", file=sys.stderr, flush=True)