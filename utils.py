import re
import sys
from datetime import datetime, timedelta
from linebot import LineBotApi
from linebot.exceptions import LineBotApiError
from database import get_db
from config import Config

line_bot_api = LineBotApi(Config.LINE_CHANNEL_ACCESS_TOKEN)

# 正規表達式工具
NAME_CLEAN_REGEX = re.compile(r'[（(\[【].*?[)）\]】]')
DATE_CLEAN_REGEX = re.compile(r'\d{4}[./-]\d{1,2}[./-]\d{1,2}')
PREFIX_JUNK_REGEX = re.compile(r'^\d+[\.、\s]?')

def normalize_name(name):
    """標準化姓名：去除日期、括號內容、數字前綴"""
    if not name: return ""
    name = DATE_CLEAN_REGEX.sub('', name) # 去除日期
    name = NAME_CLEAN_REGEX.sub('', name) # 去除括號
    name = PREFIX_JUNK_REGEX.sub('', name) # 去除 "1." 這種前綴
    return name.strip()

def extract_insight(content):
    """擷取心得重點 (Regex 優化版)"""
    pattern = re.compile(r'(?:^|\n)\s*(?:5[\.、]\s*)?.*?(?:領悟|心得).*?[:：\?？]?\s*\n([\s\S]*?)(?=\n\s*6[\.、]|\n\s*付出不亞於|\n\s*\d{1,2}[\.、]|$)')
    match = pattern.search(content)
    if match:
        return match.group(1).strip()[:800]
    return "無特殊領悟 (或是格式未能抓取)"

def get_group_name(group_id):
    try:
        summary = line_bot_api.get_group_summary(group_id)
        return summary.group_name
    except:
        return f"群組({group_id[-4:]})"

def calculate_missing_stats(group_id, start_date, end_date):
    """
    [核心重構 - 邊界修正版] 
    查詢結束日期往後延一天，確保 end_date 當天日報不會因為邊界問題被切掉。
    """
    try:
        # 1. 產生目標日期列表
        delta = (end_date - start_date).days + 1
        target_dates = [start_date + timedelta(days=i) for i in range(delta)]
        
        # 🔥 [修正重點] SQL 查詢範圍加一天
        query_end_date = end_date + timedelta(days=1)

        with get_db() as conn:
            with conn.cursor() as cur:
                # 2. 撈取群組成員
                cur.execute("SELECT normalized_name, vip_name FROM group_vips WHERE group_id = %s", (group_id,))
                members_rows = cur.fetchall()
                members = {row[0]: row[1] for row in members_rows}
                
                if not members: 
                    return {}, "❌ 本群組尚無成員資料，請先讓大家打一次日報。"
                
                # 3. 撈取提交記錄 (使用 query_end_date)
                cur.execute("""
                    SELECT normalized_name, report_date 
                    FROM reports 
                    WHERE group_id = %s AND report_date >= %s AND report_date < %s
                """, (group_id, start_date, query_end_date))
                
                submitted = {norm: set() for norm in members}
                for row in cur.fetchall():
                    r_name = row[0]
                    r_date = row[1]
                    if isinstance(r_date, datetime):
                        r_date = r_date.date()
                        
                    if r_name in submitted:
                        submitted[r_name].add(r_date)
                
                # 4. 比對缺交
                missing_report = {}
                for norm_name, display_name in members.items():
                    missing_dates = []
                    for d in target_dates:
                        if d not in submitted[norm_name]:
                            missing_dates.append(d.strftime('%m/%d'))
                    
                    if missing_dates:
                        missing_report[display_name] = missing_dates
                
                if not missing_report: 
                    return {}, "🎉 太神啦！這段時間 **全員全勤**！"
                
                return missing_report, "OK"

    except Exception as e:
        print(f"❌ 計算缺交錯誤: {e}", file=sys.stderr)
        return {}, f"💥 統計系統炸裂：{e}"