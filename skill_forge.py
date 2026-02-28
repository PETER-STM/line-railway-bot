import os
import sys
import importlib.util
from dotenv import load_dotenv
from ai_service import SafeGeminiModel

load_dotenv()

# 🔥 劃定安全區：AI 寫的所有新程式碼，都只能放在這個資料夾裡
SKILLS_DIR = "skills"
if not os.path.exists(SKILLS_DIR):
    os.makedirs(SKILLS_DIR)

class SkillForge:
    """
    第三階段 AGI 引擎：神經元鐵匠爐
    任務：讓 AI 根據需求，自己寫出 Python 模組，並動態掛載到系統上。
    """
    def __init__(self):
        self.model = SafeGeminiModel()
        # 寫程式需要最嚴謹的邏輯，強制鎖定高智商 Pro 大腦
        self.model.current_model_name = "gemini-2.5-pro"

    def forge_and_mount_skill(self, skill_name, description):
        print(f"\n🔨 [神經元鐵匠爐] 系統正在自行撰寫 Python 擴充模組：【{skill_name}.py】")
        print(f"🎯 模組設計圖：\n{description}")

        # 1. 逼迫 AI 寫出純 Python 程式碼
        prompt = f"""
        你現在是一位頂尖的 Python 系統架構師。
        我們的系統需要一個全新的外掛模組來分析學員的日報。
        
        模組名稱：{skill_name}
        功能描述：{description}

        請寫出完整的 Python 程式碼。
        【嚴格要求】：
        1. 必須包含一個名為 `analyze(report_text)` 的主函數。
        2. 該函數必須回傳一個 Python 字典 (dict)，包含分析結果。
        3. 絕對禁止使用 os, sys, subprocess 等危險的系統操作庫。
        4. 純程式碼輸出！不要 Markdown 標記 (不要有 ```python)，不要任何解釋文字，只要純粹的程式碼。
        """

        res = self.model.generate_content(prompt)
        if not res or not res.text:
            print("❌ [失敗] 腦力耗盡，無法生成程式碼。")
            return False

        # 清理可能殘留的 markdown 標記
        code = res.text.replace('```python', '').replace('```', '').strip()

        # 2. 寫入實體檔案 (創造新的神經元)
        file_path = os.path.join(SKILLS_DIR, f"{skill_name}.py")
        try:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(code)
            print(f"💾 [燒錄完成] 程式碼已實體化：{file_path}")
        except Exception as e:
            print(f"❌ [燒錄失敗] 無法寫入檔案：{e}")
            return False

        # 3. 動態掛載與試運轉 (Hot-loading)
        return self._mount_and_test(skill_name, file_path)

    def _mount_and_test(self, skill_name, file_path):
        print(f"🧠 [神經元掛載] 正在嘗試動態載入 {skill_name} 模組...")
        try:
            # Python 黑魔法：動態載入不在主程式碼裡的外部檔案
            spec = importlib.util.spec_from_file_location(skill_name, file_path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            # 🔥 實彈壓力測試：換成保時捷團隊超常見的「隱性外部歸因」日報
            test_report = "今天沒收單，因為下雨的關係路上都沒人，加上遇到很多超級恨的小朋友，客質真的很差。後來體力不支狀態不好，沒辦法只好提早收了。"
            print("🔬 [試運轉] 注入測試日報，觀察神經元反應...")
            print(f"📄 測試內容：「{test_report}」")
            
            result = module.analyze(test_report)
            
            print(f"\n✅ [掛載成功] 新神經元運作完美！測試輸出結果：")
            print(result)
            return True

        except Exception as e:
            print(f"🚨 [排斥反應] AI 寫的程式碼有 Bug，系統掛載失敗！錯誤：{e}")
            print(f"🗑️ [自我清理] 正在刪除壞死的神經元 ({file_path})...")
            if os.path.exists(file_path):
                os.remove(file_path)
            return False

if __name__ == "__main__":
    forge = SkillForge()
    
    # 🔥 啟動 V2 演化：專屬台灣業務團隊的隱性藉口探測器
    forge.forge_and_mount_skill(
        skill_name="excuse_detector_v2",
        description="""
        分析業務日報中的『隱性外部歸因』與『藉口』。
        請使用正則表達式(Regex)或關鍵字陣列，重點捕捉以下四類台灣業務常見的卸責語意，並給予對應的扣分權重：
        1. 天氣與環境(30分)：下雨、人太少、沒人、磁場不好、大會盯上。
        2. 顧客素質(30分)：客質差、客人有問題、年紀太小、被晃點、奧客、超級恨。
        3. 身體與情緒(20分)：體力不支、太勞累、被影響心情、狀態不好、生病。
        4. 一般推託(10分)：因為、沒辦法、沒時間。
        請回傳 dict 包含 'excuse_level' (加總分數) 與 'detected_keywords'。
        """
    )