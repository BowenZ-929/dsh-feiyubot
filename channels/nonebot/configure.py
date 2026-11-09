"""停 QQ 后运行：只修改既有 6200 反向连接，保存可原样恢复的本地备份。"""
import argparse
from datetime import datetime
import json
from pathlib import Path
import secrets
import shutil

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("config", type=Path, help="NapCat onebot11_<小号>.json")
args = parser.parse_args()
data = json.loads(args.config.read_text())
clients = [item for item in data["network"]["websocketClients"]
           if item.get("enable") and item.get("url") == "ws://127.0.0.1:6200/ws"]
if len(clients) != 1:
    parser.error("需要恰好一条已启用的 6200/ws 连接；未修改配置")
state = Path(__file__).resolve().parents[1] / "onebot/.state"
state.mkdir(parents=True, exist_ok=True)
backup = state / ("napcat-before-nonebot-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".json")
shutil.copy2(args.config, backup)
backup.chmod(0o600)
token = secrets.token_urlsafe(32)
clients[0].update(url="ws://127.0.0.1:6290/onebot/v11/ws", token=token)
settings = state / "nonebot.json"
settings.write_text(json.dumps({"token": token, "napcat_config": str(args.config), "backup": str(backup)}, indent=2) + "\n")
settings.chmod(0o600)
args.config.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
print("已将既有小号连接改为 NoneBot；账号与其他连接未修改。")
print("原配置备份：", backup)
