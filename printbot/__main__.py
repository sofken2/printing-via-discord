import logging
import os
import sys

from .bot import run
from .config import ConfigError, load_config


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("PRINTBOT_LOG_LEVEL", "INFO"),
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        config = load_config()
    except ConfigError as e:
        print(f"設定エラー: {e}", file=sys.stderr)
        sys.exit(2)
    run(config)


if __name__ == "__main__":
    main()
