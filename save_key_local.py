"""Save a DeepSeek key outside the project for local development only."""

import getpass
import os
from pathlib import Path


KEY_PATH = Path.home() / ".config" / "ai-sdr-demo" / "deepseek.key"


def main():
    key = getpass.getpass("粘贴 DeepSeek API Key（输入不显示），然后按回车：").strip()
    if not key:
        raise SystemExit("未输入 Key；没有保存任何内容。")
    directory = KEY_PATH.parent
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    descriptor = os.open(KEY_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(key)
    finally:
        KEY_PATH.chmod(0o600)
    print(f"已保存在本机：{KEY_PATH}（仅当前用户可读）。")
    print("现在可以关闭此终端；如需撤销本机保存，删除这个文件即可。")


if __name__ == "__main__":
    main()
