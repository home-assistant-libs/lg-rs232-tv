"""CLI to test an LG TV over RS232.

Usage:
    python -m lg_rs232_tv /dev/ttyUSB0
    python -m lg_rs232_tv socket://192.168.1.29:5000
    python -m lg_rs232_tv 'esphome://192.168.1.29/?port_name=TTL'
    python -m lg_rs232_tv /dev/ttyUSB0 --set-id 2
    python -m lg_rs232_tv /dev/ttyUSB0 --power on
    python -m lg_rs232_tv service-menu              # webOS service menu (needs [remote] extra)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from . import (
    DEFAULT_SET_ID,
    LGTV,
    AspectRatio,
    CommandRejected,
    InputSource,
    PowerState,
    RemoteKey,
    TVState,
)


def _format_enum(val: object | None) -> str:
    if val is None:
        return "?"
    if hasattr(val, "name"):
        return val.name
    return str(val)


def _format_bool(val: bool | None, on: str = "ON", off: str = "OFF") -> str:
    if val is None:
        return "?"
    return on if val else off


def _format_percent(val: int | None) -> str:
    if val is None:
        return "?"
    return f"{val}%"


def _print_state(state: TVState) -> None:
    print()
    print("=== LG TV Status ===")
    print()
    print(f"  Power:           {_format_enum(state.power)}")
    print(f"  Input source:    {_format_enum(state.input_source)}")
    print(f"  Aspect ratio:    {_format_enum(state.aspect_ratio)}")
    print(f"  Screen mute:     {_format_enum(state.screen_mute)}")
    print(f"  Volume mute:     {_format_bool(state.volume_mute)}")
    print(f"  Volume:          {_format_percent(state.volume)}")
    print()
    print("  Picture:")
    print(f"    Picture mode:  {_format_enum(state.picture_mode)}")
    print(f"    Color temp:    {_format_enum(state.color_temperature)}")
    print(f"    Energy saving: {_format_enum(state.energy_saving)}")
    print(f"    Backlight:     {_format_percent(state.backlight)}")
    print(f"    Contrast:      {_format_percent(state.contrast)}")
    print(f"    Brightness:    {_format_percent(state.brightness)}")
    print(f"    Color:         {_format_percent(state.color)}")
    print(f"    Tint:          {_format_percent(state.tint)}")
    print(f"    Sharpness:     {_format_percent(state.sharpness)}")
    print()
    print("  Audio:")
    print(f"    Sound mode:    {_format_enum(state.sound_mode)}")
    print(f"    Treble:        {_format_percent(state.treble)}")
    print(f"    Bass:          {_format_percent(state.bass)}")
    print(f"    Balance:       {_format_percent(state.balance)}")
    print()
    print("  System:")
    print(f"    OSD:           {_format_bool(state.osd_enabled)}")
    print(f"    Remote lock:   {_format_bool(state.remote_lock)}")
    print()


async def _diagnose(port: str, set_id: int) -> int:
    """Open the port, broadcast power queries to many set IDs, and dump anything
    we hear back. Useful when the TV's set ID is unknown or the cable is suspect.
    """
    import serialx

    from . import BAUD_RATE

    print(f"[diag] Opening {port} at {BAUD_RATE} baud (raw)...")
    reader, writer = await serialx.open_serial_connection(port, baudrate=BAUD_RATE)
    received: list[bytes] = []

    async def reader_task() -> None:
        while True:
            chunk = await reader.read(256)
            if not chunk:
                return
            print(f"[diag] RX: {chunk!r}")
            received.append(chunk)

    task = asyncio.create_task(reader_task())
    try:
        for sid in range(0, 100):
            writer.write(f"ka {sid:02d} ff\r".encode("ascii"))
        await writer.drain()
        print("[diag] Wrote power query to set IDs 0..99. Listening 5 seconds...")
        await asyncio.sleep(5)
    finally:
        task.cancel()
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass

    if received:
        print(
            f"[diag] Got {len(received)} chunks; total {sum(len(c) for c in received)} bytes"
        )
        return 0
    print(
        "[diag] Nothing received. Most likely the TV is off, the RX line is "
        "not connected (try the TX/RX swap switch on the proxy), or the TV's "
        "RS-232 setting is disabled in its menu."
    )
    return 1


async def _run(args: argparse.Namespace) -> int:
    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)

    if args.diagnose:
        return await _diagnose(args.port, args.set_id)

    tv = LGTV(args.port, set_id=args.set_id)
    print(f"Connecting to {args.port} (set_id={args.set_id})...")
    try:
        await tv.connect()
    except ConnectionError as err:
        print(f"Error: {err}", file=sys.stderr)
        return 1

    try:
        try:
            if args.power == "on":
                print("Sending power on...")
                await tv.power_on()
                return 0
            if args.power == "off":
                print("Sending power off...")
                await tv.power_off()
                return 0
            if args.input is not None:
                try:
                    source = InputSource[args.input.upper()]
                except KeyError:
                    print(
                        f"Unknown input source: {args.input!r}. "
                        f"Choices: {', '.join(s.name for s in InputSource)}",
                        file=sys.stderr,
                    )
                    return 1
                print(f"Selecting input {source.name}...")
                await tv.select_input_source(source)
                return 0
            if args.volume is not None:
                print(f"Setting volume to {args.volume}%...")
                await tv.set_volume(args.volume)
                return 0
            if args.mute == "on":
                print("Muting...")
                await tv.mute_on()
                return 0
            if args.mute == "off":
                print("Unmuting...")
                await tv.mute_off()
                return 0
            if args.aspect is not None:
                try:
                    ratio = AspectRatio[args.aspect.upper()]
                except KeyError:
                    print(
                        f"Unknown aspect ratio: {args.aspect!r}. "
                        f"Choices: {', '.join(r.name for r in AspectRatio)}",
                        file=sys.stderr,
                    )
                    return 1
                print(f"Setting aspect ratio {ratio.name}...")
                await tv.set_aspect_ratio(ratio)
                return 0
            if args.key is not None:
                try:
                    key = RemoteKey[args.key.upper()]
                except KeyError:
                    print(
                        f"Unknown remote key: {args.key!r}. "
                        f"Choices: {', '.join(k.name for k in RemoteKey)}",
                        file=sys.stderr,
                    )
                    return 1
                print(f"Sending remote key {key.name}...")
                await tv.send_remote_key(key)
                return 0

            # Default: query everything and print
            try:
                power = await tv.query_power()
            except CommandRejected as err:
                print(f"Power query failed: {err}", file=sys.stderr)
                power = None

            if power is PowerState.OFF:
                print()
                print(
                    "TV is OFF — most queries will be skipped (TV does not "
                    "respond to status queries while in standby)."
                )
                _print_state(tv.state)
                return 0

            print("Querying TV state...")
            await tv.query_state()
            _print_state(tv.state)
            return 0
        except CommandRejected as err:
            print(
                f"Warning: TV rejected the command (NG). The command is "
                f"likely not supported by this model or current configuration "
                f"(e.g. volume/mute have no effect when audio is routed to "
                f"optical out). Details: {err}",
                file=sys.stderr,
            )
            return 1
    finally:
        await tv.disconnect()


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "service-menu":
        try:
            from .remote import main as remote_main
        except ImportError:
            print(
                "Error: the 'service-menu' command requires the 'remote' extra. "
                "Install with: pip install 'lg-rs232-tv[remote]'",
                file=sys.stderr,
            )
            sys.exit(1)
        sys.exit(remote_main(sys.argv[2:], prog="python -m lg_rs232_tv service-menu"))

    parser = argparse.ArgumentParser(
        description="Test an LG TV over RS232",
    )
    parser.add_argument(
        "port",
        help=(
            "Serial port URL. Examples: /dev/ttyUSB0, "
            "socket://192.168.1.29:5000, "
            "esphome://192.168.1.29/?port_name=TTL"
        ),
    )
    parser.add_argument(
        "--set-id",
        type=int,
        default=DEFAULT_SET_ID,
        help=f"TV set ID (default: {DEFAULT_SET_ID})",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")
    parser.add_argument(
        "--diagnose",
        action="store_true",
        help="Diagnostic mode: broadcast power queries to set IDs 0..99 and "
        "dump everything received. Useful when the TV's set ID or wiring is unknown.",
    )

    action = parser.add_mutually_exclusive_group()
    action.add_argument("--power", choices=["on", "off"], help="Set power state")
    action.add_argument(
        "--input",
        help="Select input source (e.g. HDMI1, HDMI2, AV1, COMPONENT1, RGB_PC)",
    )
    action.add_argument("--volume", type=int, help="Set volume 0..100")
    action.add_argument("--mute", choices=["on", "off"], help="Set mute")
    action.add_argument(
        "--aspect",
        help="Set aspect ratio (e.g. R_4_3, R_16_9, JUST_SCAN, FULL_WIDE)",
    )
    action.add_argument(
        "--key", help="Send a remote-control key (e.g. POWER, MENU, OK, HOME)"
    )

    args = parser.parse_args()
    sys.exit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
