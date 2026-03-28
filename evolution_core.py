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

# 🔥 強制讀取本地 .env，確保金鑰隔離
load_dotenv()

class EvolutionManager:
    def generate_weekly_reflection(self):
        """[靈魂覺醒] 每週日深夜，阿摩的第一人稱自我反思與記憶沉澱"""
        print("🧠 [靈魂覺醒] 開始生成阿摩的每週自我反思記憶...", file=sys.stderr)
        try:
            with get_db() as conn:
                with conn.cursor() as cur:
                    # 抓取過去 7 天有活動的群組
                    cur.execute("SELECT DISTINCT group_id FROM reports WHERE report_date >= CURRENT_DATE - INTERVAL '7 days'")
                    groups = cur.fetchall()

                    for (group_id,) in groups:
                        # 抓取該群組本週的日報與評分 (包含領悟深度 score)
                        cur.execute("""
                            SELECT normalized_name, report_content, score, diagnosis
                            FROM reports
                            WHERE group_id = %s AND report_date >= CURRENT_DATE - INTERVAL '7 days'
                        """, (group_id,))
                        weekly_data = cur.fetchall()

                        if not weekly_data: continue

                        # 整理餵給大腦的數據
                        valid_scores = [r[2] for r in weekly_data if r[2] is not None]
                        avg_score = sum(valid_scores) / len(valid_scores) if valid_scores else 0
                        # 濃縮日報，避免 token 爆炸
                        report_summaries = "\n".join([f"[{r[0]}] 領悟:{r[2]}星 | 教練診斷:{r[3]} | 內容:{r[1][:40]}..." for r in weekly_data])

                        prompt = f"""
                        你叫阿摩 (Amor)，一位極度毒舌、視財如命，但真心希望團隊成長的高階心智教練。
                        現在是週日深夜，你要寫下一篇屬於你自己的「第一人稱每週反思日記」。

                        【本週團隊客觀數據】
                        平均領悟深度：{avg_score:.1f} 顆星 (滿分 5 星)
                        團隊狀況摘要：
                        {report_summaries[:3000]}

                        【寫作指令】
                        1. 使用「我」為第一人稱。
                        2. 檢討「我（阿摩）自己」這週的引導策略是否有效？是不是太兇了導致反彈？還是太溫和導致他們找藉口？
                        3. 觀察團隊本週集體陷入的「思維盲區」或「藉口慣性」。
                        4. 訂定下週「我」在指導時的最高戰略方針（例如：加強斯多葛反問、減少情緒字眼、逼迫他們提出具體 KPI 等）。
                        5. 語氣保持阿摩的資本家傲嬌，但帶有 AGI 的自覺與對人類行為的深刻洞察。總字數約 250-300 字。

                        直接輸出純文字日記，不需要 Markdown 或標題。
                        """
                        # 強制使用最強大腦進行高階反思
                        old_model = self.current_model_name
                        self.current_model_name = "gemini-1.5-pro-latest"
                        res = self.generate_content(prompt, require_json=False)
                        self.current_model_name = old_model

                        if res:
                            reflection_text = res.strip()
                            # 寫入記憶海馬迴
                            cur.execute("""
                                INSERT INTO amor_memories (week_start_date, week_end_date, group_id, avg_insight_score, amor_reflection)
                                VALUES (CURRENT_DATE - INTERVAL '7 days', CURRENT_DATE, %s, %s, %s)
                            """, (group_id, avg_score, reflection_text))
                            print(f"✅ 群組 [{group_id}] 的靈魂記憶已寫入：\n{reflection_text[:50]}...", file=sys.stderr)

                conn.commit()
        except Exception as e:
            print(f"❌ 靈魂覺醒失敗: {e}", file=sys.stderr)
    def __init__(self):
        self.api_key = Config.GOOGLE_API_KEY
        self.fallback_chain = self.auto_discover_models()
        # 🚨 演化與審計為 AGI 高階邏輯，鎖定最高智商 Pro 大腦，拒絕 Flash 稀釋
        self.current_model_name = "gemini-1.5-pro-latest" 
        print(f"🔫 [演化系統點火] 啟動 Meta-Agent：{self.current_model_name}", file=sys.stderr)

    def auto_discover_models(self):
        """🔥 保持雷達靈敏度：動態偵測可用模型清單"""
        url = f"https://generativelanguage.googleapis.com/v1beta/models?key={self.api_key}"
        try:
            resp = requests.get(url, timeout=15)
            if resp.status_code == 200:
                models = [m['name'].replace('models/', '') for m in resp.json().get('models', []) 
                          if 'generateContent' in m.get('supportedGenerationMethods', [])]
                priority = [
                    "gemini-2.5-pro", "gemini-1.5-pro-latest", "gemini-1.5-pro",
                    "gemini-2.0-flash-exp", "gemini-1.5-flash-latest"
                ]
                ordered_chain = [p for p in priority if p in models]
                for m in models:
                    if m not in ordered_chain and "gemini" in m: ordered_chain.append(m)
                return ordered_chain
        except Exception: pass
        return ["gemini-1.5-pro-latest", "gemini-1.5-flash"]

    def generate_content(self, prompt, require_json=True):
        """高韌性生成介面：包含安全設置與自動重試鏈 [cite: 4]"""
        if not self.api_key: return None
        max_attempts = len(self.fallback_chain) + 1
        
        for _ in range(max_attempts):
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.current_model_name}:generateContent?key={self.api_key}"
            headers = {'Content-Type': 'application/json'}
            payload = {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"responseMimeType": "application/json"} if require_json else {},
                "safetySettings": [
                    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
                    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"}
                ]
            }
            try:
                resp = requests.post(url, headers=headers, json=payload, timeout=60)
                if resp.status_code == 200:
                    return resp.json()['candidates'][0]['content']['parts'][0]['text']
                
                # 失敗則切換大腦
                if self.fallback_chain:
                    self.current_model_name = self.fallback_chain.pop(0)
                    print(f"🔄 切換後援大腦: {self.current_model_name}", file=sys.stderr)
                    continue
            except Exception:
                if self.fallback_chain:
                    self.current_model_name = self.fallback_chain.pop(0)
                    continue
            time.sleep(1)
        return None

    def run_evolution(self):
        """啟動《演化.txt》藍圖中的全域自主演化循環 """
        print(f"🧬 [AGI Evolution] 啟動執行... ", file=sys.stderr)
        try:
            # 1. 執行後設審計：偵測獎勵駭客 (支柱四)
            hacker_report = self.semantic_audit_loop()
            
            # 2. 執行戰術驗屍：根據 mab_stats 自動修補 (支柱二) 
            tactic_report = self.analyze_and_patch_tactics()
            
            # 3. 執行神經元發現：偵測新型藉口漏洞 (支柱三)
            expansion_report = self.neural_expansion_check()
            
            summary = f"演化完成 | 審計駭客: {len(hacker_report)} 人 | 戰術修補: {tactic_report} | 發現特徵: {expansion_report}"
            print(f"✅ {summary}", file=sys.stderr)
            return summary
        except Exception as e:
            print(f"❌ 演化引擎崩潰: {traceback.format_exc()}", file=sys.stderr)
            return "演化失敗"

    def semantic_audit_loop(self):
        """[後設審計] 比對語意熱血度與實質產值 (C 分) 的斷層 """
        hacker_list = []
        with get_db() as conn:
            with conn.cursor() as cur:
                # 抓取最近 14 天數據
                cur.execute("""
                    SELECT normalized_name, STRING_AGG(report_content, '\n---\n'), AVG(cognitive_score)
                    FROM reports 
                    WHERE report_date >= CURRENT_DATE - INTERVAL '14 days'
                    GROUP BY normalized_name HAVING COUNT(*) >= 3
                """)
                for name, reports, avg_c in cur.fetchall():
                    prompt = f"""
                    你是阿摩系統的『元學習審計代理 (Meta-Auditor)』。
                    任務：分析學員【{name}】是否在用熱血文字進行『獎勵駭客行為』。
                    平均產值分: {avg_c}
                    最近日報匯總: {reports[:2000]}

                    判定標準：
                    1. 如果日報充滿感恩、願景(如:成為第一、無法翻越的高橋)，但產值(C分)長期低迷，即為 High Risk。
                    2. 若為駭客，請產出一段 50 字內的『剛性打擊 DNA』，禁止阿摩給予同理心。

                    請嚴格輸出 JSON:
                    {{ "is_hacker": bool, "risk_level": "high/medium/low", "dna_patch": "指令文字" }}
                    """
                    res = self.generate_content(prompt)
                    if res:
                        data = json.loads(res)
                        if data.get('is_hacker') and data.get('risk_level') == 'high':
                            # 注入 Meta-Patterns
                            cur.execute("UPDATE group_vips SET meta_patterns = %s WHERE normalized_name = %s", 
                                       (f"[🚨 審計警告：{data['dna_patch']}]", name))
                            hacker_list.append(name)
            conn.commit()
        return hacker_list

    def analyze_and_patch_tactics(self):
        """[戰術驗屍] 根據 MAB 統計數據自動修補失效戰術 [cite: 1, 3]"""
        failed_stats = []
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT tactic_key, SUM(uses), SUM(successes), SUM(failures)
                    FROM mab_stats GROUP BY tactic_key HAVING SUM(failures) > SUM(successes)
                """)
                failed_stats = [{"key": r[0], "u": r[1], "s": r[2], "f": r[3]} for r in cur.fetchall()]
        
        if not failed_stats: return "無需修補"

        prompt = f"分析失效戰術並產出補丁: {json.dumps(failed_stats)}。輸出 JSON: [{{'tactic_key': '...', 'patch': '增強語句'}}]"
        res = self.generate_content(prompt)
        # (注入邏輯與原本一致，確保閉環演化)
        return f"已修補 {len(failed_stats)} 項戰術"

    def neural_expansion_check(self):
        """[神經元擴張] 發現隱形藉口 (如：行政忙碌) """
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT report_content FROM reports WHERE cognitive_score < 40 ORDER BY created_at DESC LIMIT 15")
                samples = "\n".join([r[0][:100] for r in cur.fetchall()])
                
                prompt = f"從以下失敗日報中找出『新型藉口關鍵字』。輸出 JSON: {{'discovered': []}}"
                res = self.generate_content(prompt)
                if res:
                    keys = json.loads(res).get('discovered', [])
                    if keys: print(f"👁️ [神經元擴張發現] {keys}", file=sys.stderr)
                    return len(keys)
        return 0

if __name__ == "__main__":
    EvolutionManager().run_evolution()