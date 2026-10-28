"""Logging setup and the shared `session_lib` logger."""

import logging
import sys


log = logging.getLogger("session_lib")


class _ConsoleColorFormatter(logging.Formatter):
    """Colorizes console output: PASSED results green, FAILED/error output
    red, and "=== ... ===" section banners (a new row, row block, or check
    starting) blue. Plain text otherwise -- this never touches the file
    handler, so the on-disk log stays clean and grep/diff-friendly."""

    RESET = "\033[0m"
    GREEN = "\033[32m"
    RED = "\033[31m"
    BLUE = "\033[34m"

    def format(self, record):
        text = super().format(record)
        message = record.getMessage()
        if "RESULT: PASSED" in message:
            color = self.GREEN
        elif record.levelno >= logging.ERROR or "RESULT: FAILED" in message:
            color = self.RED
        elif message.strip().startswith("==="):
            color = self.BLUE
        else:
            return text
        return f"{color}{text}{self.RESET}"


class _OwnDebugOnly(logging.Filter):
    """DEBUG records only from this project's own loggers. Libraries log at
    DEBUG through the root logger too: bleak/dbus-fast dump every D-Bus
    signal, including each nearby device's address and name."""

    OWN = ("session_lib", "reconnect_regression", "__main__")

    def filter(self, record):
        return record.levelno >= logging.INFO or record.name.split(".")[0] in self.OWN


def setup_logging(session_dir):
    """Configure logging to both the console and a log file inside this
    session's own directory, so the full history of a session is on disk,
    not just scrollback."""
    log_path = session_dir / "session.log"
    root = logging.getLogger()
    # DEBUG-level records (e.g. the periodic driver heartbeat) are written to
    # the file but filtered out of the console, which shows a live progress
    # bar instead -- see sleep_and_watch_driver.
    root.setLevel(logging.DEBUG)
    fmt = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    file_handler = logging.FileHandler(log_path)
    file_handler.setLevel(logging.DEBUG)
    file_handler.addFilter(_OwnDebugOnly())
    file_handler.setFormatter(fmt)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    if sys.stdout.isatty():
        console_handler.setFormatter(
            _ConsoleColorFormatter(
                fmt="%(asctime)s [%(levelname)s] %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
    else:
        console_handler.setFormatter(fmt)
    root.handlers = [file_handler, console_handler]
    log.info(f"logging to {log_path}")
    return log_path
