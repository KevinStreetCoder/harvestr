
import logging
import re
from typing import Optional
import parameters
import sys

_ANSI_RE = re.compile(r'\x1b\[[0-9;]*m')

# Initialize colorama for Windows color support if available
try:
    from colorama import init, Fore, Back, Style
    init(autoreset=True)
    COLORAMA_AVAILABLE = True
except ImportError:
    COLORAMA_AVAILABLE = False


# Console output must tolerate non-ASCII (emoji, em-dash, …). On a Windows
# cp1252 console such characters would otherwise raise UnicodeEncodeError
# inside the StreamHandler mid-log. The file handler is already utf-8; this
# makes stdout escape un-encodable chars instead of crashing the handler.
try:
    sys.stdout.reconfigure(errors="backslashreplace")
except Exception:
    pass


# Persistent, retained ERROR log for the live recorder. streamonitor.log keeps
# everything but is huge and INFO-heavy; this is an errors-only file in the
# project logs/ dir, daily-rotated and kept ~14 days, so past errors stay easy
# to read. Added once to the root logger; per-bot loggers propagate to it.
try:
    import os as _os
    from logging.handlers import TimedRotatingFileHandler as _TRFH
    _err_dir = _os.path.join(
        _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))),
        "logs",
    )
    _os.makedirs(_err_dir, exist_ok=True)
    _root = logging.getLogger()
    if not any(getattr(_h, "_live_err_marker", False) for _h in _root.handlers):
        _eh = _TRFH(_os.path.join(_err_dir, "live-errors.log"),
                    when="midnight", backupCount=14, encoding="utf-8", delay=True)
        _eh.setLevel(logging.ERROR)
        _eh.setFormatter(logging.Formatter(
            "%(asctime)s - %(levelname)-8s - %(name)s: %(message)s"))
        _eh._live_err_marker = True
        _root.addHandler(_eh)
except Exception:
    pass


class _PlainFormatter(logging.Formatter):
    def format(self, record):
        record.msg = _ANSI_RE.sub('', str(record.msg))
        return super().format(record)


_SM_FH = None
_SM_FH_LOCK = __import__("threading").Lock()


def _streamonitor_file_handler(level):
    """ONE FileHandler (one OS handle) on logs/streamonitor.log, shared by every
    bot logger. It used to be one per logger name: 1670 open handles to the
    same file on a 1651-model fleet. %(name)s carries the logger name that used
    to be baked into each formatter, so the lines read exactly as before."""
    global _SM_FH
    with _SM_FH_LOCK:
        if _SM_FH is None:
            import os
            log_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'logs')
            os.makedirs(log_dir, exist_ok=True)
            h = logging.FileHandler(os.path.join(log_dir, 'streamonitor.log'), encoding='utf-8')
            h.setFormatter(_PlainFormatter('%(asctime)s - %(levelname)-8s - %(name)s: %(message)s'))
            h.setLevel(level)
            _SM_FH = h
        return _SM_FH


class ColoredFormatter(logging.Formatter):
    """Custom formatter that gets colors from the bot instance."""
    
    def __init__(self, logger_name: str, bot_instance=None):
        super().__init__('%(asctime)s - %(levelname)s - {}: %(message)s'.format(logger_name))
        self.bot_instance = bot_instance
    
    def format(self, record):
        # Get colors from bot if available
        if self.bot_instance and hasattr(self.bot_instance, 'get_site_color'):
            try:
                color, attrs = self.bot_instance.get_site_color()
            except:
                color, attrs = ("white", [])
        else:
            color, attrs = ("white", [])

        from termcolor import colored, COLORS

        # Validate color name against termcolor's supported colors
        if color not in COLORS:
            color = "white"

        try:
            # Color the timestamp with different time parts
            formatted_time = self.formatTime(record, self.datefmt)
            # Split time into date and time parts for better coloring
            parts = formatted_time.split(' ', 1)
            if len(parts) == 2:
                date_part, time_part = parts
                colored_time = colored(date_part, "grey") + " " + colored(time_part, "cyan", attrs=["bold"])
            else:
                colored_time = colored(formatted_time, "cyan")

            # Color the log level based on level type
            level_colors = {
                'DEBUG': ("white", []),
                'INFO': ("green", ["bold"]),
                'WARNING': ("yellow", ["bold"]),
                'ERROR': ("red", ["bold"]),
                'CRITICAL': ("red", ["bold", "underline"])
            }
            level_color, level_attrs = level_colors.get(record.levelname, ("white", []))
            colored_level = colored(record.levelname.ljust(8), level_color, attrs=level_attrs)

            # Color the logger name (site + username) with site color and bold
            logger_name_part = self._style._fmt.split(': %(message)s')[0].split(' - ')[-1]
            # Filter attrs to only valid termcolor attrs
            valid_attrs = [a for a in attrs if a in ("bold", "dark", "underline", "blink", "reverse", "concealed")]
            colored_logger_name = colored(logger_name_part, color, attrs=["bold"] + valid_attrs)

            # The message may already contain ANSI codes from colored() calls - pass through as-is
            message = record.getMessage()

            # Create the final formatted message with better spacing
            return f"{colored_time} - {colored_level} - {colored_logger_name}: {message}"
        except Exception:
            # Fallback to plain formatting if anything fails
            return super().format(record)


class Logger:
    def __init__(self, name: str = "__name__", bot_instance=None) -> None:
        self.name = name
        self.bot_instance = bot_instance
        self.formatter = ColoredFormatter(name, bot_instance)
        self.handler: Optional[logging.StreamHandler] = None
        
        # Avoid duplicate handlers
        self.logger = logging.getLogger(self.name)
        if not self.logger.handlers:
            self.handler = logging.StreamHandler(sys.stdout)
            self.handler.setFormatter(self.formatter)

            loglevel = logging.DEBUG if parameters.DEBUG else logging.INFO
            self.logger.setLevel(loglevel)
            self.logger.addHandler(self.handler)

            # Add file handler for persistent logging (one shared handle)
            try:
                self.logger.addHandler(_streamonitor_file_handler(loglevel))
            except Exception:
                pass

    def get_logger(self) -> logging.Logger:
        """Get the configured logger instance."""
        logger = logging.getLogger(self.name)
        
        # Only add handler if not already present
        if not logger.handlers and self.handler:
            logger.setLevel(logging.DEBUG if parameters.DEBUG else logging.INFO)
            logger.addHandler(self.handler)
        
        return logger

    def debug(self, msg: str) -> None:
        self.logger.debug(msg)

    def warning(self, msg: str) -> None:
        self.logger.warning(msg)

    def error(self, msg: str) -> None:
        self.logger.error(msg)

    def info(self, msg: str) -> None:
        self.logger.info(msg)

    def exception(self, msg: str) -> None:
        self.logger.exception(msg)

    def critical(self, msg: str) -> None:
        self.logger.critical(msg)
    
    def setLevel(self, level: int) -> None:
        """Set the logging level for this logger."""
        self.logger.setLevel(level)
        if self.handler:
            self.handler.setLevel(level)

    def verbose(self, msg: str) -> None:
        if parameters.DEBUG:
            self.logger.debug(msg)

    def set_level(self, level: int) -> None:
        self.logger.setLevel(level)