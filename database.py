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
    
    # V23.26: 維持高強度的重試機制
    for attempt in range(5):
        try:
            print(f"🔌 Connecting DB (Attempt {attempt+1})...", file=sys.stderr, flush=True)
            db_pool = psycopg2.pool.ThreadedConnectionPool(
                1, 20, 
                dsn=Config.DATABASE_URL, 
                sslmode='require', 
                connect_timeout=15,
                keepalives=1, 
                keepalives_idle=60,
                keepalives_interval=10,
                keepalives_count=5
            )
            with db_pool.getconn() as conn:
                with conn.cursor() as cur: cur.execute("SELECT 1")
                # 🔥 這裡不需要 rollback，因為 getconn 馬上還回去了，但保險起見可以加
                db_pool.putconn(conn)
            print("✅ DB Connected!", file=sys.stderr, flush=True)
            return True
        except Exception as e:
            print(f"⚠️ DB Connection Failed: {e}. Retrying in 5s...", file=sys.stderr)
            time.sleep(5)
    return False

@contextmanager
def get_db():
    conn = None
    try:
        if not db_pool: 
            if not init_pool(): raise Exception("DB Pool Init Failed")
        conn = db_pool.getconn()
        try:
            # 🔥 健康檢查
            with conn.cursor() as cur: cur.execute("SELECT 1")
            # 🔥🔥 關鍵修正：檢查完立刻 Rollback，清除隱式交易狀態
            conn.rollback()
        except (OperationalError, psycopg2.InterfaceError):
            try:
                db_pool.putconn(conn, close=True)
            except:
                pass
            conn = db_pool.getconn()
            # 重連後也要確保乾淨
            conn.rollback()
            
        yield conn
    except Exception as e:
        if conn:
            try:
                conn.rollback()
            except:
                pass
        raise e
    finally:
        if conn:
            try:
                # 確保歸還前是乾淨的
                conn.rollback() 
                db_pool.putconn(conn)
            except:
                pass

def init_db():
    print("🚀 DB Migration...", file=sys.stderr, flush=True)
    if not init_pool(): return

    # 使用獨立的連接邏輯避免 context manager 的干擾，或者直接在 context 內處理
    try:
        with get_db() as conn:
            # 🔥🔥 關鍵修正：在設定 autocommit 前確保無交易進行中
            conn.rollback()
            conn.autocommit = True 
            
            with conn.cursor() as cur:
                cur.execute("""CREATE TABLE IF NOT EXISTS group_vips (
                    group_id TEXT NOT NULL, vip_name TEXT NOT NULL, normalized_name TEXT NOT NULL,
                    last_report_date DATE, current_streak INT DEFAULT 0, max_streak INT DEFAULT 0,
                    total_missed INT DEFAULT 0, personality TEXT DEFAULT '', line_user_id TEXT,
                    PRIMARY KEY (group_id, normalized_name))""")
                
                cur.execute("""CREATE TABLE IF NOT EXISTS reports (
                    id SERIAL PRIMARY KEY, group_id TEXT NOT NULL, reporter_name TEXT NOT NULL,
                    normalized_name TEXT NOT NULL, report_date DATE NOT NULL, report_content TEXT,
                    cognitive_score INT DEFAULT 0,
                    is_fake BOOLEAN DEFAULT FALSE,
                    is_fragile BOOLEAN DEFAULT FALSE,
                    distortion TEXT DEFAULT '',
                    diagnosis TEXT DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
                
                # 自動遷移新欄位
                new_report_cols = [
                    ("cognitive_score", "INT DEFAULT 0"),
                    ("is_fake", "BOOLEAN DEFAULT FALSE"),
                    ("is_fragile", "BOOLEAN DEFAULT FALSE"),
                    ("distortion", "TEXT DEFAULT ''"),
                    ("diagnosis", "TEXT DEFAULT ''") 
                ]
                for col, dtype in new_report_cols:
                    try:
                        cur.execute(f"ALTER TABLE reports ADD COLUMN {col} {dtype}")
                    except psycopg2.errors.DuplicateColumn:
                        pass 
                    except Exception:
                        pass # 忽略其他錯誤

                cur.execute("""CREATE TABLE IF NOT EXISTS group_configs (
                    group_id TEXT PRIMARY KEY, ai_mode BOOLEAN DEFAULT FALSE, mode_type TEXT DEFAULT 'simple')""")

                vip_cols = [("cognitive_tier", "TEXT DEFAULT 'L1'"), ("tier_confidence", "FLOAT DEFAULT 0.0"), 
                            ("tr_tag", "TEXT"), ("tr_strategy", "TEXT"), ("tr_incantation", "TEXT")]
                for col, dtype in vip_cols:
                    try:
                        cur.execute(f"ALTER TABLE group_vips ADD COLUMN {col} {dtype}")
                    except psycopg2.errors.DuplicateColumn:
                        pass
                    except Exception:
                        pass

        print("✅ Schema Up-to-date!", file=sys.stderr, flush=True)
    except Exception as e:
        print(f"❌ Migration Failed: {e}", file=sys.stderr)
