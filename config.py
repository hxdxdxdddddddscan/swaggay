import os

# Одноимённые переменные окружения (например на Render) приоритетнее значений ниже.
API_ID = os.environ.get("API_ID") or "31206776"
API_HASH = os.environ.get("API_HASH") or "7ca69453a014d6a87d67afe4d9df8284"
TG_SESSION = os.environ.get("TG_SESSION") or "1ApWapzMBuxszhLTc2AHi5uXtlHiVS7kfd5EQZqsTpgoYfDaJWdfHoHSNe-YczAg0-A5czreAV1cfqxle_SudgzJyrS_r6jxGVPzYKny9EqrEnlx8xnkLJF_qSHW11ncpK5sEFoe2ifwYRT8ab9DMxs3LiPUTo0VBqL0ItFMbXooBsaZONB0y0618VPTjRFvWNujl6ymxIMNizMTGNLIKQx2N9qkyx-IkfiB6m3qn56f3F8CiEIIBEYtPdsLTkcjR8bXFkrbXehY_y98IyzgWUmyzIp0r3TBZ9NClKInkgSribKW0L_c4RpPgR_JEfeSFhj8od6YrDczEmwnWqmnY_NqVUEh8H0o="

SUPABASE_URL = os.environ.get("SUPABASE_URL") or "https://ltmninhhmymypfzcpfcd.supabase.co"
SUPABASE_KEY = os.environ.get("SUPABASE_KEY") or "sb_secret_vrhbQwCu6MEY4-AZKCAJhw_n4-Vqtlg"  # secret key

ADMIN_USER = os.environ.get("ADMIN_USER") or "hamer"
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD") or "8161"

BOT_TOKEN = os.environ.get("BOT_TOKEN") or "8641923131:AAGxv7loUgbkloiSZdyPC6GxqKXLtPWtOoU"  # бот-логгер
ADMIN_ID = int(os.environ.get("ADMIN_ID") or "8521863420")  # кому бот шлёт отчёты
