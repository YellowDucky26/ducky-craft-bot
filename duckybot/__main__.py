"""Start de bot: ``python -m duckybot``."""
import logging
import os

from .bot import DuckyBot
from .config import Config


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    config = Config.from_env()
    bot = DuckyBot(config)
    bot.run(config.discord_token, log_handler=None)


if __name__ == "__main__":
    main()
