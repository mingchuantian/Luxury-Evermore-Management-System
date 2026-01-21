import os
import logging
import sys

# 配置日志，确保所有日志输出到标准输出/标准错误（会被 nohup 捕获到 app.log）
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    stream=sys.stdout
)

from luxury_app import create_app

app = create_app()


if __name__ == "__main__":
    debug = os.getenv("FLASK_DEBUG", "").strip() in ("1", "true", "True", "yes", "on")
    app.run(debug=debug, host="0.0.0.0", port=int(os.getenv("PORT", "5001")))


