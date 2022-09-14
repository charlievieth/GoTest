import logging
import sys


NAME = "GoTest"


_logger_initialized = False


def init_logger(level: int = logging.INFO) -> None:
    global _logger_initialized
    if not _logger_initialized:
        logging.basicConfig(
            level=level,
            format="[%(name)s:%(levelname)s] [%(filename)s:%(lineno)d]: %(message)s",
            handlers=[logging.StreamHandler(sys.stdout)],
        )
        _logger_initialized = True


def get_logger(name: str = NAME) -> logging.Logger:
    init_logger()
    return logging.getLogger(name)
