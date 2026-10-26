#!/usr/bin/env python3
"""Create a new private, provider-disabled local installation (stdlib only)."""

import argparse
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shlex
from urllib.parse import urlsplit


def public_origin(value):
    try:
        parsed = urlsplit(value)
        if (not value.startswith("https://") or not value.isascii()
                or any(c.isspace() for c in value) or any(c in value for c in "\\%@?#")
                or not parsed.hostname or parsed.path or parsed.username is not None
                or parsed.password is not None or parsed.port not in (None, 443)
                or parsed.netloc.endswith(":")):
            raise ValueError
        if ":" in parsed.hostname:
            ipaddress.IPv6Address(parsed.hostname)
        elif not all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                     for label in parsed.hostname.split(".")):
            raise ValueError
    except ValueError:
        raise argparse.ArgumentTypeError("expected an HTTPS origin on port 443, without path, credentials, query or fragment") from None
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True, help="new private directory; absolute path or ~/path")
    parser.add_argument("--public-origin", type=public_origin, default="https://mcp.example.com")
    args = parser.parse_args()
    root = Path(args.directory).expanduser()
    if not root.is_absolute():
        parser.error("--directory must be absolute (or start with ~)")
    # The parent must already exist and be trusted. Never resolve/follow an existing leaf.
    root = root.parent.resolve(strict=True) / root.name
    repo = Path(__file__).resolve().parent
    if root == repo or repo in root.parents:
        parser.error("--directory must be outside the source checkout")
    os.umask(0o077)
    try:
        root.mkdir(mode=0o700)
    except FileExistsError:
        parser.error("--directory already exists (including symlinks); nothing overwritten")
    credentials, state = root / "credentials", root / "state"
    credentials.mkdir(mode=0o700)
    state.mkdir(mode=0o700)

    def write(path, value, mode=0o600):
        with path.open("x") as stream:
            stream.write(value)
        path.chmod(mode)

    write(credentials / "mcp-token", secrets.token_urlsafe(48) + "\n")
    write(credentials / "slack-token", "")
    write(credentials / "slack-signing-secret", "")
    write(root / "config.json", json.dumps({
        "slack_team_id": None, "slack_owner_id": None, "slack_rooms": [],
        "slack_send_enabled": False, "owner_alert_enabled": False,
    }, indent=2) + "\n")
    environment = {
        "CREDENTIALS_DIRECTORY": str(credentials), "STATE_DIRECTORY": str(state),
        "SLACK_CONFIG": str(root / "config.json"), "MCP_PUBLIC_ORIGIN": args.public_origin,
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    script = "#!/bin/sh\nset -eu\numask 077\ncd " + shlex.quote(str(repo)) + "\n"
    script += "".join("export " + key + "=" + shlex.quote(value) + "\n" for key, value in environment.items())
    command = [str(repo / ".venv/bin/python"), "-m", "uvicorn", "server:app_factory", "--factory",
               "--host", "127.0.0.1", "--port", "8002", "--workers", "1",
               "--no-proxy-headers", "--no-access-log"]
    write(root / "run.sh", script + "exec " + shlex.join(command) + "\n", 0o700)
    print("Created provider-disabled local configuration. Run the new directory's run.sh; credentials stay in files.")


if __name__ == "__main__":
    main()
