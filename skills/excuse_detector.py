import re

def analyze(report_text):
    """
    分析學員日報中的推卸責任與外部歸因關鍵字，並計算藉口指數。
    """
    if not report_text or not isinstance(report_text, str):
        return {"excuse_level": 0, "detected_keywords": []}

    # 定義藉口關鍵字及其權重
    excuse_patterns = {
        "因為": 10,
        "由於": 10,
        "但是": 5,
        "可是": 5,
        "沒辦法": 15,
        "不關我": 30,
        "不是我的錯": 30,
        "都是因為": 20,
        "都怪": 25,
        "老師沒教": 20,
        "助教沒回": 15,
        "時間不夠": 15,
        "太難了": 10,
        "環境問題": 15,
        "電腦壞了": 20,
        "網路慢": 10,
        "沒人幫我": 20,
        "運氣不好": 15,
        "沒說明清楚": 15,
        "這不是我負責的": 25,
        "聽說": 5,
        "大概": 5,
        "可能": 5,
        "不知道為什麼": 10
    }

    detected_keywords = []
    total_score = 0

    # 進行關鍵字掃描
    for keyword, weight in excuse_patterns.items():
        if keyword in report_text:
            detected_keywords.append(keyword)
            total_score += weight

    # 處理特殊邏輯：如果字數過少但關鍵字密度高，加權
    text_length = len(report_text)
    if text_length > 0 and text_length < 50 and len(detected_keywords) >= 2:
        total_score += 20

    # 確保回傳值在 0-100 之間
    excuse_level = min(max(int(total_score), 0), 100)

    return {
        "excuse_level": excuse_level,
        "detected_keywords": detected_keywords
    }