import sys
import re
import time
import json
import random
import threading
import ast
from datetime import datetime
import pytz
import google.generativeai as genai
from config import Config

GLOBAL_MODEL_NAME = "gemini-2.5-flash"

def clean_markdown(text):
    if not text: return ""
    text = text.replace('**', '').replace('__', '').replace('##', '').replace('```json', '').replace('```', '')
    if '{' in text and '}' in text: 
        text = text[text.find('{'):text.rfind('}')+1]
    return re.sub(r'\n{3,}', '\n\n', text).strip()

def safe_parse_json(text):
    text = clean_markdown(text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        try:
            return ast.literal_eval(text)
        except:
            return {
                "reply": "阿摩正在整理思緒... (系統繁忙)",
                "score": 1,
                "is_fake": False,
                "is_fragile": False,
                "distortion": "系統格式錯誤",
                "mentor_choice": "一般模式",
                "mode_name": "系統修復中",
                "amor_os": "..."
            }

def clean_os_text(text):
    if not text: return "..."
    text = text.replace("(OS：", "").replace("(OS:", "").replace("(os:", "").replace("OS：", "")
    text = text.replace(")", "").replace("）", "") 
    return text.strip()

def clean_mode_name(text):
    if not text: return "阿摩亂入"
    return text.replace("模式", "").strip()

class DummyResponse:
    def __init__(self): self.text = "🤖 (阿摩正在數錢，暫時沒空理你...)"

class SafeGeminiModel:
    def __init__(self, name):
        safety = [
            {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"}
        ]
        self.model = genai.GenerativeModel(name, safety_settings=safety)
        self.name = name
    
    def generate_content(self, prompt):
        for attempt in range(3):
            try: return self.model.generate_content(prompt)
            except Exception as e: 
                print(f"⚠️ Gemini API Error (Attempt {attempt+1}): {e}", file=sys.stderr)
                time.sleep(1 * (attempt + 1))
                continue
        return DummyResponse()

def init_gemini():
    if not Config.GOOGLE_API_KEY: return None
    genai.configure(api_key=Config.GOOGLE_API_KEY)
    return SafeGeminiModel(GLOBAL_MODEL_NAME)

model = init_gemini()

def merge_tags_with_ai(conn, group_id, normalized_name, new_observation, source_type="self"):
    try:
        old_tags = ""
        with conn.cursor() as cur:
            cur.execute("SELECT personality FROM group_vips WHERE group_id=%s AND normalized_name=%s", (group_id, normalized_name))
            row = cur.fetchone()
            if row and row[0]:
                old_tags = row[0]
        
        context_msg = "這是他自己的日報心得" if source_type == "self" else "這是夥伴對他的觀察評價"
        prompt = (
            f"你是高階心理側寫師。正在更新用戶『{normalized_name}』的性格檔案。\n"
            f"📥 **現有標籤**：{old_tags if old_tags else '(無)'}\n"
            f"🆕 **今日新發現** ({context_msg})：『{new_observation}』\n\n"
            f"⚡ **任務：標籤融合與進化**\n"
            f"請綜合「舊標籤」與「新發現」，提煉出 **最精準的 4-6 個關鍵詞**。\n"
            f"   - 格式：[標籤1][標籤2]\n"
        )
        res = model.generate_content(prompt)
        matches = re.findall(r'\[.*?\]', res.text.strip())
        valid_tags = [tag for tag in matches if len(tag) <= 12]
        final_tags = "".join(valid_tags[:6]) 
        if final_tags:
            with conn.cursor() as cur:
                cur.execute("UPDATE group_vips SET personality=%s WHERE group_id=%s AND normalized_name=%s", 
                            (final_tags, group_id, normalized_name))
            conn.commit()
    except Exception: pass

def analyze_peer_interaction(conn, group_id, reporter_name, full_report_content):
    if not model: return
    member_map = {} 
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT normalized_name FROM group_vips WHERE group_id=%s", (group_id,))
            for r in cur.fetchall():
                if r[0] != reporter_name: member_map[r[0]] = r[0] 
    except Exception: return
    if not member_map: return

    prompt = (
        f"分析這篇日報中，作者『{reporter_name}』對其他夥伴的『觀察或評價』。\n"
        f"夥伴名單：[{'、'.join(member_map.keys())}]\n"
        f"日報內容：『{full_report_content}』\n"
        f"輸出 JSON Array：[{{'name': '夥伴名', 'observation': '特質'}}]"
    )
    try:
        res = model.generate_content(prompt)
        mentions = safe_parse_json(res.text)
        if mentions and isinstance(mentions, list):
            for item in mentions:
                target_name = item.get('name')
                observation = item.get('observation')
                found_name = next((real_name for real_name in member_map if target_name in real_name or real_name in target_name), None)
                if found_name and observation:
                    merge_tags_with_ai(conn, group_id, found_name, observation, source_type="peer")
    except Exception: pass

def evaluate_and_evolve_strategy(conn, group_id, normalized_name, insight, current_tr_data, streak):
    if not model or streak < 3: return 
    prompt = f"評估進化：{normalized_name}(連{streak})。心得：『{insight}』。無則回 FALSE。有則產出 5 欄 JSON。"
    try:
        res = model.generate_content(prompt)
        text = clean_markdown(res.text)
        if "FALSE" in text.upper(): return
        new_plan = safe_parse_json(text)
        with conn.cursor() as cur:
            cur.execute("""UPDATE group_vips SET tr_tag=%s, tr_strategy=%s, tr_concept=%s, tr_incantation=%s, tr_instruction=%s WHERE group_id=%s AND normalized_name=%s""", 
                (new_plan.get('tr_tag'), new_plan.get('tr_strategy'), new_plan.get('tr_concept'), new_plan.get('tr_incantation'), new_plan.get('tr_instruction'), group_id, normalized_name))
        conn.commit()
    except: pass

def analyze_and_update_personality(conn, group_id, normalized_name, full_report_content, old_personality):
    if not model or len(full_report_content) < 10: return
    merge_tags_with_ai(conn, group_id, normalized_name, full_report_content, source_type="self")

ROAST_PACKS = [
    {"theme": "💰 金融慣老闆", "metaphor": "用『資產負債表』的比喻。", "example_os": "(OS：他在感動自己，我在計算虧損。)"},
    {"theme": "⚔️ 戰場指揮官", "metaphor": "用『戰爭與生存』的比喻。", "example_os": "(OS：上了戰場還嫌槍重？)"},
    {"theme": "⚖️ 無情法官", "metaphor": "用『證據與判決』的比喻。", "example_os": "(OS：駁回。你的眼淚不能當呈堂證供。)"},
    {"theme": "🏋️‍♂️ 魔鬼健身教練", "metaphor": "用『肌肉與脂肪』的比喻。", "example_os": "(OS：你是在練業績還是練嘴皮子？)"}
]

INAMORI_PACKS = [
    {"theme": "🧘 嚴厲禪師", "metaphor": "用『修行與雜念』的比喻。", "example_os": "(OS：心中雜草叢生，難怪開不出智慧之花。)"},
    {"theme": "🔨 靈魂工匠", "metaphor": "用『打磨與淬鍊』的比喻。", "example_os": "(OS：這塊石頭還沒磨亮，就先喊痛了。)"}
]

def generate_ai_reply(trigger, **kwargs):
    insight = kwargs.get('insight', '')
    full_report = kwargs.get('full_report', '')
    full_text_check = full_report if full_report else insight
    
    history_context = kwargs.get('history_context', '（無歷史資料）')
    name = kwargs.get('name', '學員')
    streak = kwargs.get('streak', 1)
    personality = kwargs.get('personality', '無特殊標籤')
    tr_data = kwargs.get('tr_data') or {}
    cognitive_tier = tr_data.get('cognitive_tier', 'L1')
    incantation = tr_data.get('incantation') or "我每一天都比昨天更強大！"
    
    tw_now = datetime.now(pytz.timezone('Asia/Taipei'))
    today_key = (tw_now.month, tw_now.day)
    holiday_instruction = ""
    if today_key in Config.HOLIDAY_MODES:
        holiday_cfg = Config.HOLIDAY_MODES[today_key]
        holiday_instruction = f"🎉 特殊節慶 ({today_key[0]}/{today_key[1]})：{holiday_cfg} 必須結合節日梗。"

    inc_msg = f"\n\n🗣️ 請在群組大聲打出：「{incantation}」"
    
    if trigger != "report_success":
        if trigger == "sales_coach":
            prompt = (f"{Config.AMOR_PERSONA}\n學員難題：「{kwargs.get('question')}」。任務：銷售軍師。")
        elif trigger == "chat_mode":
            prompt = f"{Config.AMOR_PERSONA}\n學員說：「{kwargs.get('user_msg')}」\n回應：簡短毒舌，打斷藉口。"
        else:
            prompt = f"{Config.AMOR_PERSONA}\n任務：結算缺交。名單：{kwargs.get('sinners')}。總扣點數：{kwargs.get('total_fine')}點。"
        res = model.generate_content(prompt)
        return {"text": clean_markdown(res.text), "score": 0, "is_fake": False, "is_fragile": False, "distortion": "無"}

    has_revenue = False
    success_keywords = ["收單", "成交", "開市", "賣出", "業績", "入帳", "一槍兩彈"]
    if any(k in full_text_check for k in success_keywords):
        has_revenue = True
        fact_check_msg = "🚨 **[系統強制警告]：學員今日有『收單/成交』紀錄！嚴禁嘲諷『零產值』或『掛蛋』。**"
    else:
        fact_check_msg = "🚨 **[系統提示]：學員今日似乎無業績關鍵字，可針對『產值』進行檢視。**"

    prompt = (
        f"{Config.AMOR_PERSONA}\n"
        f"你是具備『認知審計能力』的教練。進行日報審核。\n"
        f"👤 學員：{name} (連{streak}天) | 🧠 內部評級：{cognitive_tier}\n"
        f"🏷️ **心理側寫**：{personality}\n"
        f"📜 歷史趨勢：\n{history_context}\n\n"
        f"📋 **日報全文**：\n{full_report}\n\n"
        f"{fact_check_msg}\n\n"
        f"⚡ **任務：深度認知病理診斷 (V16.28 Logic Update)**\n"
        f"請輸出 **JSON**，嚴格遵守以下邏輯：\n"
        f"1. **先檢查『最有產值的事』**：若學員有寫具體行動（如陌開、拜訪），請先認可其『勞動力』，再批評其『轉換率』低。\n"
        f"2. `is_fragile` (bool): **ICU 重症判定 (邏輯鬆綁版)**。\n"
        f"   - 只有當內容呈現『徹底崩潰』、『持續性無助』且『完全沒有轉念或行動』時，才設為 True。\n"
        f"   - **⚠️ 關鍵例外 (Hero's Journey)**：若學員提到『想放棄』但隨後提到『但我轉念了』、『還是完成了』或『成交了』，**嚴禁設為 True！**\n"
        f"3. `is_fake` (bool): **虛假反思偵測 (嚴格限縮)**。\n"
        f"   - **嚴禁將『流水帳』、『旅遊日記』判為 Fake**。這些內容請給低分(1-2分)並毒舌點評即可。\n"
        f"4. `distortion` (string): 認知扭曲類型。\n"
        f"5. `score` (int): Bloom 評分 (1-6)。\n"
        f"6. `mentor_choice` (string): **AI 語意路由**。若 is_fragile=True，**強制選『稻盛和夫』**。\n"
        f"   {json.dumps(ROAST_PACKS, ensure_ascii=False)}\n"
        f"   {json.dumps(INAMORI_PACKS, ensure_ascii=False)}\n"
        f"7. `mode_name` (string): 選擇最適合的模式名稱。\n"
        f"8. `reply` (string): 回覆內容 (120字內)。**若選擇稻盛和夫，嚴禁提及金錢/資產/負債，請專注於心性/利他/磨練。**\n"
        f"9. `amor_os` (string): **🔥 OS (內心獨白)**。\n"
        f"   - 25字內，冷面笑匠。**再次提醒：若有成交，嚴禁說他沒結果！**\n"
        f"   - {holiday_instruction}\n"
    )

    try:
        res = model.generate_content(prompt)
        data = safe_parse_json(res.text)
        
        reply_text = data.get('reply', '...')
        score = data.get('score', 1)
        is_fake = data.get('is_fake', False)
        is_fragile = data.get('is_fragile', False)
        distortion = data.get('distortion', '無')
        mode_name = data.get('mode_name', '阿摩亂入')
        mentor_choice = str(data.get('mentor_choice', ''))
        amor_os = clean_os_text(data.get('amor_os', '...'))
        mode_name_clean = clean_mode_name(mode_name)

        replacements = [("L1", "菜鳥階段"), ("L2", "戰術階段"), ("L3", "戰略階段")]
        if "稻盛" in mode_name_clean or "Inamori" in mentor_choice:
             replacements.extend([
                 ("負債", "心靈的包袱"), ("資產", "內在的修為"), ("虧損", "修行的考驗"),
                 ("賺錢", "積累福報"), ("變現", "轉化智慧"), ("窮酸", "格局"),
                 ("成交", "結善緣"), ("收單", "圓滿"), ("💰", "✨"), ("😈", "🙏")
             ])
        else:
            replacements.extend([
                ("窮酸味", random.choice(["廉價感", "底層思維", "免洗筷的氣息", "負債的味道"])),
                ("窮酸", random.choice(["缺乏資本", "毫無槓桿", "不值錢"])),
            ])

        for old, new in replacements:
            reply_text = reply_text.replace(old, new)
            amor_os = amor_os.replace(old, new)

        if is_fragile:
            final_reply = f"🚑 **啟動機制：【 🏥 ICU 重症監護室 】**\n{reply_text}\n(阿摩後台 OS：{amor_os})"
        elif is_fake:
            final_reply = f"🚫 **觸發機制：【 虛假反思偵測 】**\n{reply_text}\n(阿摩後台 OS：{amor_os})"
        else:
            icon = "👹"
            if "稻盛" in mode_name_clean: icon = "🙏"
            elif "華爾街" in mode_name_clean: icon = "💸"
            elif "阿里" in mode_name_clean: icon = "⚔️"
            elif "健身" in mode_name_clean: icon = "🏋️‍♂️"
            if score >= 5: icon = "💎"
            final_reply = f"{icon} **啟動模式：【 {mode_name_clean} 】**\n{reply_text}\n\n(阿摩後台 OS：{amor_os})"

        return {"text": final_reply + inc_msg, "score": score, "is_fake": is_fake, "is_fragile": is_fragile, "distortion": distortion}
    except Exception as e:
        return {"text": f"阿摩讀到了亂碼 ({e})", "score": 1, "is_fake": False, "is_fragile": False, "distortion": "Error"}