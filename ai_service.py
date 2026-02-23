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
    try:
        return json.loads(text)
    except Exception:
        pass
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
        """🔥 終極雷達：自動調閱可用模型清單"""
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
                    if self.fallback_chain:
                        self.current_model_name = self.fallback_chain.pop(0)
                        continue
                    else: break
            except Exception:
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

# ==========================================
# 💾 2. 記憶策展與動態感知引擎
# ==========================================
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
    except Exception as e:
        print(f"⚠️ DNA 蒸餾失敗: {e}", file=sys.stderr)

def get_recent_competence_trend(group_id, name, days=7):
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT sdt_c FROM reports 
                    WHERE group_id=%s AND normalized_name=%s AND sdt_c IS NOT NULL
                    ORDER BY report_date DESC LIMIT %s
                """, (group_id, name, days))
                records = cur.fetchall()
                return [float(r[0]) for r in records][::-1]
    except: return []

def evaluate_sdt_state(insight):
    try:
        prompt = f"""
        Analyze SDT (0.0-1.0). Report: "{insight}"
        RULE: Implicit Cue Detection - If user expresses negative emotion but reports successful action/breakthrough, Competence (C) MUST > 0.8.
        Return JSON ONLY using this exact schema: {{"A": float, "C": float, "R": float}}
        """
        res = model.generate_content(prompt, require_json=True)
        payload = extract_json_payload(res.text) if res else None
        if not payload: return "VICTIM", "A:0.5|C:0.5|R:0.5", (0.5, 0.5, 0.5)
        
        a, c, r = float(payload.get("A", 0.5)), float(payload.get("C", 0.5)), float(payload.get("R", 0.5))
        if a < 0.4 and c < 0.3 and not any(k in insight for k in ["成交", "收單", "單"]): state = "FRAGILE"
        elif a < 0.6 or c < 0.5: state = "VICTIM"
        elif r < 0.6: state = "ACHIEVER"
        else: state = "LEADER"
        return state, f"A:{a:.1f}|C:{c:.1f}|R:{r:.1f}", (a, c, r)
    except: return "VICTIM", "A:0.5|C:0.5|R:0.5", (0.5, 0.5, 0.5)

# ==========================================
# ⚖️ 3. 雙軌制獎勵函數 (Double-Track Reward)
# ==========================================
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
                    delta_c = current_c - (prev_c or 0.5)
                    delta_a = current_a - (prev_a or 0.5)
                    is_success = (delta_c > 0.1) or (delta_a > 0.1)
                    
                    if is_success:
                        cur.execute("UPDATE mab_stats SET successes = successes + 1 WHERE normalized_name=%s AND tactic_key=%s", (name, last_tactic))
                    else:
                        cur.execute("UPDATE mab_stats SET failures = failures + 1 WHERE normalized_name=%s AND tactic_key=%s", (name, last_tactic))
            conn.commit()
    except Exception as e:
        print(f"⚠️ 獎勵結算異常: {e}", file=sys.stderr)

# ==========================================
# 💬 4. 系統組裝 (閉環演化掛載點)
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
        recent_trend = get_recent_competence_trend(group_id, name, days=7)
        trend_context = f"🔥 該員近7日勝任感(C)軌跡: {recent_trend}" if recent_trend else ""

        calculate_and_record_reward(group_id, name, report_date, sa, sc)

        # ==========================================
        # 🔥 V36 動態戰術讀取器 (從資料庫載入最新演化的神經元)
        # ==========================================
        db_tactics = []
        try:
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT tactic_key, description, enhancement, risk_level FROM dynamic_tactics")
                    db_tactics = cur.fetchall()
        except Exception as e:
            print(f"⚠️ 讀取動態戰術失敗: {e}", file=sys.stderr)

        allowed = {}
        for row in db_tactics:
            t_key, t_desc, t_enh, t_risk = row
            if state == "FRAGILE" and t_risk == "high": continue
            if state == "LEADER" and t_key in ["留白配速", "休克療法", "行動微光"]: continue
            
            # 🔥 終極拼圖：把原始指令 (t_desc) 加上 AI 自行演化的增強語句 (t_enh)
            allowed[t_key] = f"{t_desc} {t_enh}".strip()
            
        if not allowed:
            allowed = {"斯多葛反問": "使用向下探究技術，剝開表層藉口，直擊其控制範圍內的責任。"}
            
        chosen_key = random.choice(list(allowed.keys()))
        mab_instruction = allowed[chosen_key]
        # ==========================================

        theme_map = {"LEADER": "💮 靈性導師", "ACHIEVER": "🗿 斯多葛教練", "VICTIM": "📿 棒喝禪師", "FRAGILE": "🩹 戰地醫護兵"}
        theme = theme_map.get(state, "📿 棒喝禪師")

        user_input = f"""
        Role: {theme} | User: {name}
        DNA: {old_dna}
        Trend: {trend_context}
        {CONTEXT_LIBRARY['CORE']}
        {CONTEXT_LIBRARY.get(state, "")}
        REQUIRED TACTIC: {mab_instruction}
        Report: {clean_rpt}
        
        # STRICT JSON ONLY (Ensure semantic completeness):
        {{
            "EMPATHY": "先同理處境，建立安全感 (30-60 words)",
            "POINT": "向下探究技術：直擊核心信念或給予深度肯定 (30-80 words)",
            "LOGIC": "認知重構：提供邏輯，引導思維轉變 (50-120 words)",
            "ACTION": "FBM 行為提示：根據狀態進行能力降維，給出明日具體行動 (50-120 words)",
            "OS": "毒舌或幽默收尾 (20-40 words)",
            "SCORE": 8
        }}
        """

        res = model.generate_content(user_input, require_json=True)
        if not res: raise Exception("AI_RETURNED_NONE")
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

        if chosen_key == "留白配速":
            final_reply = (
                f"{theme.split(' ')[0]} 啟動模式：【 {theme.split(' ')[1]} 】\n"
                f"📊 狀態：`{sdt_scores}`\n\n"
                f"🤝 共鳴 (The Empathy)\n{safe_text(res_json.get('EMPATHY'))}\n\n"
                f"⏳ 教練留白\n當你準備好面對時，我們再繼續深入。\n\n"
                f"(阿摩 OS：{safe_text(res_json.get('OS'))})"
            )
        else:
            final_reply = (
                f"{theme.split(' ')[0]} 啟動模式：【 {theme.split(' ')[1]} 】\n"
                f"📊 狀態：`{sdt_scores}`\n\n"
                f"🤝 共鳴 (The Empathy)\n{safe_text(res_json.get('EMPATHY'))}\n\n"
                f"🎯 盲點 (The Point)\n{safe_text(res_json.get('POINT'))}\n\n"
                f"🧠 邏輯 (The Logic)\n{safe_text(res_json.get('LOGIC'))}\n\n"
                f"⚡ 突破 (The Action)\n戰術：【 {chosen_key} 】\n{safe_text(res_json.get('ACTION'))}\n\n"
                f"(阿摩 OS：{safe_text(res_json.get('OS'))})"
            )
            
        return {"text": final_reply, "score": int(res_json.get("SCORE", 5)), "diagnosis": safe_text(res_json.get("POINT"))}

    except Exception as e:
        print(f"❌ 系統架構崩潰: {traceback.format_exc()}", file=sys.stderr)
        return {
            "text": "📿 **啟動模式：【 靜默觀照 】**\n\n阿摩正在深層連結你的數據。剛才的思考被雜訊干擾，但我已記下你的成長。明日繼續保持動作。",
            "score": 5, "diagnosis": "系統解析暫失效"
        }