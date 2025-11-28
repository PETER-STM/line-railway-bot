import os
import sys
import argparse
import psycopg2
import google.generativeai as genai
from linebot import LineBotApi
from linebot.models import TextSendMessage
from linebot.exceptions import LineBotApiError

# --- 環境變數讀取 ---
LINE_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
DB_URL = os.environ.get('DATABASE_URL')
GOOGLE_API_KEY = os.environ.get('GOOGLE_API_KEY')

if not DB_URL or not GOOGLE_API_KEY:
    print("FATAL: Missing environment variables (DATABASE_URL or GOOGLE_API_KEY).", file=sys.stderr)
    sys.exit(1)

# --- 初始化 AI ---
try:
    genai.configure(api_key=GOOGLE_API_KEY)
    # 使用 2.0 Flash 模型以獲得快速且高品質的摘要
    model = genai.GenerativeModel('gemini-2.0-flash')
except Exception as e:
    print(f"AI Init Error: {e}", file=sys.stderr)
    sys.exit(1)

def get_ai_summary(content):
    """將單一回報內容濃縮成一句話"""
    try:
        # Prompt 設計：要求客觀、簡潔、抓重點
        prompt = f"請將以下這份工作日報/心得，總結為一句話(包含重點進度與情緒狀態)，語氣請保持專業客觀，不要使用第一人稱，不要超過50個字：\n\n{content}"
        response = model.generate_content(prompt)
        return response.text.strip()
    except Exception as e:
        print(f"   (AI Error: {e})", file=sys.stderr)
        return "內容讀取失敗"

def run_summary(target_date_str, target_name=None, target_group_id=None, send_to_line=False):
    print(f"🚀 正在搜尋 {target_date_str} 的回報紀錄...", file=sys.stderr)
    if target_name:
        print(f"🔍 過濾條件：姓名包含 '{target_name}'", file=sys.stderr)
    
    conn = psycopg2.connect(DB_URL, sslmode='require')
    cur = conn.cursor()
    
    try:
        # 1. 動態建構 SQL 查詢
        sql = """
            SELECT group_id, reporter_name, report_content 
            FROM reports 
            WHERE report_date = %s
        """
        params = [target_date_str]

        # 篩選特定群組
        if target_group_id:
            sql += " AND group_id = %s"
            params.append(target_group_id)
        
        # 篩選特定人名 (模糊搜尋)
        if target_name:
            sql += " AND (reporter_name ILIKE %s OR normalized_name ILIKE %s)"
            params.extend([f"%{target_name}%", f"%{target_name}%"])

        sql += " ORDER BY group_id, created_at ASC"

        cur.execute(sql, tuple(params))
        rows = cur.fetchall()
        
        if not rows:
            print(f"📭 {target_date_str} 沒有找到符合條件的回報紀錄。", file=sys.stderr)
            return

        print(f"📄 找到 {len(rows)} 筆回報。正在分類與分析...", file=sys.stderr)

        # 2. 按群組分類資料
        # 結構: { group_id: [ (name, content), ... ] }
        reports_by_group = {}
        for gid, rname, content in rows:
            if gid not in reports_by_group:
                reports_by_group[gid] = []
            reports_by_group[gid].append((rname, content))

        # 3. 逐一群組產生報告並發送
        bot = LineBotApi(LINE_TOKEN) if send_to_line and LINE_TOKEN else None

        for gid, reports in reports_by_group.items():
            print(f"\nProcessing Group: {gid} ({len(reports)} reports)...")
            
            # 標題依據是否有篩選人名而變
            title = f"📊 【{target_date_str}】"
            title += f"{target_name} 的回報總結" if target_name else "團隊回報總結"
            
            summary_lines = [title, "---------------------------"]
            
            for name, content in reports:
                print(f"   -> Analyzing {name}...", file=sys.stderr)
                ai_summary = get_ai_summary(content)
                summary_lines.append(f"👤 **{name}**：\n{ai_summary}")
            
            summary_lines.append("---------------------------")
            
            final_msg = "\n".join(summary_lines)

            # 顯示預覽
            print(f"--- [預覽: {gid}] ---")
            print(final_msg)
            print("---------------------")

            # 發送動作
            if send_to_line and bot:
                try:
                    bot.push_message(gid, TextSendMessage(text=final_msg))
                    print(f"✅ 已發送到群組 {gid}", file=sys.stderr)
                except LineBotApiError as e:
                    print(f"❌ 發送失敗 (Group {gid}): {e}", file=sys.stderr)
            elif send_to_line and not bot:
                print("❌ 無法發送：缺少 Token", file=sys.stderr)
            else:
                print("🔒 安全模式：未發送 (使用 --send 啟用發送)", file=sys.stderr)

    except Exception as e:
        print(f"System Error: {e}", file=sys.stderr)
    finally:
        conn.close()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate daily summary report using AI.")
    parser.add_argument('--date', type=str, required=True, help="日期格式 (YYYY-MM-DD)")
    parser.add_argument('--name', type=str, help="指定特定人名 (選填，若不填則總結全體)")
    parser.add_argument('--group-id', type=str, help="指定特定群組 ID (選填，若不填則搜尋所有群組)")
    parser.add_argument('--send', action='store_true', help="加上此參數才會真的發送到 LINE")
    
    args = parser.parse_args()
    
    run_summary(args.date, args.name, args.group_id, args.send)