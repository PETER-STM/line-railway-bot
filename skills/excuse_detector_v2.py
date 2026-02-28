def analyze(report_text):
    """
    分析業務日報中的隱性外部歸因與藉口，並根據權重計算扣分。
    
    Args:
        report_text (str): 業務日報文字內容
        
    Returns:
        dict: 包含 excuse_level (總分) 與 detected_keywords (偵測到的關鍵字清單)
    """
    
    # 定義四大類別關鍵字及其對應權重
    excuse_categories = [
        {
            "name": "環境與天氣",
            "weight": 30,
            "keywords": ["下雨", "人太少", "沒人", "磁場不好", "大會盯上"]
        },
        {
            "name": "顧客素質",
            "weight": 30,
            "keywords": ["客質差", "客人有問題", "年紀太小", "被晃點", "奧客", "超級恨"]
        },
        {
            "name": "身體與情緒",
            "weight": 20,
            "keywords": ["體力不支", "太勞累", "被影響心情", "狀態不好", "生病"]
        },
        {
            "name": "一般推託",
            "weight": 10,
            "keywords": ["因為", "沒辦法", "沒時間"]
        }
    ]

    detected_keywords = []
    total_excuse_score = 0

    if not report_text or not isinstance(report_text, str):
        return {
            "excuse_level": 0,
            "detected_keywords": []
        }

    # 進行關鍵字掃描與分數加總
    for category in excuse_categories:
        category_weight = category["weight"]
        for kw in category["keywords"]:
            if kw in report_text:
                detected_keywords.append(kw)
                total_excuse_score += category_weight

    return {
        "excuse_level": total_excuse_score,
        "detected_keywords": detected_keywords
    }