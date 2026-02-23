import os
import re
import psycopg2
from datetime import datetime

# ==========================================
# ⚙️ 1. 設定區 (Configuration)
# ==========================================
# 🚨 請將這裡替換成您 Railway 上的 Database URL (PostgreSQL 連線字串)
DATABASE_URL = "postgresql://postgres:xYwUUdAgpujXplEGKXtmNsWlREiBnpju@switchyard.proxy.rlwy.net:22646/railway"

# 您上傳的檔案名稱與對應的 Group ID (請確保檔案與本腳本放在同一資料夾)
FILES_TO_PROCESS = [
    {"file": "[LINE]🐬海豚保時捷夢幻團隊🏖️⛷️🏂.txt", "group_id": "海豚保時捷夢幻團隊"},
    {"file": "[LINE]台中心得群組.txt", "group_id": "台中心得群組"}
]

# 🧠 終極人員名單字典 (將 LINE 暱稱轉換為系統標準名稱，確保戰功不漏接)
NAME_NORMALIZER = {
    # === 🚀 核心群組發言者 ===
    "王俊干🎩": "俊千",
    "施恩澤": "彼得",
    "明(魔術教學互動)": "連長",
    "𝕼𝖚𝖎𝖓𝖓♡⃛千金": "千金",
    "林慈修": "慈修",
    "Aien  Lu": "愛恩",
    "Aien Lu": "愛恩",
    "阿J❤️魔術": "阿傑",
    "海豚🐬": "海豚",
    "Carol Han浣熊🦝": "浣熊",
    "Eason 順榆": "Eason",    
    "🍮布丁": "布丁",
    "陸吾:(；ﾞﾟ'ωﾟ'):🐺": "奶油", 
    "邦": "邦妮",
    "邦妮": "邦妮",
    "吳仲明": "小明",
    "伯龍": "芒果",           
    "蔡橙❤️": "橙橙",         
    "Lily": "Lily",
    "花": "花",
    "小花": "花",

    # === 👥 日報中被提及的主管/夥伴/新人 ===
    "小郭": "小郭",
    "亮亮": "亮亮",
    "泰慶": "泰慶",
    "蘋果": "蘋果",
    "狐狸": "狐狸",
    "阿勳": "阿勳",
    "尚汶": "尚汶",
    "淇淇": "淇淇",
    "小牛": "小牛",
    "小海": "小海",
    "小李": "小李",
    "小藍": "小藍",
    "小綾": "小綾",
    "小楊": "小楊",
    "阿強": "阿強",
    "小徐": "小徐",
    "冬冬": "冬冬",
    "老蕭": "老蕭",
    "維尼": "維尼",
    "蛋糕": "蛋糕",
    "宗翰": "宗翰",
    "兔子": "兔子",
    "姍姍": "姍姍",
    "烏爾": "烏爾",
    "彩虹": "彩虹",
    "軍軍": "軍軍",
    "Max": "Max",
    "Allen": "Allen",
    "奇異果": "奇異果",
    "猴子": "猴子",
    "蛇蛇": "蛇蛇"
}

# ==========================================
# 🧠 2. 核心解析邏輯 (Parser Logic)
# ==========================================
def normalize_name(line_name):
    # 尋找是否包含在字典中，沒有的話就回傳原本的去空白名稱
    for key, std_name in NAME_NORMALIZER.items():
        if key in line_name or std_name in line_name:
            return std_name
    return line_name.strip()

def is_valid_report(text):
    """判斷這段文字是不是日報 (過濾掉一般的聊天打屁)"""
    if len(text) < 30: return False
    # 只要包含以下關鍵字中的兩個，就認定為日報
    keywords = ['感恩', '產值', '領悟', '做的不順', '不順', '精進', '心得', '改進']
    matches = sum(1 for k in keywords if k in text)
    return matches >= 2

def parse_line_chat(file_path, group_id):
    print(f"📂 正在解析檔案: {file_path} ...")
    with open(file_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    current_date = None
    current_msg = None
    reports = []

    # 捕捉日期標籤 (e.g., 2026.02.15 星期日)
    date_pattern = re.compile(r'^(\d{4}\.\d{2}\.\d{2})\s+星期.')
    
    for line in lines:
        raw_line = line
        line = line.strip()
        if not line: continue

        date_match = date_pattern.match(line)
        if date_match:
            current_date = datetime.strptime(date_match.group(1), '%Y.%m.%d').date()
            continue

        # 嘗試解析 LINE 的對話格式 (通常是以 Tab 分隔: 時間\t發言者\t內容)
        parts = raw_line.split('\t')
        if len(parts) >= 3 and re.match(r'^\d{2}:\d{2}$', parts[0].strip()):
            if current_msg and is_valid_report(current_msg['text']):
                reports.append(current_msg)

            time_str = parts[0].strip()
            sender = parts[1].strip()
            text = '\t'.join(parts[2:]).strip() + '\n'
            
            current_msg = {
                'group_id': group_id,
                'reporter_name': sender,
                'normalized_name': normalize_name(sender),
                'report_date': current_date,
                'text': text
            }
        # 如果不是 Tab 分隔，嘗試用空白分隔解析
        elif re.match(r'^(\d{2}:\d{2})\s+', line):
            msg_match = re.match(r'^(\d{2}:\d{2})\s+([^\s]+(?:[\s]+[^\s]+)*?)\s+(.*)', line)
            if msg_match:
                if current_msg and is_valid_report(current_msg['text']):
                    reports.append(current_msg)

                time_str, sender, text = msg_match.groups()
                current_msg = {
                    'group_id': group_id,
                    'reporter_name': sender,
                    'normalized_name': normalize_name(sender),
                    'report_date': current_date,
                    'text': text + '\n'
                }
        else:
            # 這行沒有時間戳，代表是上一則訊息的換行延續
            if current_msg:
                current_msg['text'] += line + '\n'

    # 迴圈結束後，檢查最後一則訊息
    if current_msg and is_valid_report(current_msg['text']):
        reports.append(current_msg)

    print(f"✅ 在 {file_path} 中找到了 {len(reports)} 篇有效日報！")
    return reports

# ==========================================
# 💾 3. 資料庫灌頂邏輯 (Database Injection)
# ==========================================
def inject_to_database(all_reports):
    print("\n🚀 開始將資料灌入 PostgreSQL ...")
    try:
        conn = psycopg2.connect(DATABASE_URL, sslmode='require')
        cur = conn.cursor()
        
        success_count = 0
        for r in all_reports:
            # 檢查是否已經存在 (避免重複匯入)
            cur.execute("""
                SELECT id FROM reports 
                WHERE group_id=%s AND normalized_name=%s AND report_date=%s
            """, (r['group_id'], r['normalized_name'], r['report_date']))
            
            if cur.fetchone():
                continue # 已存在則跳過

            # 寫入歷史日報
            cur.execute("""
                INSERT INTO reports (group_id, reporter_name, normalized_name, report_date, report_content, created_at)
                VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
            """, (r['group_id'], r['reporter_name'], r['normalized_name'], r['report_date'], r['text']))
            
            # 順便建立/更新 VIP 基本資料，讓貝氏引擎有對象可以追蹤
            cur.execute("""
                INSERT INTO group_vips (group_id, vip_name, normalized_name, last_report_date, current_streak, cognitive_tier, tier_confidence)
                VALUES (%s, %s, %s, %s, 1, 'L1', 0.0)
                ON CONFLICT (group_id, normalized_name) 
                DO UPDATE SET last_report_date = GREATEST(group_vips.last_report_date, EXCLUDED.last_report_date)
            """, (r['group_id'], r['reporter_name'], r['normalized_name'], r['report_date']))
            
            success_count += 1

        conn.commit()
        cur.close()
        conn.close()
        print(f"🎉 灌頂成功！總共匯入了 {success_count} 篇全新歷史日報到資料庫中！")
        
    except Exception as e:
        print(f"❌ 資料庫寫入失敗: {e}")

# ==========================================
# 🏃‍♂️ 4. 執行主程式
# ==========================================
if __name__ == "__main__":
    if DATABASE_URL == "postgres://您的帳號:密碼@伺服器位址:連接埠/資料庫名稱":
        print("⚠️ 警告：您還沒有填寫第 10 行的 DATABASE_URL (資料庫連線字串)！")
        print("請先替換為您 Railway 上的真實連線字串後再執行。")
        exit()

    all_reports = []
    for item in FILES_TO_PROCESS:
        if os.path.exists(item['file']):
            reports = parse_line_chat(item['file'], item['group_id'])
            all_reports.extend(reports)
        else:
            print(f"⚠️ 找不到檔案 {item['file']}，請確保它與此腳本放在同一個資料夾。")
            
    if all_reports:
        inject_to_database(all_reports)
    else:
        print("沒有找到任何可匯入的資料。")