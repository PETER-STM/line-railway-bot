import sys
import re
import time
import json
import random
import threading
import traceback
import requests 
from datetime import datetime, timedelta
from config import Config
from database import get_db

# ==========================================
# 🧠 核心模型與安全設定 (裝載終極雷達防彈引擎)
# ==========================================
def extract_json_payload(text):
    """智慧雙層 JSON 解析器"""
    if not text: return None
    try: return json.loads(text)
    except Exception: pass
    try:
        clean_text = re.sub(r'```json\s*|```', '', text, flags=re.IGNORECASE).strip()
        start = clean_text.find('{')
        end = clean_text.rfind('}')
        if start != -1 and end != -1:
            json_str = clean_text[start:end+1]
            json_str = re.sub(r',\s*}', '}', json_str) 
            json_str = "".join(c for c in json_str if ord(c) >= 32 or c in "\n\r\t")
            return json.loads(json_str)
    except Exception as e:
        print(f"⚠️ 暴力 JSON 解析也失敗: {e}", file=sys.stderr)
    return None

class SafeGeminiModel:
    def __init__(self):
        self.api_key = Config.GOOGLE_API_KEY
        self.fallback_chain = self.auto_discover_models()
        if self.fallback_chain:
            self.current_model_name = self.fallback_chain.pop(0)
            print(f"🔫 [前線大腦] 鎖定最高智商合法大腦：{self.current_model_name}", file=sys.stderr)
        else:
            self.current_model_name = "gemini-1.5-flash"
            print("⚠️ [系統警告] 無法取得核准清單，使用預設大腦盲狙...", file=sys.stderr)

    def auto_discover_models(self):
        url = f"https://generativelanguage.googleapis.com/v1beta/models?key={self.api_key}"
        try:
            resp = requests.get(url, timeout=15)
            if resp.status_code == 200:
                models = [m['name'].replace('models/', '') for m in resp.json().get('models', []) if 'generateContent' in m.get('supportedGenerationMethods', [])]
                priority = [
                    "gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.0-flash-exp", "gemini-2.0-flash",
                    "gemini-1.5-pro-002", "gemini-1.5-pro-latest", "gemini-1.5-pro",
                    "gemini-1.5-flash-002", "gemini-1.5-flash-latest", "gemini-1.5-flash"
                ]
                ordered_chain = [p for p in priority if p in models]
                for m in models:
                    if m not in ordered_chain and "gemini" in m: ordered_chain.append(m)
                return ordered_chain
            else:
                print(f"⚠️ 取得模型失敗 ({resp.status_code}): {resp.text[:100]}", file=sys.stderr)
        except Exception: pass
        return []

    def generate_content(self, prompt, require_json=False):
        if not self.api_key: return None
        max_attempts = len(self.fallback_chain) + 2
        
        for _ in range(max_attempts):
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.current_model_name}:generateContent?key={self.api_key}"
            headers = {'Content-Type': 'application/json'}
            payload = {
                "contents": [{"parts": [{"text": prompt}]}],
                "safetySettings": [
                    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
                    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
                    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
                    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"}
                ]
            }
            if require_json and "pro" in self.current_model_name:
                payload["generationConfig"] = {"responseMimeType": "application/json"}
                
            try:
                resp = requests.post(url, headers=headers, json=payload, timeout=25)
                if resp.status_code == 200:
                    data = resp.json()
                    if "candidates" in data and len(data["candidates"]) > 0:
                        class DummyResponse:
                            def __init__(self, text): self.text = text
                        return DummyResponse(data['candidates'][0]['content']['parts'][0]['text'])
                else:
                    print(f"🚨 API 拒絕 ({resp.status_code}): {resp.text[:150]}", file=sys.stderr)
                    if self.fallback_chain:
                        self.current_model_name = self.fallback_chain.pop(0)
                        continue
                    else: break
            except Exception as e:
                print(f"🚨 連線例外: {e}", file=sys.stderr)
                if self.fallback_chain:
                    self.current_model_name = self.fallback_chain.pop(0)
                    continue
            time.sleep(1) 
        return None

model = SafeGeminiModel() if Config.GOOGLE_API_KEY else None

# ==========================================
# 🧩 1. 情境工程：語境設定
# ==========================================
CONTEXT_LIBRARY = {
    "CORE": "你是一面心態校準器。唯一目標是確保使用者的檢討正確，並推動『正向四循環：思維影響行為 ➡️ 行為養成習慣 ➡️ 習慣造就性格』。拒絕空泛雞湯，保持高管威嚴，嚴禁給出早產的解決方案。NO markdown symbols (**).",
    "FRAGILE": "[MODE: 戰地醫護兵] 目標極度脆弱。啟動 FBM 能力降維，任務必須極小化。給予最高程度的情感共鳴與安全感。",
    "VICTIM": "[MODE: 棒喝禪師] 目標陷入受害者情結。使用向下探究技術，直擊其認知扭曲。",
    "ACHIEVER": "[MODE: 斯多葛教練] 目標有產值但陷於執念。引導區分可控與不可控，教導抽離。",
    "LEADER": "[MODE: 靈性導師] 目標突破極限。給予高度認可，並賦予其引導團隊的社會責任。"
}

def distill_mindset_dna(group_id, normalized_name, new_report, old_dna):
    try:
        prompt = f"將DNA與新日報蒸餾成120字DNA。舊DNA:\"{old_dna}\" 新日報:\"{new_report}\" 包含1.核心恐懼 2.行為慣性 3.突破口。(純文字)"
        res = model.generate_content(prompt, require_json=False)
        if res and res.text:
            new_dna = res.text.replace('`', '').strip()[:120]
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute("UPDATE group_vips SET meta_patterns=%s WHERE group_id=%s AND normalized_name=%s", (new_dna, group_id, normalized_name))
                conn.commit()
    except Exception: pass

def get_recent_competence_trend(group_id, name, days=7):
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT sdt_c FROM reports WHERE group_id=%s AND normalized_name=%s AND sdt_c IS NOT NULL ORDER BY report_date DESC LIMIT %s", (group_id, name, days))
                return [float(r[0]) for r in cur.fetchall()][::-1]
    except: return []

# ==========================================
# 🔥 V42 終極物理鎖定 (Neuro-symbolic 3.0)
# 徹底根除 AI 正向偏見，導入三維度強制降分邏輯
# ==========================================
def evaluate_sdt_state(insight):
    try:
        prompt = f"""
        你是一位極度冷血、只看客觀事實的系統稽核員。請根據以下日報，評估三個維度(0.0~1.0)，並回答三個客觀事實(true/false)。
        
        【第一階段：事實查核 (Fact Check)】
        1. `has_physical_action`: 是否有實質的「物理業務動作」？(如：收單、跳摳、面試、客訴處理、實體開發)。純思考、感恩、開會聽講，皆為 false。
        2. `has_deep_reflection`: 是否有拆解出問題的「底層邏輯」並提出「改變策略」？只是說"要更努力、成績不理想"皆為 false。
        3. `has_altruism`: 是否有「實質幫助他人」的物理動作？(如：幫同事收單、教導新人)。只有口頭寫"感謝某某某"，皆為 false。
        
        【第二階段：SDT 初始打分】
        0.5 為普通凡人基準線。
        A (自主性): 0.5=被動執行；0.8+=展現強烈主動破局思維。
        C (勝任感): 0.5=運氣好或無產值；0.8+=絕對掌控局勢與高價值產出。
        R (關聯性): 0.5=表面客套；0.8+=實質團隊貢獻與領導。
        
        日報內容: "{insight}"
        
        Return JSON ONLY using this exact schema: 
        {{
            "has_physical_action": boolean,
            "has_deep_reflection": boolean,
            "has_altruism": boolean,
            "A": float,
            "C": float,
            "R": float
        }}
        """
        res = model.generate_content(prompt, require_json=True)
        if not res: return "API_ERROR", "A:0|C:0|R:0", (0.0, 0.0, 0.0)
        
        payload = extract_json_payload(res.text)
        if not payload: return "API_ERROR", "A:0|C:0|R:0", (0.0, 0.0, 0.0)
        
        a = float(payload.get("A", 0.5))
        c = float(payload.get("C", 0.5))
        r = float(payload.get("R", 0.5))
        
        has_action = payload.get("has_physical_action", True) 
        has_reflection = payload.get("has_deep_reflection", True) 
        has_altruism = payload.get("has_altruism", True) 
        
        # 🔥 V42 Python 剛性執法：無情上鎖
        lock_msgs = []
        if not has_action:
            a, c = min(a, 0.5), min(c, 0.5)
            lock_msgs.append("無動作(A/C鎖0.5)")
        if not has_reflection:
            c = min(c, 0.6)
            lock_msgs.append("無深度覆盤(C鎖0.6)")
        if not has_altruism:
            r = min(r, 0.5) 
            lock_msgs.append("無實質利他(R鎖0.5)")
            
        if lock_msgs:
            print(f"🔒 [V42 物理鎖觸發] {', '.join(lock_msgs)}", file=sys.stderr)
            
        if a < 0.4 and c < 0.4 and not has_action: state = "FRAGILE"
        elif a < 0.6 or c < 0.6: state = "VICTIM"
        elif a >= 0.8 and c >= 0.8 and r >= 0.7: state = "LEADER"
        else: state = "ACHIEVER"
            
        return state, f"A:{a:.1f}|C:{c:.1f}|R:{r:.1f}", (a, c, r)
    except Exception as e: 
        print(f"⚠️ 評分系統異常: {e}", file=sys.stderr)
        return "API_ERROR", "A:0|C:0|R:0", (0.0, 0.0, 0.0)

def calculate_and_record_reward(group_id, name, report_date_str, current_a, current_c):
    try:
        report_date = datetime.strptime(report_date_str, '%Y-%m-%d').date()
        yesterday = report_date - timedelta(days=1)
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT sdt_a, sdt_c FROM reports WHERE group_id=%s AND normalized_name=%s AND report_date=%s", (group_id, name, yesterday))
                prev_record = cur.fetchone()
                cur.execute("SELECT last_tactic FROM group_vips WHERE group_id=%s AND normalized_name=%s", (group_id, name))
                vip_record = cur.fetchone()
                last_tactic = vip_record[0] if vip_record and vip_record[0] else None

                if prev_record and last_tactic:
                    prev_a, prev_c = prev_record
                    is_success = (current_c - (prev_c or 0.5) > 0.1) or (current_a - (prev_a or 0.5) > 0.1)
                    if is_success: cur.execute("UPDATE mab_stats SET successes = successes + 1 WHERE normalized_name=%s AND tactic_key=%s", (name, last_tactic))
                    else: cur.execute("UPDATE mab_stats SET failures = failures + 1 WHERE normalized_name=%s AND tactic_key=%s", (name, last_tactic))
            conn.commit()
    except Exception: pass

# ==========================================
# 💬 4. 系統組裝 (V43 平衡版 Smart Brevity)
# ==========================================
def generate_ai_reply(trigger, **kwargs):
    if trigger != "report_success": return {"text": "已記錄。", "score": 0}

    try:
        full_report = kwargs.get('full_report', '')
        clean_rpt = re.sub(r'(?:\n|^)\s*[6六][\.\s、].*?精進[\s\S]*', '', full_report)
        name = kwargs.get('normalized_name', '夥伴')
        group_id = kwargs.get('group_id')
        old_dna = kwargs.get('personality', '尚無記憶') 
        
        match_date = re.search(r'^(\d{4}[./-]\d{1,2}[./-]\d{1,2})', full_report)
        report_date = match_date.group(1).replace('/', '-').replace('.', '-') if match_date else datetime.now().strftime('%Y-%m-%d')

        if group_id and name:
            threading.Thread(target=distill_mindset_dna, args=(group_id, name, clean_rpt, old_dna)).start()

        state, sdt_scores, (sa, sc, sr) = evaluate_sdt_state(clean_rpt)
        
        if state == "API_ERROR":
            return {"text": "📿 **啟提模式：【 靜默觀照 】**\n\n阿摩正在深層連結你的數據。剛才的思考被雜訊干擾，但我已記下你的成長。", "score": 5, "diagnosis": "API 額度耗盡或連線失敗"}

        recent_trend = get_recent_competence_trend(group_id, name, days=7)
        trend_context = f"🔥 該員近7日勝任感(C)軌跡: {recent_trend}" if recent_trend else ""
        calculate_and_record_reward(group_id, name, report_date, sa, sc)

        db_tactics = []
        try:
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT tactic_key, description, enhancement, risk_level FROM dynamic_tactics")
                    db_tactics = cur.fetchall()
        except: pass

        allowed = {}
        for row in db_tactics:
            t_key, t_desc, t_enh, t_risk = row
            if state == "FRAGILE" and t_risk == "high": continue
            if state == "LEADER" and t_key in ["留白配速", "休克療法", "行動微光"]: continue
            allowed[t_key] = f"{t_desc} {t_enh}".strip()
            
        if not allowed: allowed = {"斯多葛反問": "直擊藉口，探究責任。"}
        chosen_key = random.choice(list(allowed.keys()))
        mab_instruction = allowed[chosen_key]

        theme_map = {"LEADER": "💮 靈性導師", "ACHIEVER": "🗿 斯多葛教練", "VICTIM": "📿 棒喝禪師", "FRAGILE": "🩹 戰地醫護兵"}
        theme = theme_map.get(state, "📿 棒喝禪師")

        # 🔥 V43 平衡版：Smart Brevity 框架，極限控制字數
        user_input = f"""
        Role: {theme} | User: {name}
        DNA: {old_dna}
        Trend: {trend_context}
        {CONTEXT_LIBRARY['CORE']}
        {CONTEXT_LIBRARY.get(state, "")}
        REQUIRED TACTIC: {mab_instruction}
        Report: {clean_rpt}
        
        【極度重要：貫徹 Smart Brevity (精簡溝通) 鐵律】
        讀者處於高壓疲勞狀態，你必須消除修辭贅肉，拒絕長篇大論的心靈雞湯，使用白話文與直白語氣。
        請根據以下結構產出，各區塊(含標點)嚴格控制字數，不分場合皆適用此標準。
        
        # STRICT JSON ONLY:
        {{
            "HOOK": "引子：一句話直接肯定核心成就或點出處境。拒絕過度情緒鋪陳。(限 20 字內)",
            "LEDE": "導言：一句話簡單粗暴直擊盲點，或宣告客觀真理。(限 30 字內)",
            "CONTEXT": "脈絡：為什麼這很重要？強制使用 1~2 個條列式重點說明因果關係。(限 50 字內)",
            "ACTION": "行動：給出明日具體、可量化、有時限的物理 KPI。嚴禁「感受能量」等模糊任務。(限 30 字內)",
            "OS": "毒舌、資本家嘲諷或幽默收尾，一針見血。(限 20-40 字)",
            "SCORE": 8
        }}
        """

        res = model.generate_content(user_input, require_json=True)
        if not res: raise Exception("API_RETURNED_NONE")
        res_json = extract_json_payload(res.text)
        if not res_json: raise Exception("JSON_PARSE_FAILED")

        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE reports SET sdt_a=%s, sdt_c=%s, sdt_r=%s WHERE group_id=%s AND normalized_name=%s AND report_date=%s::date", (sa, sc, sr, group_id, name, report_date))
                cur.execute("UPDATE group_vips SET last_tactic=%s WHERE group_id=%s AND normalized_name=%s", (chosen_key, group_id, name))
                try: cur.execute("INSERT INTO mab_stats (normalized_name, tactic_key, uses) VALUES (%s, %s, 1) ON CONFLICT (normalized_name, tactic_key) DO UPDATE SET uses = mab_stats.uses + 1", (name, chosen_key))
                except: pass
            conn.commit()

        def safe_text(t): return re.sub(r'\*\*', '', str(t)).strip()

        # 🔥 真正的 V43 極簡四標題輸出格式 (完全拋棄共鳴/盲點等舊詞彙)
        if chosen_key == "留白配速":
            final_reply = f"{theme.split(' ')[0]} 啟動模式：【 {theme.split(' ')[1]} 】\n📊 狀態：`{sdt_scores}`\n\n📌 引子\n{safe_text(res_json.get('HOOK'))}\n\n⏳ 教練留白\n當你準備好面對時，我們再繼續深入。\n\n(阿摩 OS：{safe_text(res_json.get('OS'))})"
        else:
            final_reply = f"{theme.split(' ')[0]} 啟動模式：【 {theme.split(' ')[1]} 】\n📊 狀態：`{sdt_scores}`\n\n📌 引子\n{safe_text(res_json.get('HOOK'))}\n\n🎯 導言\n{safe_text(res_json.get('LEDE'))}\n\n🧠 脈絡\n{safe_text(res_json.get('CONTEXT'))}\n\n⚡ 行動 (戰術：{chosen_key})\n{safe_text(res_json.get('ACTION'))}\n\n(阿摩 OS：{safe_text(res_json.get('OS'))})"
            
        return {"text": final_reply, "score": int(res_json.get("SCORE", 5)), "diagnosis": safe_text(res_json.get("LEDE"))}

    except Exception as e:
        print(f"❌ 系統架構崩潰: {traceback.format_exc()}", file=sys.stderr)
        return {"text": "📿 **啟動模式：【 靜默觀照 】**\n\n阿摩正在深層連結你的數據。剛才的思考被雜訊干擾，但我已記下你的成長。", "score": 5, "diagnosis": "系統解析暫失效"}