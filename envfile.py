"""
Minimal .env loader shared by tncstation_daemon.py and tncstation_rest.py, so
one .env file in the project folder configures both programs.
"""

import os

from typing import List, MutableMapping, Optional


# Settings are TNCSTATION_* since the project was renamed from
# kamxl_winlink. KAMXL_* (the old prefix) is still read -- see
# apply_legacy_env().
ENV_PREFIX = "TNCSTATION_"
LEGACY_ENV_PREFIX = "KAMXL_"


def load_dotenv(path: str = ".env") -> None:
    """
    Populate os.environ from a simple KEY=VALUE .env file, if present
    -- e.g. WINLINK_API_KEY, so it doesn't have to be `export`ed by
    hand every time the daemon starts. A real environment variable
    that's already set always wins (standard dotenv behavior -- the
    file only fills in whatever isn't already set, it never
    overrides), so this is purely a convenience on top of the
    env-var-only design (see _require_winlink_api_key()'s docstring
    for why that was chosen over a CLI flag) -- not a config file the
    daemon depends on.

    Deliberately minimal, no third-party dotenv dependency (same
    stdlib-only choice tncstation_rest.py made for http.server and
    winlink_api.py made for urllib): blank lines and lines starting
    with "#" are skipped, "KEY=VALUE" is split on the first "=", and a
    value wrapped in matching single/double quotes has them stripped.
    No line continuation, no variable interpolation, no "export "
    prefix handling -- add it if a real need for any of that shows up.

    Silently does nothing if ``path`` doesn't exist -- this is a
    convenience, not a requirement; the daemon must keep working with
    real environment variables and no .env file at all, exactly as it
    did before this existed.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return

    for raw_line in lines:
        line = raw_line.strip()

        if not line or line.startswith("#") or "=" not in line:
            continue

        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()

        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]

        if key and key not in os.environ:
            os.environ[key] = value


def apply_legacy_env(
    environ: Optional[MutableMapping[str, str]] = None,
) -> List[str]:
    """
    Honor settings that still use the old KAMXL_* prefix: copy each
    KAMXL_FOO into TNCSTATION_FOO, unless TNCSTATION_FOO is already set
    (the new name always wins). Call it after load_dotenv(), so an old
    .env file works unchanged too.

    Returns the legacy names that were used, so the caller can suggest
    renaming them.
    """
    if environ is None:
        environ = os.environ

    used: List[str] = []

    for key in sorted(k for k in environ if k.startswith(LEGACY_ENV_PREFIX)):
        new_key = ENV_PREFIX + key[len(LEGACY_ENV_PREFIX):]

        if new_key not in environ:
            environ[new_key] = environ[key]
            used.append(key)

    return used


TRUE_VALUES = ("1", "true", "yes", "on")


def env_flag(name: str, environ: Optional[MutableMapping[str, str]] = None) -> bool:
    """
    True if the setting ``TNCSTATION_<name>`` is 1/true/yes/on (any
    case). Unset or anything else is False. Call after
    apply_legacy_env(), so an old KAMXL_<name> counts too.
    """
    if environ is None:
        environ = os.environ

    return environ.get(ENV_PREFIX + name, "").strip().lower() in TRUE_VALUES
