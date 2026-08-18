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

Two failures, not one. `0.0.0.0` — or an absent host_ip, which means the same
thing — is the obvious one. The other is a **publicly routable** address, which
looks deliberate and reads as safe precisely because someone chose it: a person
who wants the dashboard on their phone looks up "my IP address", gets the
public one their router presents to the world, and sets that. It is a specific
address, so a wildcard check waves it through, and it is the worst possible
value for it.

A private address is fine, and that is the supported way to share this:
`ATP_WEB_BIND_ADDR` exists for exactly that. The test is `is_global` rather
than `is_private`, deliberately — Tailscale hands out addresses in the shared
CGNAT range 100.64.0.0/10, which reports `is_private == False` while being
unroutable from the internet, so an `is_private` test would refuse the VPN
route this project documents as the one to prefer.

Interpolation is resolved by compose before this sees it, so running with
`ATP_WEB_BIND_ADDR` set checks what you are about to start, not only what is
committed.
"""

from __future__ import annotations

import ipaddress
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
    public: list[str] = []
    unclassifiable: list[str] = []
    bound: list[str] = []
    for name, service in sorted(services.items()):
        for port in service.get("ports", []):
            host_ip = port.get("host_ip")
            published = port.get("published")
            if host_ip in WILDCARD_HOSTS:
                shown = host_ip or "0.0.0.0 (host_ip unset)"
                exposed.append(f"{name} publishes {published} on {shown}")
                continue
            try:
                address = ipaddress.ip_address(host_ip)
            except ValueError:
                if host_ip == "localhost":
                    bound.append(f"{name}: {host_ip}:{published}")
                else:
                    # A name can resolve anywhere, and to somewhere different
                    # tomorrow. Refuse rather than guess.
                    unclassifiable.append(f"{name} publishes {published} on {host_ip!r}")
                continue
            if address.is_global:
                public.append(f"{name} publishes {published} on {host_ip}")
            else:
                bound.append(f"{name}: {host_ip}:{published}")

    if exposed or public or unclassifiable:
        if exposed:
            print("ERROR: a service publishes a port on every interface.")
            for line in exposed:
                print(f"  {line}")
        if public:
            print("ERROR: a service publishes a port on a PUBLIC address.")
            for line in public:
                print(f"  {line}")
            print()
            print("That address is routable from the internet. If you looked up")
            print('"my IP address" to find it, that is your router\'s public address,')
            print("not your machine's — you want the private one from `ip -4 -brief")
            print("addr` (192.168.x, 10.x) or `tailscale ip -4` (100.x).")
        if unclassifiable:
            print("ERROR: a host address that is not an IP literal.")
            for line in unclassifiable:
                print(f"  {line}")
            print()
            print("A name can resolve anywhere, and somewhere else tomorrow. Use a")
            print("literal address so what is exposed is readable from this file.")
        print()
        print("There is no authentication in front of any of this (docs/SAFETY.md):")
        print("whoever reaches the port reads the whole book. Bind 127.0.0.1 for")
        print("everything except the dashboard, and one private LAN or VPN address")
        print("via ATP_WEB_BIND_ADDR for that one.")
        return 1

    print("port bindings: every published port is bound to a specific address")
    for line in bound:
        print(f"  {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
