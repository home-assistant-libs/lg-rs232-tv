"""Open the InStart / EzAdjust service menu on an LG webOS TV.

Requires the ``remote`` extra::

    pip install lg-tv-rs232[remote]

First run will prompt on the TV to accept pairing — accept it; the client key
is stored in ``./.lg_service_menu_keys.json`` (cwd) for future runs. Default
access code is 0413.

CLI usage::

    python -m lg_tv_rs232 service-menu                  # auto-discover, InStart
    python -m lg_tv_rs232 service-menu 192.168.1.42
    python -m lg_tv_rs232 service-menu --menu ezAdjust  # service menu 1
"""

from __future__ import annotations

import argparse
import asyncio
import json
import socket
import sys
from pathlib import Path
from urllib.parse import urlparse

from aiowebostv import WebOsClient

KEYS_PATH = Path(".lg_service_menu_keys.json")

SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 1900
SSDP_ST = "urn:lge-com:service:webos-second-screen:1"

# irKey values accepted by com.webos.app.factorywin's executeFactory entry point
# (per openlgtv hacking notes). inStart and ezAdjust are the two service menus.
IR_KEYS = ["inStart", "ezAdjust", "powerOnly", "inStop", "pCheck", "sCheck", "tilt"]


def discover(timeout: float = 3.0) -> list[str]:
    """Return IPs of LG webOS TVs that respond to an SSDP M-SEARCH."""
    msg = (
        "M-SEARCH * HTTP/1.1\r\n"
        f"HOST: {SSDP_ADDR}:{SSDP_PORT}\r\n"
        'MAN: "ssdp:discover"\r\n'
        f"MX: {int(timeout)}\r\n"
        f"ST: {SSDP_ST}\r\n"
        "\r\n"
    ).encode()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.settimeout(timeout)
    try:
        sock.sendto(msg, (SSDP_ADDR, SSDP_PORT))
        ips: list[str] = []
        while True:
            try:
                data, _ = sock.recvfrom(2048)
            except TimeoutError:
                break
            for line in data.decode("utf-8", "replace").splitlines():
                if line.lower().startswith("location:"):
                    host = urlparse(line.split(":", 1)[1].strip()).hostname
                    if host and host not in ips:
                        ips.append(host)
        return ips
    finally:
        sock.close()


async def open_service_menu(client: WebOsClient, ir_key: str) -> dict:
    """Launch com.webos.app.factorywin directly via SSAP system.launcher/launch."""
    return await client.request(
        "system.launcher/launch",
        payload={
            "id": "com.webos.app.factorywin",
            "params": {"id": "executeFactory", "irKey": ir_key},
        },
    )


async def enter_code(client: WebOsClient, code: str) -> None:
    """Type each digit of the access code. The keypad submits on the last digit."""
    await asyncio.sleep(1.0)  # let the factorywin keypad render
    for digit in code:
        await client.button(digit)
        await asyncio.sleep(0.15)


def load_keys(path: Path = KEYS_PATH) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_key(ip: str, client_key: str, path: Path = KEYS_PATH) -> None:
    keys = load_keys(path)
    if keys.get(ip) == client_key:
        return
    keys[ip] = client_key
    path.write_text(json.dumps(keys, indent=2))


async def run(ip: str, ir_key: str, code: str | None) -> None:
    client = WebOsClient(ip, load_keys().get(ip))
    try:
        await client.connect()
        if client.client_key:
            save_key(ip, client.client_key)
        result = await open_service_menu(client, ir_key)
        print(f"launch result: {result}", file=sys.stderr)
        if code:
            await enter_code(client, code)
    finally:
        await client.disconnect()


def resolve_ip(arg: str | None) -> str:
    if arg:
        return arg
    print("Discovering LG TVs via SSDP...", file=sys.stderr)
    ips = discover()
    if not ips:
        print("No LG TV found. Is it powered on and on the same network?", file=sys.stderr)
        sys.exit(1)
    if len(ips) == 1:
        print(f"Found TV at {ips[0]}", file=sys.stderr)
        return ips[0]
    print("Multiple TVs found, pass one as an argument:", file=sys.stderr)
    for ip in ips:
        print(f"  {ip}", file=sys.stderr)
    sys.exit(1)


def main(argv: list[str] | None = None, prog: str | None = None) -> int:
    parser = argparse.ArgumentParser(prog=prog, description=__doc__)
    parser.add_argument("ip", nargs="?", help="TV IP address (auto-discover if omitted)")
    parser.add_argument(
        "--menu",
        default="inStart",
        choices=IR_KEYS,
        help="Which factorywin entry point to launch (default: inStart)",
    )
    parser.add_argument(
        "--code",
        default="0413",
        help="Access code to type after the menu opens (default: 0413, '' to skip)",
    )
    args = parser.parse_args(argv)
    asyncio.run(run(resolve_ip(args.ip), args.menu, args.code))
    return 0


if __name__ == "__main__":
    sys.exit(main())
