"""
Command-line parameter parsing for the Qizhi (齐治) bastion bridge.

齐治堡垒机 launches the configured remote client directly, e.g.:

    C:\Program Files (x86)\QizhiTech\AccessClient\putty.exe

This replacement ``putty.exe`` receives PuTTY-style arguments.  This module
normalises the observed PuTTY invocation styles into the canonical
``ConnectionParams`` dataclass and returns any unrecognised tokens for
diagnostic purposes.

Supported input styles:

* Explicit SSH flags::

      -ssh user@host -P 22 -pw password
      -ssh host -P 22 -l user -pw password

* Bare host with flags::

      user@host -P 22 -pw password
      host -P 22 -l user -pw password

* Saved session style::

      -load "session_name" -l user -pw password

Any unknown or malformed input is logged and never allowed to crash the tool.
"""

import re
from pathlib import Path
from typing import Any, Dict, List, Tuple

from connection import ConnectionParams
from logger import get_logger

logger = get_logger("params")


#: Flags whose following token should be redacted from logs.
_PASSWORD_FLAGS = {
    "-pw", "-password",
    "--password", "--passwd", "--pass",
    "--token", "--secret", "--api-key", "--api_key", "--apikey",
}


#: Regex to extract ``user@host:port`` with optional password and port.
_USER_HOST_PORT_RE = re.compile(
    r"^(?:(?P<user>[^:@]+)(?::(?P<pass>[^@]*))?@)?"
    r"(?P<host>[^:\s]+)"
    r"(?::(?P<port>\d+))?$"
)


def _mask_inline_secret(value: str) -> str:
    """Redact credentials embedded in options or user/password host tokens."""
    option_match = re.match(
        r"(?i)^(-(?:pw|password)|--(?:password|passwd|pass|token|secret|api[-_]?key))=(.*)$",
        value,
    )
    if option_match:
        return f"{option_match.group(1)}=***"

    credential_match = re.match(r"(?i)^(ssh://)?([^/@:\s]+):([^@\s]*)@(.+)$", value)
    if credential_match:
        scheme = credential_match.group(1) or ""
        return f"{scheme}{credential_match.group(2)}:***@{credential_match.group(4)}"
    return value


def _mask_argv(argv: List[str]) -> List[str]:
    """Mask values that follow known password-like flags in argv."""
    masked: List[str] = []
    skip_next = False
    for token in argv:
        if skip_next:
            masked.append("***")
            skip_next = False
            continue
        lower = token.lower()
        if lower in _PASSWORD_FLAGS:
            masked.append(token)
            skip_next = True
        else:
            masked.append(_mask_inline_secret(token))
    return masked


def _extract_host_port(value: str) -> Dict[str, Any]:
    """Parse ``user@host:port`` or ``host:port`` into a dict."""
    result: Dict[str, Any] = {}
    match = _USER_HOST_PORT_RE.match(value)
    if match:
        groups = match.groupdict()
        if groups.get("user"):
            result["username"] = groups["user"]
        if groups.get("pass"):
            result["password"] = groups["pass"]
        result["host"] = groups.get("host")
        if groups.get("port"):
            try:
                result["port"] = int(groups["port"])
            except ValueError:
                pass
    return result


def _load_putty_session_file(raw_value: str) -> Dict[str, Any]:
    """Parse a PuTTY session configuration file.

    齐治堡垒机 passes ``-load tmp:<path>`` where ``<path>`` points to a temporary
    PuTTY session file containing HostName, PortNumber, UserName, etc.  This
    function tries several common PuTTY config file formats:

    * Backslash-separated::

          HostName\\192.168.1.10\\
          PortNumber\\22\\
          UserName\\admin\\

    * Equals-separated::

          HostName=192.168.1.10
          PortNumber=22
          UserName=admin

    * Colon-separated::

          HostName: 192.168.1.10

    Returns:
        A dict with any of ``host``, ``port``, ``username`` keys that were found.
    """
    result: Dict[str, Any] = {}

    # Strip optional "tmp:" prefix and whitespace.
    value = raw_value.strip()
    if value.lower().startswith("tmp:"):
        value = value[4:].strip()

    if not value:
        return result

    # If the value is not an existing file, return empty; the caller will keep
    # the raw value in extra for diagnostics.
    path = Path(value)
    if not path.is_file():
        return result

    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError as exc:
        logger.warning("Could not read PuTTY session file %s: %s", path, exc)
        return result

    # Try multiple key/value separators.
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(";") or line.startswith("#"):
            continue

        # 1) Key\value\  (PuTTY registry dump style)
        if "\\" in line:
            parts = line.split("\\")
            if len(parts) >= 3:
                key = parts[0].strip().lower()
                val = parts[1].strip()
                if key in ("hostname", "host"):
                    result["host"] = val
                elif key in ("portnumber", "port"):
                    try:
                        result["port"] = int(val)
                    except ValueError:
                        pass
                elif key in ("username", "user"):
                    result["username"] = val
            continue

        # 2) Key=value or Key: value
        for sep in ("=", ":"):
            if sep in line:
                key, _, val = line.partition(sep)
                key = key.strip().lower()
                val = val.strip()
                if key in ("hostname", "host"):
                    result["host"] = val
                elif key in ("portnumber", "port"):
                    try:
                        result["port"] = int(val)
                    except ValueError:
                        pass
                elif key in ("username", "user"):
                    result["username"] = val
                break

    logger.info("Loaded PuTTY session from %s: %r", path, result)
    return result


def parse_args(argv: List[str]) -> Tuple[ConnectionParams, Dict[str, Any]]:
    """Parse PuTTY-style arguments into ``ConnectionParams``.

    Returns:
        A tuple of ``(ConnectionParams, diagnostics)``.  The diagnostics dict
        contains raw/unknown tokens and intermediate parsing state for
        debugging.
    """
    diagnostics: Dict[str, Any] = {
        "raw_argv": list(argv),
        "errors": [],
    }

    cleaned = [str(token) for token in argv if token is not None and str(token) != ""]
    logger.info("Parsing argv: %s", _mask_argv(cleaned))

    result: Dict[str, Any] = {
        "host": None,
        "port": 22,
        "username": None,
        "password": None,
        "protocol": "ssh2",
        "key_path": None,
        "bastion_host": None,
        "extra": {},
    }

    iterator = iter(cleaned)
    positional_consumed = False

    for token in iterator:
        lower = token.lower()
        try:
            if lower == "-ssh":
                result["protocol"] = "ssh2"
                # The next token may be the host or user@host.
                value = next(iterator)
                parsed = _extract_host_port(value)
                if parsed.get("host"):
                    result["host"] = parsed["host"]
                    result["username"] = parsed.get("username") or result["username"]
                    result["password"] = parsed.get("password") or result["password"]
                    result["port"] = parsed.get("port") or result["port"]
                else:
                    # Might be a session name or bare host; store and re-evaluate later.
                    if result["host"] is None:
                        result["host"] = value
                    else:
                        result["extra"]["positional"] = result["extra"].get("positional", "") + " " + value

            elif lower == "-telnet":
                result["protocol"] = "telnet"

            elif lower == "-load":
                load_value = next(iterator)
                result["extra"]["session_raw"] = load_value
                session_data = _load_putty_session_file(load_value)
                if session_data:
                    result["host"] = session_data.get("host") or result["host"]
                    result["port"] = session_data.get("port") or result["port"]
                    result["username"] = session_data.get("username") or result["username"]
                else:
                    # Fallback: keep the raw value as session name for diagnostics.
                    result["extra"]["session_name"] = load_value

            elif lower == "-l":
                result["username"] = next(iterator)

            elif lower == "-p":
                # Lowercase -p is not standard PuTTY for port, but tolerate it.
                try:
                    result["port"] = int(next(iterator))
                except ValueError:
                    diagnostics["errors"].append(f"Invalid port after {token}")

            elif lower == "-P":
                try:
                    result["port"] = int(next(iterator))
                except ValueError:
                    diagnostics["errors"].append(f"Invalid port after {token}")

            elif lower in ("-pw", "-password"):
                result["password"] = next(iterator)

            elif lower == "-m":
                # PuTTY remote command file; record but do not execute.
                result["extra"]["command_file"] = next(iterator)

            elif lower == "-t":
                result["extra"]["allocate_pty"] = True

            elif lower.startswith("-"):
                # Unknown flag: consume a value if it looks like the flag takes one.
                diagnostics["errors"].append(f"Unknown flag {token}")
                # Heuristic: if next token does not start with '-', treat it as the value.
                # We do not consume it blindly to avoid swallowing positional hosts.

            elif not positional_consumed and result["host"] is None:
                # First bare positional token is treated as host or user@host.
                parsed = _extract_host_port(token)
                if parsed.get("host"):
                    result["host"] = parsed["host"]
                    result["username"] = parsed.get("username") or result["username"]
                    result["password"] = parsed.get("password") or result["password"]
                    result["port"] = parsed.get("port") or result["port"]
                else:
                    result["host"] = token
                positional_consumed = True

            else:
                result["extra"]["unknown_tokens"] = result["extra"].get("unknown_tokens", "") + " " + token

        except StopIteration:
            diagnostics["errors"].append(f"Flag {token} has no associated value")
            continue

    params = ConnectionParams(
        host=result["host"] or "",
        port=result["port"],
        username=result["username"] or "",
        password=result["password"],
        protocol=result["protocol"],
        key_path=result["key_path"],
        bastion_host=result["bastion_host"],
        extra=result["extra"],
    )

    diagnostics["final_params"] = params.to_dict()
    diagnostics["valid"] = params.is_valid()

    if not params.is_valid():
        logger.warning(
            "Parsed params are incomplete: host=%r username=%r port=%r",
            params.host,
            params.username,
            params.port,
        )

    return params, diagnostics
