import os
import sys
import re
from datetime import datetime, timedelta
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError, LineBotApiError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, SourceGroup, SourceRoom, SourceUser
import psycopg2
# 引入 Google Gemini (如果 GOOGLE_API_KEY 有設置)
import google.generativeai as genai 
# from google.generativeai.errors import APIError # <--- 修正：舊版 SDK 的錯誤類別路徑已移除，改用 genai.APIError
from google.generativeai import APIError # 引入 APIError 以便處理模型錯誤

# --- 姓名正規化工具 (用於確保 VIP 記錄唯一性) ---
def normalize_name(name):
    """
    對人名進行正規化處理，主要移除開頭的班級或編號標記。
    例如: "(三) 浣熊🦝" -> "浣熊🦝"
    """
    # 移除開頭被括號 (圓括號、全形括號、方括號、書名號) 包裹的內容
    normalized = re.sub(r'^\s*[（(\[【][^()\[\]]{1,10}[)）\]】]\s*', '', name).strip()
    
    # 如果正規化結果為空，返回原始名稱
    return normalized if normalized else name

# --- 環境變數設定 ---
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
DATABASE_URL = os.environ.get('DATABASE_URL')
GOOGLE_API_KEY = os.environ.get('GOOGLE_API_KEY') # 新增：Gemini API Key
# NEW: 排除的群組ID列表 (用於測試功能時跳過某些群組)
EXCLUDE_GROUP_IDS_STR = os.environ.get('EXCLUDE_GROUP_IDS', '')
EXCLUDE_GROUP_IDS = set(EXCLUDE_GROUP_IDS_STR.split(',')) if EXCLUDE_GROUP_IDS_STR else set()

# --- 診斷與初始化 ---\
if not LINE_CHANNEL_ACCESS_TOKEN:
    sys.exit("LINE_CHANNEL_ACCESS_TOKEN is missing!")
if not LINE_CHANNEL_SECRET:
    sys.exit("LINE_CHANNEL_SECRET is missing!")

app = Flask(__name__)
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# --- AI 設定與初始化 ---
# 只有在 GOOGLE_API_KEY 存在時才初始化 Gemini
if GOOGLE_API_KEY:
    try:
        genai.configure(api_key=GOOGLE_API_KEY)
        # 為了相容性，使用 gemini-2.5-flash
        MODEL_NAME = 'gemini-2.5-flash' 
        print(f"Gemini API initialized with model: {MODEL_NAME}", file=sys.stderr)
    except Exception as e:
        print(f"Gemini API configuration failed: {e}", file=sys.stderr)
        # 即使配置失敗，也允許程式繼續執行，但 AI 相關功能將無法使用
        MODEL_NAME = None 
else:
    print("GOOGLE_API_KEY is missing. AI chat features disabled.", file=sys.stderr)
    MODEL_NAME = None

# --- AI 閒聊生成函式 ---
def generate_ai_reply(prompt):
    if not MODEL_NAME:
        return "AI 功能目前未啟用，請聯繫管理員檢查 GOOGLE_API_KEY 設定。"
    
    try:
        # 使用簡單的內容生成，不使用聊天歷史
        # 由於是閒聊，不強制使用 search grounding
        response = genai.generate_content(
            model=MODEL_NAME,
            contents=prompt
        )
        return response.text
    # 修正為直接引用引入的 APIError 類別
    except APIError as e: 
        print(f"Gemini API Error: {e}", file=sys.stderr)
        return "抱歉，AI 服務出了點小問題，請稍後再試。"
    except Exception as e:
        print(f"General AI Error: {e}", file=sys.stderr)
        return "抱歉，AI 處理請求時發生了未知錯誤。"


# --- 資料庫連線函式 ---
def get_db_connection():
    # 確保 DATABASE_URL 已設置
    if not DATABASE_URL:
        print("DATABASE_URL is missing!", file=sys.stderr)
        return None
        
    try:
        conn = psycopg2.connect(DATABASE_URL)
        return conn
    except Exception as e:
        print(f"Database connection error: {e}", file=sys.stderr)
        return None

# --- 資料庫操作：記錄心得回報/打卡 ---
def log_report(group_id, report_date, reporter_name):
    """
    記錄心得回報/打卡。
    """
    conn = get_db_connection()
    if not conn:
        return "❌ 資料庫連線失敗，請稍後再試！"

    # 正規化人名
    normalized_name = normalize_name(reporter_name)

    # 檢查是否已記錄過 (同一群組、同一天、正規化後的人名)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM reports WHERE group_id = %s AND report_date = %s AND normalized_name = %s",
                (group_id, report_date, normalized_name)
            )
            count = cur.fetchone()[0]

            if count > 0:
                # 已存在記錄
                return f"📝 {reporter_name}，你已經在 {report_date.strftime('%Y/%m/%d')} 記錄過了哦！無需重複打卡。"

            # 插入新記錄
            cur.execute(
                "INSERT INTO reports (group_id, report_date, reporter_name, normalized_name, created_at) VALUES (%s, %s, %s, %s, NOW())",
                (group_id, report_date, reporter_name, normalized_name)
            )
            conn.commit()
            return f"✅ {reporter_name} 已經成功在 {report_date.strftime('%Y/%m/%d')} 報到！恭喜你完成了今天的學習目標！"

    except Exception as e:
        conn.rollback()
        print(f"Database INSERT error: {e}", file=sys.stderr)
        return f"❌ 資料庫寫入錯誤：{e}，請聯繫管理員！"
    finally:
        conn.close()

# --- 資料庫操作：查詢 VIP 名單 ---
def get_vip_list(group_id):
    """
    查詢特定群組的 VIP 名單 (每行一筆記錄)。
    """
    conn = get_db_connection()
    if not conn:
        return None, "❌ 資料庫連線失敗，請稍後再試！"
    
    try:
        with conn.cursor() as cur:
            # 查詢 group_modes 表，取得 vip_list
            cur.execute(
                "SELECT vip_list FROM group_modes WHERE group_id = %s",
                (group_id,)
            )
            result = cur.fetchone()
            
            if result and result[0]:
                # vip_list 是一個 TEXT 欄位，每行一個 VIP 姓名
                vip_names = [name.strip() for name in result[0].split('\n') if name.strip()]
                # 正規化所有名稱
                normalized_vips = {normalize_name(name): name for name in vip_names}
                return normalized_vips, None # 返回正規化後的字典 {normalized_name: original_name}
            else:
                return {}, "ℹ️ 這個群組尚未設定 VIP 名單！請使用『VIP名單 設定 [名單內容]』來設定。"
                
    except Exception as e:
        print(f"Database SELECT VIP list error: {e}", file=sys.stderr)
        return None, f"❌ 查詢 VIP 名單時發生錯誤：{e}"
    finally:
        conn.close()

# --- 資料庫操作：設定 VIP 名單 ---
def set_vip_list(group_id, vip_list_content):
    """
    設定特定群組的 VIP 名單。
    """
    conn = get_db_connection()
    if not conn:
        return "❌ 資料庫連線失敗，請稍後再試！"
        
    try:
        with conn.cursor() as cur:
            # 使用 UPSERT 語法 (INSERT OR UPDATE)
            # 檢查是否存在
            cur.execute(
                "SELECT COUNT(*) FROM group_modes WHERE group_id = %s",
                (group_id,)
            )
            exists = cur.fetchone()[0]
            
            if exists:
                # 更新
                cur.execute(
                    "UPDATE group_modes SET vip_list = %s, updated_at = NOW() WHERE group_id = %s",
                    (vip_list_content, group_id)
                )
                action = "更新"
            else:
                # 插入 (同時設定預設模式為 'REPORT')
                cur.execute(
                    "INSERT INTO group_modes (group_id, mode, vip_list, created_at, updated_at) VALUES (%s, %s, %s, NOW(), NOW())",
                    (group_id, 'REPORT', vip_list_content)
                )
                action = "設定"

            conn.commit()
            
            # 重新檢查並列出 VIP 名單
            vip_names = [name.strip() for name in vip_list_content.split('\n') if name.strip()]
            list_of_names = "\n".join([f"- {name}" for name in vip_names])
            
            return f"✅ VIP 名單已成功{action}！\n\n目前 VIP ({len(vip_names)}人)：\n{list_of_names}"
            
    except Exception as e:
        conn.rollback()
        print(f"Database SET VIP list error: {e}", file=sys.stderr)
        return f"❌ 設定 VIP 名單時發生錯誤：{e}"
    finally:
        conn.close()


# --- 資料庫操作：取得群組模式 ---
def get_group_mode(group_id):
    """
    取得特定群組的運作模式 ('REPORT' 或 'AI')，預設為 'REPORT'。
    """
    conn = get_db_connection()
    if not conn:
        # 如果無法連線資料庫，預設為 REPORT 模式
        return 'REPORT'

    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT mode FROM group_modes WHERE group_id = %s",
                (group_id,)
            )
            result = cur.fetchone()
            
            # 如果 group_modes 中有記錄，返回其 mode，否則返回預設 'REPORT'
            return result[0] if result else 'REPORT'
                
    except Exception as e:
        print(f"Database GET group mode error: {e}", file=sys.stderr)
        # 發生錯誤時，返回預設 'REPORT'
        return 'REPORT'
    finally:
        conn.close()

# --- 資料庫操作：設定群組模式 ---
def set_group_mode(group_id, mode):
    """
    設定特定群組的運作模式 ('REPORT' 或 'AI')。
    """
    if mode not in ['REPORT', 'AI']:
        return "❌ 模式設定錯誤，模式只能是 'REPORT' 或 'AI'！"

    conn = get_db_connection()
    if not conn:
        return "❌ 資料庫連線失敗，請稍後再試！"
        
    try:
        with conn.cursor() as cur:
            # 使用 UPSERT 語法 (INSERT OR UPDATE)
            # 檢查是否存在
            cur.execute(
                "SELECT COUNT(*) FROM group_modes WHERE group_id = %s",
                (group_id,)
            )
            exists = cur.fetchone()[0]
            
            if exists:
                # 更新
                cur.execute(
                    "UPDATE group_modes SET mode = %s, updated_at = NOW() WHERE group_id = %s",
                    (mode, group_id)
                )
                action = "更新"
            else:
                # 插入 (同時 vip_list 預設為空)
                cur.execute(
                    "INSERT INTO group_modes (group_id, mode, vip_list, created_at, updated_at) VALUES (%s, %s, %s, NOW(), NOW())",
                    (group_id, mode, '')
                )
                action = "設定"

            conn.commit()
            return f"✅ 群組模式已成功{action}為：『{mode}』模式！"
            
    except Exception as e:
        conn.rollback()
        print(f"Database SET group mode error: {e}", file=sys.stderr)
        return f"❌ 設定群組模式時發生錯誤：{e}"
    finally:
        conn.close()


# --- 主要訊息處理函式 ---
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    text = event.message.text.strip()
    reply_text = None
    source = event.source
    
    # 僅處理群組/聊天室訊息
    if isinstance(source, SourceGroup):
        group_id = source.group_id
    elif isinstance(source, SourceRoom):
        group_id = source.room_id
    else:
        # 忽略個人聊天訊息
        return

    # 排除特定測試群組
    if group_id in EXCLUDE_GROUP_IDS:
        print(f"Ignoring message from excluded group: {group_id}", file=sys.stderr)
        return

    # --- 1. 處理指令 (優先處理) ---
    
    # VIP 名單查詢/設定
    if text.startswith('VIP名單'):
        parts = text.split()
        if len(parts) == 1 or (len(parts) == 2 and parts[1] in ['查詢', '查', 'list']):
            # VIP名單 或 VIP名單 查詢
            vips, error_msg = get_vip_list(group_id)
            if error_msg:
                reply_text = error_msg
            else:
                original_names = sorted(vips.values())
                list_of_names = "\n".join([f"- {name}" for name in original_names])
                reply_text = f"📋 群組 VIP 名單 ({len(original_names)}人)：\n{list_of_names}"
        
        elif len(parts) >= 3 and parts[1] in ['設定', 'set']:
            # VIP名單 設定 ...
            vip_list_content = ' '.join(parts[2:])
            # 處理多行輸入 (用逗號、分號或空格分隔)
            if ',' in vip_list_content or '；' in vip_list_content:
                names = re.split(r'[;；,]', vip_list_content)
                vip_list_content = '\n'.join([name.strip() for name in names if name.strip()])

            if not vip_list_content:
                reply_text = "⚠️ 請提供要設定的 VIP 名單內容！"
            else:
                reply_text = set_vip_list(group_id, vip_list_content)

    # 模式切換
    elif text.startswith('模式'):
        parts = text.split()
        if len(parts) == 1:
            # 模式 (查詢當前模式)
            current_mode = get_group_mode(group_id)
            reply_text = f"⚙️ 目前模式為：『{current_mode}』。\n\n切換指令：\n- 模式 報到\n- 模式 AI"
        elif len(parts) == 2 and parts[1] in ['報到', 'REPORT', 'report']:
            # 模式 報到
            if get_group_mode(group_id) == 'REPORT':
                reply_text = "ℹ️ 目前已是『REPORT』報到模式，無需切換。"
            else:
                reply_text = set_group_mode(group_id, 'REPORT')
        elif len(parts) == 2 and parts[1] in ['AI', 'ai', '閒聊']:
            # 模式 AI
            if get_group_mode(group_id) == 'AI':
                reply_text = "ℹ️ 目前已是『AI』閒聊模式，無需切換。"
            else:
                # 檢查 AI 服務是否可用
                if not MODEL_NAME:
                    reply_text = "❌ 由於 GOOGLE_API_KEY 缺失，AI 模式無法啟用。"
                else:
                    reply_text = set_group_mode(group_id, 'AI')


    # 幫助指令
    elif text in ['幫助', 'help', '功能', '指令']:
        reply_text = (
            "🤖 心得打卡機器人功能說明 🤖\n\n"
            "1. **心得報到 (REPORT 模式)**\n"
            "   - 格式: `YYYY.MM.DD 姓名`\n"
            "   - 範例: `2025.11.24 浣熊🦝`\n"
            "2. **VIP 名單管理**\n"
            "   - 查詢: `VIP名單 查詢`\n"
            "   - 設定: `VIP名單 設定 [名單內容]` (一行一位，或用逗號/分號分隔)\n"
            "   - 範例: `VIP名單 設定 (三) 浣熊🦝\n(二) 狐狸🦊`\n"
            "3. **群組模式切換**\n"
            "   - 查詢模式: `模式`\n"
            "   - 切換報到: `模式 報到`\n"
            "   - 切換 AI 閒聊: `模式 AI`\n"
        )
    
    # --- 2. 處理心得報到 (僅在 REPORT 模式下) ---
    if not reply_text and get_group_mode(group_id) == 'REPORT':
        # 檢查是否符合心得回報格式 (YYYY.MM.DD 姓名)
        # 正則表達式： (\d{4}[./]\d{2}[./]\d{2})\s+(.+)
        match_report = re.match(r"^(\d{4}[./]\d{2}[./]\d{2})\s+(.+)$", text)
        
        if match_report:
            date_str = match_report.group(1) # 日期是第一個捕獲組
            name_str = match_report.group(2).strip() # 人名是第二個捕獲組

            try:
                # 轉換分隔符號為點號，以便統一解析
                date_str = date_str.replace('/', '.') 
                report_date = datetime.strptime(date_str, '%Y.%m.%d').date()
                reporter_name = name_str
                
                # 確保人名不為空
                if not reporter_name:
                    # 記錄回報 (人名遺失) 模板
                    reply_text = "⚠️ 日期後面請記得加上人名，不然我不知道誰交的啊！\n\n（你總不會想讓我自己猜吧？）"
                else:
                    # 呼叫 log_report，只記錄打卡資訊
                    reply_text = log_report(group_id, report_date, reporter_name)
                
            except ValueError:
                # 記錄回報 (日期格式錯誤) 模板
                reply_text = "❌ 日期長得怪怪的。\n\n請用標準格式：YYYY.MM.DD 姓名\n\n（小數點不是你的自由發揮。）"

    # --- 3. AI 閒聊 ---
    if not reply_text and get_group_mode(group_id) == 'AI':
        reply_text = generate_ai_reply(text)

    # 發送回覆訊息
    if reply_text:
        try:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=reply_text)
            )
        except LineBotApiError as e:
            print(f"LINE API PUSH/REPLY ERROR: {e}", file=sys.stderr)
            pass 

# --- Webhook 主入口 ---
@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        print("Invalid signature. Please check your channel access token/secret.")
        abort(400)
    except Exception as e:
        print(f"Error handling webhook: {e}", file=sys.stderr)
        abort(500)

    return 'OK'

# --- 啟動 Flask 應用程式 ---
if __name__ == "__main__":
    # 使用 os.getenv 而不是 os.environ.get，因為我們在診斷區塊檢查過了
    port = int(os.getenv("PORT", 8080))
    app.run(host='0.0.0.0', port=port)