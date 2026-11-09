"""Serves the Mini App on http://127.0.0.1:8770 against data/dev.db, no Telegram needed."""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.update(DEV_USER_ID="1", DB_PATH="data/dev.db", WEB_HOST="127.0.0.1")
os.environ.setdefault("PORT", "8770")
sys.argv.append("--web-only")

from nosi_tempo.__main__ import main  # noqa: E402

asyncio.run(main())
