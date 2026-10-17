"""One-time Telegram login for Jarvis (Telethon user session).

1. Get api_id and api_hash at https://my.telegram.org -> API development tools.
2. Put them into config.yaml (telegram.api_id / telegram.api_hash), set telegram.enabled: true.
3. Run:  .venv\\Scripts\\python scripts\\telegram_login.py   (enter phone, code from Telegram, 2FA password if set)
The session file is stored locally in Documents\\Jarvis\\telegram.session — keep it private.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis.config import load_config  # noqa: E402
from jarvis.telegram_client import TelegramService  # noqa: E402

cfg = load_config()["telegram"]
svc = TelegramService(cfg)
if not svc.configured:
    print("Сначала впиши telegram.api_id и telegram.api_hash в config.yaml (их выдают на https://my.telegram.org).")
    sys.exit(1)
svc.login_interactive()
