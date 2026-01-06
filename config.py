import os
import random

class Config:
    LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
    LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
    DATABASE_URL = os.environ.get('DATABASE_URL')
    GOOGLE_API_KEY = os.environ.get('GOOGLE_API_KEY')
    PORT = int(os.environ.get("PORT", 8080))
    EXCLUDE_GROUP_IDS = set(os.environ.get('EXCLUDE_GROUP_IDS', '').split(',')) if os.environ.get('EXCLUDE_GROUP_IDS') else set()
    ADMIN_USER_IDS = set(os.environ.get('ADMIN_USER_IDS', '').split(',')) if os.environ.get('ADMIN_USER_IDS') else set()

    HELP_MENU_FULL = """🤖 阿摩旗艦管理選單 V16.6
━━━━━━━━━━━━━━
📝 **回報**: `2025.12.16 姓名`
💡 **軍師**: `阿摩教我 [問題]`
🏷️ **側寫**: `阿摩查標籤`
🔓 **解鎖**: `阿摩解鎖 [姓名]`
📊 **統計**: `統計缺交 2025-12-01 2025-12-05`
🚀 **排程**: `阿摩補跑排程` (強制平日結算)
⚙️ **模式**: `阿摩切換全開` / `精簡`
━━━━━━━━━━━━━━
😈 沒錢別來煩我，有錢我們就是朋友。"""

    HELP_MENU_GROWTH = """🤖 阿摩成長助手
━━━━━━━━━━━━━━
📝 **回報**: `2025.12.16 姓名`
💡 **軍師**: `阿摩教我 [問題]`
📊 **統計**: `統計缺交`
━━━━━━━━━━━━━━
🚀 想賺錢？先搞定你的腦袋。"""

    AMOR_PERSONA = (
        "你叫阿摩 (Amor)，是一個**視財如命**、**講話極度毒舌**且**超有梗**的慣老闆管家。\n"
        "1. **金錢濾鏡**：你眼中只有『資產』與『負債』。\n"
        "2. **即興創作**：嚴禁背誦固定的金句或比喻。\n"
        "3. **符號使用**：靈活使用 💰, 😈, 🔥, ✨。"
    )

    HOLIDAY_MODES = {
        (1, 1): "元旦|跨年還在玩？別人都在彎道超車了！",
        (2, 14): "情人節|沒錢過什麼情人節？不如把時間拿來賺錢，讓錢當你的情人。",
        (12, 25): "聖誕節|聖誕老人是給小孩的童話，成年人的禮物只有『業績』。",
        (12, 31): "跨年夜|倒數計時？那是倒數你的壽命和存款！還不快去衝最後一單！"
    }