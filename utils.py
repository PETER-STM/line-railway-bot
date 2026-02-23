import re
import sys
from datetime import datetime, timedelta
from linebot import LineBotApi
from linebot.exceptions import LineBotApiError
from database import get_db
from config import Config

line_bot_api = LineBotApi(Config.LINE_CHANNEL_ACCESS_TOKEN)

# ==========================================
# 🧹 正規表達式工具箱 (清潔大隊)
# ==========================================
NAME_CLEAN_REGEX = re.compile(r'[（(\[【].*?[)）\]】]')
DATE_CLEAN_REGEX = re.compile(r'\d{4}[./-]\d{1,2}[./-]\d{1,2}')
PREFIX_JUNK_REGEX = re.compile(r'^\d+[\.、\s]?')
EMOJI_REGEX = re.compile(r'[\U00010000-\U0010ffff]', flags=re.UNICODE)

def normalize_name(name):
    """
    [後台邏輯專用] 標準化姓名：
    強制過濾 Emoji，確保資料庫歸戶統一。
    """
    if not name: return ""
    name = EMOJI_REGEX.sub('', name)
    name = DATE_CLEAN_REGEX.sub('', name) 
    name = NAME_CLEAN_REGEX.sub('', name) 
    name = PREFIX_JUNK_REGEX.sub('', name) 
    return name.strip()

def extract_insight(content):
    """
    擷取心得重點
    """
    pattern = re.compile(r'(?:^|\n)\s*(?:5[\.、]\s*)?.*?(?:領悟|心得).*?[:：\?？]?\s*\n([\s\S]*?)(?=\n\s*6[\.、]|\n\s*付出不|\n\s*六項|$)', re.IGNORECASE)
    match = pattern.search(content)
    if match: return match.group(1).strip()[:200]
    
    lines = content.split('\n')
    valid_lines = [l for l in lines if len(l.strip()) > 5 and not l.strip().startswith('6.')]
    if valid_lines: return valid_lines[-1].strip()[:100]
    return "（學員未填寫心得）"

def calculate_missing_stats(group_id, start_date, end_date):
    """
    🔥 [統計邏輯 - 高效 SQL 優化版] (V23.25 修正日期格式對接)
    回傳: (缺交字典, 全勤名單列表, 狀態訊息)
    """
    try:
        # 1. 產生日期列表
        target_dates = []
        curr = start_date
        while curr <= end_date:
            target_dates.append(curr)
            curr += timedelta(days=1)
            
        with get_db() as conn:
            with conn.cursor() as cur:
                # 2. 抓取該群組所有 VIP 成員
                cur.execute("SELECT normalized_name, vip_name FROM group_vips WHERE group_id = %s", (group_id,))
                members = cur.fetchall()
                
                if not members: 
                    return {}, [], "⚠️ 群組尚無成員資料，請先請大家傳送日報。"

                # 3. 一次抓取該時段內「所有」日報
                query_end_date = end_date + timedelta(days=1)
                cur.execute("""
                    SELECT normalized_name, report_date 
                    FROM reports 
                    WHERE group_id = %s AND report_date >= %s AND report_date < %s
                """, (group_id, start_date, query_end_date))
                
                submitted_map = {}
                for row in cur.fetchall():
                    n_name = row[0]
                    r_date = row[1]
                    if isinstance(r_date, datetime): r_date = r_date.date()
                    
                    if n_name not in submitted_map: submitted_map[n_name] = set()
                    submitted_map[n_name].add(r_date)
                
                # 4. 比對缺交 & 全勤
                missing_report = {}
                completed_list = [] # 🔥 新增：全勤名單容器

                for norm_name, vip_name in members:
                    display_name = vip_name if vip_name else norm_name
                    user_submitted = submitted_map.get(norm_name, set())
                    
                    user_missing_dates = []
                    for d in target_dates:
                        if d not in user_submitted:
                            # 🔥 V23.25 關鍵修正：
                            # 這裡必須回傳完整年份 '%Y-%m-%d'，app.py 的壓縮演算法才能計算連續日期。
                            # app.py 接收後會自動轉回 '月/日' 的短格式顯示。
                            user_missing_dates.append(d.strftime('%Y-%m-%d'))
                    
                    if user_missing_dates:
                        missing_report[display_name] = user_missing_dates
                    else:
                        completed_list.append(display_name) # 🔥 沒缺交就是全勤
                
                if not missing_report: 
                    return {}, completed_list, "🎉 太神啦！這段時間 **全員全勤**！"
                
                return missing_report, completed_list, "OK"

    except Exception as e:
        print(f"❌ Stat Error: {e}", file=sys.stderr)
        return {}, [], f"💥 統計系統炸裂: {e}"

def get_group_name(group_id):
    try:
        summary = line_bot_api.get_group_summary(group_id)
        return summary.group_name
    except:
        return "未知群組"