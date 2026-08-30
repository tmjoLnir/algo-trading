#!/usr/bin/env python3
"""Fail if the compose stack would expose a port, deploy the wrong code, or
come back from a reboot in pieces.

The API authenticates now (ADR 0008), and this check is unchanged by that,
because most of what it guards never did. `db` is Postgres with the base file's
`atp`/`atp`; `redis` holds the kill-switch state with no password in front of
it at all, so whoever reaches that port can clear a halt. Those two are the
reason the rule exists, and a sign-in screen on the API does not touch them.

For the API itself a session is a floor, not a perimeter: one operator, one
bcrypt hash, no TLS of our own and no way to revoke a session before it
expires. docs/SAFETY.md's "Access control" section states the rule that
follows: bind to localhost, and move exactly one port off it — the dashboard's,
to one private LAN or VPN address, via ATP_WEB_BIND_ADDR.

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

**Both configurations are checked**, because there are two. `docker-compose.yml`
is the development stack; `docker-compose.prod.yml` overlays it into the
deployed one (ADR 0011). The deployed file is the one where a wrong bind matters
most, and until it was added here it was the one file nothing looked at.

Restart policies are checked on **both**, and that is a correction rather than a
widening — see `check_restart_policies`. The one service in the repository still
missing a policy was `web`, which the overlay puts behind a profile, so scoping
this check to the deployed configuration meant the only service that could fail
it was the only one it never saw.

The deployed configuration is additionally checked for *shape*, which is a
different question from exposure and is here for the same reason. The overlay
removes the base file's source bind mounts and its `--reload` with compose's
`!reset` tag; a compose too old to know that tag, or an overlay someone edits
later, leaves them in place **silently**, and the stack then runs whatever
source is in the checkout on the host instead of the image that was built and
tested. Asserting the resolved configuration is the only way to tell — reading
the file tells you what was intended, not what compose did with it.

The `--reload` half of that reads every part of the resolved command rather than
asking whether the list contains it as an element. The base file now starts the
API through `sh -c` so that a configuration it cannot import is an exit rather
than a reloader idling forever with nothing bound, and an element test against
`["sh", "-c", "... --reload"]` is False — the check would have gone on passing
while the flag it exists to catch sat one level down inside the string.
"""

from __future__ import annotations

import ipaddress
import json
import os
import subprocess
import sys

#: A port bound to any of these is reachable from off the machine. `None` is in
#: the list because compose omits `host_ip` entirely for the `"8080:80"` short
#: form, which is the one that looks innocuous and means 0.0.0.0.
WILDCARD_HOSTS = {None, "", "0.0.0.0", "::"}

#: The two configurations, by the command that resolves each. The development
#: one asks for the `prod` profile so that `web-prod` is included; the deployed
#: one does not, because the overlay takes that service out of its profile and
#: puts the dev server into one.
CONFIGS = (
    ("development", ("docker", "compose", "--profile", "prod", "config", "--format", "json")),
    (
        "deployed",
        (
            "docker",
            "compose",
            "-f",
            "docker-compose.yml",
            "-f",
            "docker-compose.prod.yml",
            "config",
            "--format",
            "json",
        ),
    ),
)

#: Services whose code must come from the image rather than from the host. The
#: database is deliberately not one of them: its bind mount is `infra/db/init`,
#: which is configuration read once at initdb, not code.
CODE_SERVICES = ("api", "worker")


def _resolve(command: tuple[str, ...]) -> dict:
    """Run `docker compose config` and parse it, or exit explaining why not."""
    env = dict(os.environ)
    # The deployed overlay requires this and fails closed without it, which is
    # the point of it — but that is a *deploy-time* requirement about the value.
    # This check is about ports and shape, neither of which the password affects,
    # so a placeholder stands in when the operator has not set one. Their own
    # value is used when they have.
    env.setdefault("ATP_DB_PASSWORD", "placeholder-for-config-check")
    try:
        raw = subprocess.run(command, capture_output=True, text=True, check=True, env=env).stdout
    except FileNotFoundError:
        print("docker not found — cannot check port bindings", file=sys.stderr)
        raise SystemExit(2) from None
    except subprocess.CalledProcessError as exc:
        print(f"docker compose config failed:\n{exc.stderr}", file=sys.stderr)
        raise SystemExit(2) from None
    return json.loads(raw)


def check_bindings(label: str, services: dict) -> list[str]:
    """Report every published port that is not bound to a specific private address."""
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

    if exposed:
        print(f"ERROR [{label}]: a service publishes a port on every interface.")
        for line in exposed:
            print(f"  {line}")
    if public:
        print(f"ERROR [{label}]: a service publishes a port on a PUBLIC address.")
        for line in public:
            print(f"  {line}")
        print()
        print("That address is routable from the internet. If you looked up")
        print('"my IP address" to find it, that is your router\'s public address,')
        print("not your machine's — you want the private one from `ip -4 -brief")
        print("addr` (192.168.x, 10.x) or `tailscale ip -4` (100.x).")
    if unclassifiable:
        print(f"ERROR [{label}]: a host address that is not an IP literal.")
        for line in unclassifiable:
            print(f"  {line}")
        print()
        print("A name can resolve anywhere, and somewhere else tomorrow. Use a")
        print("literal address so what is exposed is readable from this file.")

    if not (exposed or public or unclassifiable):
        print(f"port bindings [{label}]: every published port is bound to a specific address")
        for line in bound:
            print(f"  {line}")
    return exposed + public + unclassifiable


def check_restart_policies(label: str, services: dict) -> list[str]:
    """Report every service that would not come back after a reboot.

    **Both configurations**, and the development one is not the afterthought it
    looks like. This check used to run against the deployed configuration alone,
    on the reasoning that surviving a reboot is a deployment concern — the same
    reasoning that once left `db`, `redis` and `api` without a policy while
    `worker` had one. It is wrong for the same reason it was wrong then: the
    development file also serves a dashboard, through `make up-prod`, and the
    cost of a missing policy is not "one service is down" but "the two ends of a
    request disagree about whether the stack is running".

    Scoping it to `deployed` also meant it could not see the service it needed
    to. The overlay puts the dev server behind a profile, so `web` is absent
    from the deployed configuration entirely — the one service in the repository
    still missing a restart policy was the one service this check never looked
    at.
    """
    problems = [
        f"{name} has no restart policy — a host reboot leaves it down"
        for name, service in sorted(services.items())
        if not service.get("restart")
    ]
    if problems:
        print(f"ERROR [{label}]: a service would not come back after a reboot.")
        for line in problems:
            print(f"  {line}")
        print()
        print("A stack that comes back in pieces is worse than one that stays down:")
        print("whatever serves the dashboard renders it perfectly against whatever")
        print("did not come back, and the result reads as a broken page rather than")
        print("as a stopped container.")
    else:
        print(f"restart policies [{label}]: every service comes back after a reboot")
    return problems


def check_deployed_shape(services: dict) -> list[str]:
    """Report anything that would deploy the checkout instead of the image.

    Each of these is a property the overlay claims and compose has to actually
    have delivered. `!reset` needs Compose v2.24+, and a version that does not
    know the tag leaves the base file's mounts and `--reload` in place without
    saying so.
    """
    problems: list[str] = []

    for name in CODE_SERVICES:
        service = services.get(name)
        if service is None:
            problems.append(f"{name} is missing from the deployed configuration")
            continue
        for volume in service.get("volumes") or []:
            problems.append(
                f"{name} still bind-mounts {volume.get('source')} -> {volume.get('target')}"
            )
        command = service.get("command") or []
        if any("--reload" in part for part in command):
            problems.append(f"{name} still runs with --reload: {' '.join(command)}")

    if problems:
        print("ERROR [deployed]: the deployed configuration is not the deployed shape.")
        for line in problems:
            print(f"  {line}")
        print()
        print("A source mount or a --reload here means the stack runs whatever is in")
        print("the checkout rather than the image that was built and tested.")
        print("`!reset` needs Compose v2.24+ — check `docker compose version`.")
    else:
        print("deployed shape: code comes from the image")
    return problems


def main() -> int:
    failures: list[str] = []
    for label, command in CONFIGS:
        services = _resolve(command).get("services", {})
        failures += check_bindings(label, services)
        failures += check_restart_policies(label, services)
        if label == "deployed":
            failures += check_deployed_shape(services)
        print()

    if failures:
        print("Not everything behind these ports asks who is calling (docs/SAFETY.md).")
        print("The API does. The Postgres behind it answers to atp/atp, and the Redis")
        print("holding the kill-switch state asks for nothing at all — so anyone who")
        print("can open a socket to that one can clear a halt. Bind 127.0.0.1 for")
        print("everything except the dashboard, and one private LAN or VPN address")
        print("via ATP_WEB_BIND_ADDR for that one.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
