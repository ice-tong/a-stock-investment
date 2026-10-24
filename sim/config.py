"""路径与配置加载。

数据目录默认是项目根目录；设置环境变量 SIM_HOME 可指向别处（测试时用）。
配置始终从项目根目录的 config/ 读取。
"""
import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"


def home() -> Path:
    return Path(os.environ.get("SIM_HOME", ROOT))


def accounts_dir() -> Path:
    return home() / "accounts"


def orders_dir() -> Path:
    return home() / "orders"


def screen_dir() -> Path:
    return home() / "data" / "screen"


def cache_dir() -> Path:
    return ROOT / "data" / "cache"


def reports_dir() -> Path:
    return home() / "reports" / "sim"


def load(name: str) -> dict:
    with open(CONFIG_DIR / f"{name}.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)

