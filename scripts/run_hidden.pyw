# запуск без вікна консолі (pythonw) для автозапуску в Windows; лог пишеться в data/bot.log
import os
import runpy
import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent
os.chdir(root)
sys.path.insert(0, str(root))
os.environ.setdefault("LOG_FILE", str(root / "data" / "bot.log"))

runpy.run_module("nosi_tempo", run_name="__main__", alter_sys=True)
