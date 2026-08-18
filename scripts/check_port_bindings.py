#!/usr/bin/env python3
"""Fail if any compose service publishes a port on every interface.

The platform has no authentication — `get_current_user()` is a stub, and every
endpoint under /risk, /orders and /positions is reachable by anyone who can
open a socket to it. docs/SAFETY.md's "Access control" section states the rule
that follows from that: bind to localhost only until auth lands.

A rule stated only in prose is a rule that drifts. This is the same rule as a
check, run against the committed defaults so that a compose file which puts the
trading database, the Redis holding the kill-switch state, or an unauthenticated
API on every interface of the machine fails the build — rather than being
discovered later by whoever port-scans the subnet first.

`0.0.0.0` (or an absent host_ip, which means the same thing) is the failure. A
specific address is not: putting the dashboard on one LAN or VPN interface is
the supported way to share it, and `ATP_WEB_BIND_ADDR` exists for exactly that.
Interpolation is resolved by compose before this sees it, so running with that
variable set checks what you are about to start, not only what is committed.
"""

from __future__ import annotations

import json
import subprocess
import sys

#: A port bound to any of these is reachable from off the machine. `None` is in
#: the list because compose omits `host_ip` entirely for the `"8080:80"` short
#: form, which is the one that looks innocuous and means 0.0.0.0.
WILDCARD_HOSTS = {None, "", "0.0.0.0", "::"}

COMPOSE_CONFIG = ("docker", "compose", "--profile", "prod", "config", "--format", "json")


def main() -> int:
    try:
        raw = subprocess.run(COMPOSE_CONFIG, capture_output=True, text=True, check=True).stdout
    except FileNotFoundError:
        print("docker not found — cannot check port bindings", file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as exc:
        print(f"docker compose config failed:\n{exc.stderr}", file=sys.stderr)
        return 2

    services = json.loads(raw).get("services", {})

    exposed: list[str] = []
    bound: list[str] = []
    for name, service in sorted(services.items()):
        for port in service.get("ports", []):
            host_ip = port.get("host_ip")
            published = port.get("published")
            if host_ip in WILDCARD_HOSTS:
                shown = host_ip or "0.0.0.0 (host_ip unset)"
                exposed.append(f"{name} publishes {published} on {shown}")
            else:
                bound.append(f"{name}: {host_ip}:{published}")

    if exposed:
        print("ERROR: a service publishes a port on every interface.")
        for line in exposed:
            print(f"  {line}")
        print()
        print("There is no authentication in front of any of this (docs/SAFETY.md).")
        print("Bind each port to an address: 127.0.0.1 for everything except the")
        print("dashboard, and ATP_WEB_BIND_ADDR for that one.")
        return 1

    print("port bindings: every published port is bound to a specific address")
    for line in bound:
        print(f"  {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
