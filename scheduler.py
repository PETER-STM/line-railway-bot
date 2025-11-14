# scheduler.py - 週報表生成腳本
import os
import psycopg2
from datetime import datetime, timedelta, date
from collections import defaultdict
from linebot import LineBotApi
from linebot.models import TextSendMessage

# -----------------
# 1. 初始化設定與連線
# -----------------

# Line API 初始化 (用於發送主動訊息)
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN") or os.environ.get("ACCESS_TOKEN")
if not LINE_CHANNEL_ACCESS_TOKEN:
    print("Error: LINE_CHANNEL_ACCESS_TOKEN not set.")
    exit()
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)

def get_db_connection():
    """使用環境變數連線到 PostgreSQL"""
    conn_url = os.environ.get("DATABASE_URL")
    if not conn_url:
        try:
            conn = psycopg2.connect(
                host=os.environ.get('PGHOST'),
                database=os.environ.get('PGDATABASE'),
                user=os.environ.get('PGUSER'),
                password=os.environ.get('PGPASSWORD'),
                port=os.environ.get('PGPORT')
            )
            return conn
        except Exception as e:
            print(f"Database connection failed: {e}")
            return None
    return psycopg2.connect(conn_url)


def get_report_data(conn, start_date, end_date):
    """從 reports 表格中獲取指定週期內的回報資料"""
    sql = """
    SELECT report_date, name, source_id
    FROM reports
    WHERE report_date BETWEEN %s AND %s
    """
    cur = conn.cursor()
    cur.execute(sql, (start_date, end_date))
    data = cur.fetchall()
    cur.close()
    return data

def get_all_reporters(conn):
    """從 group_reporters 表格中獲取所有回報人及其所屬群組 ID"""
    sql = "SELECT group_id, reporter_name FROM group_reporters"
    cur = conn.cursor()
    cur.execute(sql)
    data = cur.fetchall()
    cur.close()
    
    # 將結果整理成 {group_id: [name1, name2, ...]} 格式
    reporters_by_group = defaultdict(set)
    for group_id, name in data:
        reporters_by_group[group_id].add(name)
    
    return {k: list(v) for k, v in reporters_by_group.items()}

# -----------------
# 2. 核心統計邏輯
# -----------------

def run_weekly_report():
    conn = get_db_connection()
    if not conn:
        return

    # 1. 定義統計週期 (假設統計前 7 天)
    end_date = date.today() - timedelta(days=1)
    start_date = end_date - timedelta(days=6)
    
    # 建立週期內所有日期的列表
    date_list = [start_date + timedelta(days=i) for i in range(7)]
    
    # 2. 獲取所有回報人資料
    all_reporters = get_all_reporters(conn)
    
    # 3. 獲取所有回報紀錄
    all_reports = get_report_data(conn, start_date, end_date)
    conn.close()

    # 將回報紀錄轉換為 (source_id, name, date) 的集合，方便快速查詢
    reported_set = set((source_id, name, report_date) for report_date, name, source_id in all_reports)
    
    # 4. 初始化結果字典 {group_id: {name: miss_count}}
    missed_reports = defaultdict(lambda: defaultdict(int))
    
    # 5. 遍歷所有群組、所有回報人、所有日期，計算缺席次數
    for group_id, reporters in all_reporters.items():
        for name in reporters:
            miss_count = 0
            for current_date in date_list:
                # 檢查該回報人/日期/群組 是否在已回報的集合中
                if (group_id, name, current_date) not in reported_set:
                    miss_count += 1
                    
            if miss_count > 0:
                missed_reports[group_id][name] = miss_count

    # 6. 生成報告並發送
    report_message = generate_report_message(start_date, end_date, missed_reports)
    send_reports(report_message)


# -----------------
# 3. 訊息發送與格式化
# -----------------

def generate_report_message(start_date, end_date, missed_reports):
    """根據統計結果生成格式化的報告訊息"""
    
    # 報告標題
    header = f"🗓️ **【週報結算報告】** 🗓️\n週期: {start_date.strftime('%Y/%m/%d')} - {end_date.strftime('%Y/%m/%d')}\n\n"
    
    reports_by_group = defaultdict(str)
    
    # 遍歷所有群組
    for group_id, misses in missed_reports.items():
        if not misses:
            continue
            
        group_summary = "❌ **缺席統計**：\n"
        
        # 遍歷該群組內有缺席的人員
        sorted_misses = sorted(misses.items(), key=lambda item: item[1], reverse=True)
        
        for name, count in sorted_misses:
            group_summary += f"  - **{name}**: 缺席 **{count}** 次\n"
            
        # 只有在群組有缺席情況時才發送報告
        reports_by_group[group_id] = header + group_summary
        
    return reports_by_group

def send_reports(reports_by_group):
    """將報告發送到對應的 Line 群組/聊天室"""
    if not reports_by_group:
        print("No missed reports found. Skipping message sending.")
        return
        
    for group_id, message in reports_by_group.items():
        try:
            line_bot_api.push_message(
                to=group_id,
                messages=TextSendMessage(text=message)
            )
            print(f"Report sent to {group_id}")
        except Exception as e:
            print(f"Failed to send message to {group_id}: {e}")

if __name__ == "__main__":
    run_weekly_report()