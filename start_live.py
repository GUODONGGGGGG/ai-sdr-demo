"""Start the local demo with a server-side DeepSeek API key."""

import getpass
import os
from pathlib import Path

from server.app import app


def main():
    if not os.getenv("DEEPSEEK_API_KEY"):
        key_file = os.getenv("DEEPSEEK_KEY_FILE")
        if key_file:
            path = Path(key_file).expanduser()
            if not path.is_file() or path.stat().st_mode & 0o077:
                raise SystemExit("本机 Key 文件不存在或权限过宽；请重新运行 save_key_local.py。")
            key = path.read_text(encoding="utf-8").strip()
        else:
            key = getpass.getpass("粘贴 DeepSeek API Key（输入不显示），然后按回车：").strip()
        if not key:
            raise SystemExit("未输入 Key，服务未启动。")
        os.environ["DEEPSEEK_API_KEY"] = key
    port = int(os.getenv("PORT", "5050"))
    print(f"DEMO 正在启动：http://127.0.0.1:{port}")
    app.run(host="127.0.0.1", port=port, debug=False)


if __name__ == "__main__":
    main()
