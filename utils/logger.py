import os
import logging
from logging.handlers import RotatingFileHandler
import colorlog

# Ensure the logs directory exists at the root of the project
LOGS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
os.makedirs(LOGS_DIR, exist_ok=True)

_ANSI_GREEN      = '\x1b[32m'
_ANSI_LIGHT_BLUE = '\x1b[94m'
_LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
_LOG_FILE_PATH = os.path.join(LOGS_DIR, "job_apply_agent.log")
_shared_file_handler = None


class _ConnectorAwareFormatter(colorlog.ColoredFormatter):
    """Colors connector INFO lines light blue; everything else follows the normal level colors."""

    def format(self, record: logging.LogRecord) -> str:
        result = super().format(record)
        if record.name.endswith('_connector') and record.levelno == logging.INFO:
            if result.startswith(_ANSI_GREEN):
                result = _ANSI_LIGHT_BLUE + result[len(_ANSI_GREEN):]
        return result


class _SafeRotatingFileHandler(RotatingFileHandler):
    """Rotate when possible; if Windows still holds the file, keep writing."""

    def doRollover(self):
        try:
            super().doRollover()
        except OSError:
            if self.stream:
                try:
                    self.stream.close()
                except OSError:
                    pass
                self.stream = None
            try:
                self.stream = self._open()
            except OSError:
                pass


def _ensure_shared_file_handler() -> logging.Handler:
    """One rotating file handler per process, on the root logger."""
    global _shared_file_handler
    if _shared_file_handler is not None:
        return _shared_file_handler
    handler = _SafeRotatingFileHandler(
        _LOG_FILE_PATH,
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT))
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(handler)
    _shared_file_handler = handler
    return handler


class _FlushingStreamHandler(colorlog.StreamHandler):
    """Write INFO to the terminal immediately (Windows/piped consoles buffer otherwise)."""

    def emit(self, record: logging.LogRecord) -> None:
        super().emit(record)
        self.flush()


def setup_logger(name: str) -> logging.Logger:
    """Sets up a logger with a colored console handler (INFO) and rotating file handler (DEBUG)."""
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)  # Catch all levels; handlers will do the filtering

    # Only this logger — Logger.hasHandlers() also walks to root, which already
    # has the shared file handler, and would skip a console for every name after
    # the first setup_logger() call (so the terminal only showed remotive).
    if logger.handlers:
        return logger

    console_handler = _FlushingStreamHandler()
    console_handler.setLevel(logging.INFO)
    console_formatter = _ConnectorAwareFormatter(
        "%(log_color)s" + _LOG_FORMAT,
        datefmt=_DATE_FORMAT,
        log_colors={
            'DEBUG': 'cyan',
            'INFO': 'green',
            'WARNING': 'yellow',
            'ERROR': 'red',
            'CRITICAL': 'red,bg_white',
        }
    )
    console_handler.setFormatter(console_formatter)
    logger.addHandler(console_handler)
    _ensure_shared_file_handler()
    logger.propagate = True
    return logger
