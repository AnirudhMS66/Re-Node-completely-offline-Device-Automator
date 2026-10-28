"""Shared loguru logger configured to write to the logs/ directory."""

import sys

from loguru import logger

import config

logger.remove()
logger.add(sys.stderr, level="INFO")
logger.add(config.LOG_DIR / "re_node.log", rotation="5 MB", retention=5, level="DEBUG")
