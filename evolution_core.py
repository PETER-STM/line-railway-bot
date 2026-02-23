import os
import sys
import json
import time
import requests
import traceback
from datetime import datetime, timedelta
from dotenv import load_dotenv
from config import Config
from database import get_db

# 🔥 強制讀取本地 .env
load_dotenv()

class EvolutionManager:
    def __init__(self):
        self.api_key = Config.GOOGLE_API_KEY
        self.fallback_chain = self.auto_discover_models()
        if self.fallback_chain:
            self.current_model_name = self.fallback_chain.pop(0)
            print(f"🔫 [系統開機] 鎖定最高智商合法大腦：{self.current_model_name}", file=sys.stderr)
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
                    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"}
                ]
            }
            if require_json and "pro" in self.current_model_name:
                payload["generationConfig"] = {"responseMimeType": "application/json"}
                
            try:
                resp = requests.post(url, headers=headers, json=payload, timeout=40)
                if resp.status_code == 200:
                    data = resp.json()
                    if "candidates" in data:
                        return data['candidates'][0]['content']['parts'][0]['text']
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

    def run_evolution(self):
        print(f"🧬 [Meta-RL 閉環演化引擎] 啟動 | 當前大腦: {self.current_model_name}", file=sys.stderr)
        try:
            hacker_count = self.detect_and_punish_reward_hacking()
            tactic_report = self.analyze_and_patch_tactics()
            summary = f"演化完成 | 懲處駭客: {hacker_count} 人 | 戰術修補狀態: {'成功' if tactic_report else '無須修補或失敗'}"
            print(f"✅ {summary}", file=sys.stderr)
            return summary
        except Exception as e:
            print(f"❌ 演化引擎崩潰: {traceback.format_exc()}", file=sys.stderr)
            return "演化失敗"

    def detect_and_punish_reward_hacking(self):
        hacker_count = 0
        roi_keywords = ["成交", "收單", "單", "跳摳", "業績", "名單", "萬", "千", "客", "攤位", "產值"]
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT normalized_name, group_id, AVG(sdt_c), STRING_AGG(report_content, ' ')
                    FROM reports 
                    WHERE report_date >= CURRENT_DATE - INTERVAL '14 days'
                    GROUP BY normalized_name, group_id HAVING COUNT(*) >= 5
                """)
                for name, group_id, avg_c, all_text in cur.fetchall():
                    if avg_c is None: continue
                    has_roi = any(k in (all_text or "") for k in roi_keywords)
                    if avg_c > 0.75 and not has_roi:
                        cur.execute("SELECT meta_patterns FROM group_vips WHERE group_id=%s AND normalized_name=%s", (group_id, name))
                        old_dna = (cur.fetchone() or [""])[0]
                        penalty = "[🚨 系統警報：該員被判定為『獎勵駭客』(假正向/無產值)。AI 必須強制收起同理心，嚴厲逼問其具體 ROI 與商業產出！]"
                        if penalty not in old_dna:
                            cur.execute("UPDATE group_vips SET meta_patterns=%s WHERE group_id=%s AND normalized_name=%s", (f"{penalty} {old_dna}"[:500], group_id, name))
                            hacker_count += 1
            conn.commit()
        return hacker_count

    def analyze_and_patch_tactics(self):
        failed_stats = []
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT tactic_key, SUM(uses), SUM(successes), SUM(failures)
                    FROM mab_stats 
                    GROUP BY tactic_key HAVING SUM(uses) >= 3 AND SUM(failures) >= SUM(successes)
                """)
                for tactic, uses, succ, fail in cur.fetchall():
                    failed_stats.append({"tactic_key": tactic, "uses": uses, "successes": succ, "failures": fail})
                    
        if not failed_stats: return False

        print(f"📊 [戰術驗屍] 發現高失敗率戰術: {failed_stats}", file=sys.stderr)
        
        # 🔥 AGI 核心：強制 AI 輸出 JSON 格式的系統更新檔
        prompt = f"""
        你是一位企業 AI 系統架構師。教練系統的戰術在實戰中失效：{json.dumps(failed_stats, ensure_ascii=False)}。
        為了修正此問題，你必須為這些失效的戰術撰寫一段「增強語句 (Enhancement)」。
        這段語句將會直接疊加在原來的 Prompt 後方，用來刺破學員的防禦藉口。

        請【嚴格】輸出為 JSON 陣列格式，不要包含任何 markdown 或說明文字，格式如下：
        [
            {{
                "tactic_key": "失效的戰術名稱",
                "new_enhancement": "針對此戰術的新增強 Prompt 語句 (約 50 字內，語氣要犀利、直擊痛點)"
            }}
        ]
        """
        
        analysis_json_str = self.generate_content(prompt)
        if not analysis_json_str: return False

        try:
            # 解析 AI 給出的更新檔
            start = analysis_json_str.find('[')
            end = analysis_json_str.rfind(']')
            if start != -1 and end != -1:
                clean_json = analysis_json_str[start:end+1]
                patches = json.loads(clean_json)
                
                # 🔥 閉環注入：將更新檔直接寫入資料庫
                with get_db() as conn:
                    with conn.cursor() as cur:
                        for patch in patches:
                            t_key = patch.get("tactic_key")
                            enhancement = patch.get("new_enhancement")
                            if t_key and enhancement:
                                cur.execute("UPDATE dynamic_tactics SET enhancement = %s WHERE tactic_key = %s", (f"[系統自我修正補丁：{enhancement}]", t_key))
                                print(f"💉 [閉環注入成功] 戰術【{t_key}】已完成神經元擴充: {enhancement}", file=sys.stderr)
                    conn.commit()
                return True
        except Exception as e:
            print(f"⚠️ 解析或注入更新檔失敗: {e}\n原始字串: {analysis_json_str}", file=sys.stderr)
            return False

if __name__ == "__main__":
    EvolutionManager().run_evolution()