"""Account login and credential storage.

Roborock has no LAN-only mode worth relying on: the robot is reached through
Roborock's cloud, so operating it means holding an account credential. Two rules
follow, and both are enforced here rather than left to the caller:

* the account **password** is never stored - it is exchanged once for a token
  (``UserData``), and only the token is written to disk;
* the token file is written with owner-only permissions, and its path is
  reported but its contents are never logged or returned by a tool.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from ...errors import ConfigError, ConnectionFailed

log = logging.getLogger(__name__)

DEFAULT_CREDENTIALS_NAME = "roborock-credentials.json"


@dataclass
class RoborockAccount:
    """Where the account token lives and which device it selects."""

    email: str
    credentials_file: Path
    device_uid: str | None = None
    device_name: str | None = None
    base_url: str | None = None

    @classmethod
    def from_config(cls, config, state_dir: Path, env: dict | None = None) -> RoborockAccount:
        """Read the roborock keys out of a device config's ``extra`` table."""
        env = dict(os.environ if env is None else env)
        extra = dict(config.extra or {})
        email = extra.get("email") or env.get("ROBOROCK_EMAIL")
        if not email:
            raise ConfigError(
                f"device '{config.device_id}': roborock needs an account email",
                hint='Add email = "you@example.com" under the device, or set ROBOROCK_EMAIL. '
                     "Then run `mhs roborock-login` once to exchange it for a token.",
            )
        raw_path = extra.get("credentials_file") or env.get("ROBOROCK_CREDENTIALS")
        path = Path(raw_path).expanduser() if raw_path else state_dir / DEFAULT_CREDENTIALS_NAME
        return cls(
            email=email,
            credentials_file=path,
            device_uid=extra.get("device_uid") or env.get("ROBOROCK_DEVICE_UID"),
            device_name=extra.get("device_name") or env.get("ROBOROCK_DEVICE_NAME"),
            base_url=extra.get("base_url") or env.get("ROBOROCK_BASE_URL"),
        )


def save_credentials(path: Path, email: str, user_data: dict, base_url: str | None = None) -> Path:
    """Write the account token with owner-only permissions."""
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"email": email, "base_url": base_url, "user_data": user_data}
    # Create restricted, then write: never widen an existing file, and never
    # leave a window where the token is world-readable.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        json.dump(payload, handle, indent=2)
    os.chmod(path, 0o600)
    return path


def load_credentials(path: Path) -> dict:
    path = Path(path).expanduser()
    if not path.is_file():
        raise ConfigError(
            f"no Roborock credentials at {path}",
            hint="Run `mhs roborock-login` once; it exchanges your password for a token "
                 "and stores only the token.",
        )
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        log.warning("%s is readable by other users (mode %o); tightening to 600", path, mode)
        os.chmod(path, 0o600)
    try:
        payload = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path} is not valid JSON: {exc}") from exc
    if "user_data" not in payload:
        raise ConfigError(f"{path} does not contain a Roborock token; log in again")
    return payload


async def login(email: str, password: str, base_url: str | None = None) -> dict:
    """Exchange an email and password for a token. The password is not retained."""
    from roborock.web_api import RoborockApiClient

    client = RoborockApiClient(username=email, base_url=base_url)
    try:
        user_data = await client.pass_login(password)
    except Exception as exc:  # the library raises several unrelated types
        raise ConnectionFailed(
            f"Roborock login failed for {email}: {exc}",
            hint="Check the password, or use `mhs roborock-login --code` to sign in with "
                 "an emailed code instead.",
        ) from exc
    return user_data.as_dict()


async def request_email_code(email: str, base_url: str | None = None) -> None:
    """Ask Roborock to email a one-time sign-in code."""
    from roborock.web_api import RoborockApiClient

    client = RoborockApiClient(username=email, base_url=base_url)
    try:
        await client.request_code()
    except Exception as exc:
        raise ConnectionFailed(f"could not request a code for {email}: {exc}") from exc


async def login_with_code(email: str, code: str, base_url: str | None = None) -> dict:
    """Exchange an emailed code for a token."""
    from roborock.web_api import RoborockApiClient

    client = RoborockApiClient(username=email, base_url=base_url)
    try:
        user_data = await client.code_login(code)
    except Exception as exc:
        raise ConnectionFailed(f"Roborock code login failed for {email}: {exc}") from exc
    return user_data.as_dict()
