import os
import os.path

# Env reader (stdlib only).
#
# This used to be `import environ` (the django-environ package). That import
# name is a trap: django-environ is installed as `django-environ` but imports
# as `environ`, and there is an unrelated, Python-2-only package on PyPI
# literally named `environ`. A user hitting "No module named 'environ'" would
# naturally `pip install environ`, get the py2 package, and the Live backend
# would then die at import with:
#     SyntaxError: invalid syntax (environ.py, line 114)
# which surfaces as a bare "live features disabled" in the UI.
#
# The whole dependency bought us ~20 typed getenv calls, so we do them here
# instead. Semantics deliberately match django-environ's Env: an UNSET var
# returns the default object untouched (so `str(..., False)` really yields the
# bool False, and `str(..., None)` yields None), while a SET var is a string
# that gets cast.

BOOLEAN_TRUE_STRINGS = ('true', 'on', 'ok', 'y', 'yes', '1')


class Env:
    """Minimal typed os.environ reader — the slice of django-environ we used."""

    @staticmethod
    def read_env(path='.env'):
        """Load KEY=VALUE lines from a .env file.

        setdefault, not assignment: env vars already exported by the caller
        (Harvestr sets STRMNTR_* before importing this module) must win over
        the file, which is django-environ's behaviour too.
        """
        try:
            with open(path, 'r', encoding='utf-8') as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith('#') or '=' not in line:
                        continue
                    key, _, val = line.partition('=')
                    val = val.strip().strip('\'"')
                    os.environ.setdefault(key.strip(), val)
        except OSError:
            pass

    @staticmethod
    def _raw(var, default):
        """Fetch the raw string, or a sentinel meaning "use the default".

        Mirrors django-environ's rule that an EMPTY value collapses to the
        default when that default is None -- this is what makes
        `STRMNTR_SEGMENT_TIME=""` mean "don't segment" rather than passing an
        empty string down into the ffmpeg argv.
        """
        val = os.environ.get(var)
        if val is None or (val == '' and default is None):
            return None
        return val

    def str(self, var, default=None):
        val = self._raw(var, default)
        return default if val is None else val

    def bool(self, var, default=False):
        val = self._raw(var, default)
        if val is None:
            return default
        val = val.strip()
        try:
            # django-environ treats any non-zero number as true, so "2" → True.
            return int(val) != 0
        except ValueError:
            return val.lower() in BOOLEAN_TRUE_STRINGS

    def int(self, var, default=0):
        val = self._raw(var, default)
        if val is None:
            return default
        try:
            return int(float(val.strip()))
        except (TypeError, ValueError):
            # django-environ raises here, taking the whole Live backend down at
            # import time over one typo'd env var. Falling back to the default
            # keeps recording alive; the value is a tuning knob, not a secret.
            return default

    def float(self, var, default=0.0):
        val = self._raw(var, default)
        if val is None:
            return default
        try:
            return float(val.strip())
        except (TypeError, ValueError):
            # Also deliberately unlike django-environ, which strips non-numeric
            # characters and would read "1:00:00" as 10000.0 -- a silently wrong
            # free-disk percentage is worse than falling back to the default.
            return default


env = Env()
if os.path.exists('.env'):
    Env.read_env('.env')


DOWNLOADS_DIR = env.str("STRMNTR_DOWNLOAD_DIR", "downloads")
MIN_FREE_DISK_PERCENT = env.float("STRMNTR_MIN_FREE_SPACE", 5.0)  # in %
DEBUG = env.bool("STRMNTR_DEBUG", False)

# The camsoda bot ignores this setting in favor of a chrome useragent generated with the fake-useragent library
HTTP_USER_AGENT = env.str("STRMNTR_USER_AGENT", "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:135.0) Gecko/20100101 Firefox/135.0")

# Specify the full path to the ffmpeg binary. By default, ffmpeg found on PATH is used.
FFMPEG_PATH = env.str("STRMNTR_FFMPEG_PATH", 'ffmpeg')

# Read rate for ffmpeg. This can be used to limit the rate at which ffmpeg reads input.
# An integer value represents the rate limit (in bytes per second). 
# True means ffmpeg native rate (typically real-time). False means unlimited.
FFMPEG_READRATE = env.str("STRMNTR_FFMPEG_READRATE", False)

# You can enter a number to select a specific height.
# Use a huge number here and closest match to get the highest resolution variant
# Eg: 240, 360, 480, 720, 1080, 1440, 99999
WANTED_RESOLUTION = env.int("STRMNTR_RESOLUTION", 99999)

# Specify match type when specified height
# Possible values: exact, exact_or_least_higher, exact_or_highest_lower, closest
# Beware of the exact policy. Nothing gets downloaded if the wanted resolution is not available
WANTED_RESOLUTION_PREFERENCE = env.str("STRMNTR_RESOLUTION_PREF", 'closest')

# Specify output container here
# Suggested values are 'mkv' or 'mp4'
CONTAINER = env.str("STRMNTR_CONTAINER", 'mkv')

# Add auto-generated VR format suffix to files
VR_FORMAT_SUFFIX = env.bool("STRMNTR_VR_FORMAT_SUFFIX", True)

# Specify the segment time in seconds
# If None, the video will be downloaded as a single file
# Example:
# 5 minutes
# SEGMENT_TIME = 300
# 1 hour
# SEGMENT_TIME = 3600
# Also see the ffmpeg documentation for the segment_time option
# You can specify time in hh:mm:ss format
# Example:
# 1 hour
# SEGMENT_TIME = '1:00:00'
SEGMENT_TIME = env.str("STRMNTR_SEGMENT_TIME", None)

# Keep only the N newest recordings per model; 0 (default) keeps everything.
# Applied after a recording finishes -- see Bot._prune_old_recordings.
KEEP_LAST_N = env.int("STRMNTR_KEEP_LAST_N", 0)


# HTTP Manager configuration

# Bind address for the web server
# 0.0.0.0 for remote access from all host
WEBSERVER_HOST = env.str("STRMNTR_HOST", "127.0.0.1")
WEBSERVER_PORT = env.int("STRMNTR_PORT", 5000)

# set frequency in seconds of how often the streamer list will update
WEB_LIST_FREQUENCY = env.int("STRMNTR_LIST_FREQ", 30)

# set frequency in seconds of how often the streamer's status will update on the recording page
WEB_STATUS_FREQUENCY = env.int("STRMNTR_STATUS_FREQ", 5)

# set theater_mode
WEB_THEATER_MODE = env.bool("STRMNTR_THEATER_MODE", False)

# confirm deletes, default to mobile-only.
# set to empty string to disable
# set to "MOBILE" to explicitly confirm deletes only on mobile
# set to any other non-falsy value to always check
WEB_CONFIRM_DELETES = env.str("STRMNTR_CONFIRM_DEL", "MOBILE")

# Web UI skin
# - kseen715 - 2nd skin, currently broken
# - truck-kun (default) - 3rd skin, row oriented
# - shaftoverflow - 4th skin, card layout, links in menus
WEBSERVER_SKIN = env.str("STRMNTR_SKIN", "truck-kun")

# Password for the web server
# If empty no auth required, else username admin and choosen password
WEBSERVER_PASSWORD = env.str("STRMNTR_PASSWORD", "admin")

VERIFY_SSL = env.bool("STRMNTR_VERIFY_SSL", True)