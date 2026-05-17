"""Main LGTV controller."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable, Iterable

import serialx

from .const import (
    ACK_TERMINATOR,
    BAUD_RATE,
    COMMAND_TIMEOUT,
    CR,
    DEFAULT_SET_ID,
    AspectRatio,
    ColorTemperature,
    EnergySaving,
    InputSource,
    LegacyInputSource,
    PictureMode,
    PowerState,
    RemoteKey,
    ScreenMute,
    SoundMode,
)
from .protocol import (
    CommandRejected,
    PendingCommand,
    Response,
    data_to_percent,
    encode_command,
    int_to_hex_byte,
    parse_response,
    percent_to_data,
)
from .state import TVState

_LOGGER = logging.getLogger(__name__)


StateCallback = Callable[[TVState | None], None]


class TVNotRespondingError(ConnectionError):
    """Raised when the serial port opened but no LG TV responded.

    The serial connection itself succeeded, but the TV did not return a valid
    reply to the power query during ``connect()`` -- e.g. RS-232C control is
    disabled, the TV is off, the set ID is wrong, or the cable is miswired.
    Subclasses ``ConnectionError`` so it can be distinguished from a transport
    failure (the port itself being unreachable).
    """

# Each entry maps a (command1, command2) pair. The response is identified by
# command2 alone (the LG protocol does not echo command1).
_QUERY_DATA = "ff"

# Known status-query commands attempted during ``query_state()``. Each entry is
# (command1, command2, state_attr, parser). Parsers are pulled from local
# methods on LGTV at runtime so they can update enum-typed fields cleanly.
_STATE_QUERIES: tuple[tuple[str, str, str], ...] = (
    ("k", "a", "power"),
    ("x", "b", "input_source"),
    ("k", "c", "aspect_ratio"),
    ("k", "d", "screen_mute"),
    ("k", "e", "volume_mute"),
    ("k", "f", "volume"),
    ("k", "g", "contrast"),
    ("k", "h", "brightness"),
    ("k", "i", "color"),
    ("k", "j", "tint"),
    ("k", "k", "sharpness"),
    ("k", "l", "osd_enabled"),
    ("k", "m", "remote_lock"),
    ("k", "r", "treble"),
    ("k", "s", "bass"),
    ("k", "t", "balance"),
    ("k", "u", "color_temperature"),
    ("j", "q", "energy_saving"),
    ("m", "g", "backlight"),
    ("d", "x", "picture_mode"),
    ("d", "y", "sound_mode"),
)

# State attribute names that can be passed to ``LGTV.query()``.
QUERYABLE_ATTRIBUTES: frozenset[str] = frozenset(
    attr for _command1, _command2, attr in _STATE_QUERIES
)


class LGTV:
    """Async controller for an LG TV over RS232.

    The controller speaks the LG TVLINK / TV-RS232 ASCII protocol over a
    serial connection (any serialx-supported URL: ``/dev/ttyUSB0``,
    ``socket://host:port``, ``esphome://host/?port_name=TTL``, etc).
    """

    def __init__(
        self,
        port: str,
        set_id: int = DEFAULT_SET_ID,
    ) -> None:
        self._port = port
        self._set_id = set_id
        self._reader: asyncio.StreamReader | None = None
        self._writer: serialx.SerialStreamWriter | None = None
        self._read_task: asyncio.Task | None = None
        self._state = TVState()
        self._subscribers: list[StateCallback] = []
        self._pending: list[PendingCommand] = []
        self._write_lock = asyncio.Lock()
        self._connected = False
        self._batching = False
        self._batch_changed = False

    @property
    def state(self) -> TVState:
        """Return a snapshot of the current state."""
        return self._state.copy()

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def set_id(self) -> int:
        return self._set_id

    def subscribe(self, callback: StateCallback) -> Callable[[], None]:
        """Subscribe to state changes; returns an unsubscribe callable."""
        self._subscribers.append(callback)
        return lambda: self._subscribers.remove(callback)

    # -- Connection lifecycle ------------------------------------------------

    async def connect(self) -> None:
        """Open the serial connection and verify by querying power."""
        self._reader, self._writer = await serialx.open_serial_connection(
            self._port,
            baudrate=BAUD_RATE,
        )
        self._connected = True
        self._read_task = asyncio.create_task(self._read_loop())

        try:
            await self.query_power()
        except TimeoutError:
            await self.disconnect()
            raise TVNotRespondingError(
                f"No response from LG TV on {self._port}: the TV did not "
                "reply to a power query within "
                f"{COMMAND_TIMEOUT}s. Check that the TV is on the bus, the "
                "cable is wired correctly (TX/RX may be swapped), the set "
                "ID matches, and the baud rate is 9600."
            ) from None
        except CommandRejected as err:
            await self.disconnect()
            raise TVNotRespondingError(
                f"LG TV on {self._port} responded with NG to power query: {err}"
            ) from None

        _LOGGER.info("Connected to LG TV on %s", self._port)

    async def disconnect(self) -> None:
        """Close the serial connection."""
        await self._teardown()
        _LOGGER.info("Disconnected from LG TV")

    # -- Status queries (compound) ------------------------------------------

    async def query(self, attributes: Iterable[str]) -> None:
        """Query the given state attributes, notifying subscribers once.

        ``attributes`` is an iterable of :class:`TVState` field names (the
        members of :data:`QUERYABLE_ATTRIBUTES`); unknown names are ignored.
        Subscriber notifications are suppressed while the queries run and
        fired once at the end if any value changed.

        Queries are issued sequentially. An attribute the TV does not answer
        -- because it timed out or was rejected with NG -- is set to ``None``,
        so the state reflects the TV's latest answer rather than a stale
        value. Different LG models support different subsets of commands, and
        some attributes are only available depending on the TV's configuration
        (for example, volume-related commands when audio is routed to the TV
        speaker).
        """
        wanted = set(attributes)
        self._batching = True
        self._batch_changed = False
        try:
            for command1, command2, attr in _STATE_QUERIES:
                if attr not in wanted:
                    continue
                try:
                    await self._query(command1, command2)
                except (TimeoutError, CommandRejected) as err:
                    _LOGGER.debug(
                        "Clearing %s (%s%s) during query: %s",
                        attr,
                        command1,
                        command2,
                        err,
                    )
                    if self._set_state(attr, None):
                        self._batch_changed = True
        finally:
            self._batching = False

        if self._batch_changed:
            self._notify_subscribers()

    async def query_state(self) -> None:
        """Query every supported attribute and populate ``state``."""
        await self.query(QUERYABLE_ATTRIBUTES)

    # -- Power ---------------------------------------------------------------

    async def power_on(self) -> None:
        """Turn the TV on. Many LG TVs ignore this over RS232 when the set
        is in standby — use the IR remote / wake-on-LAN for power-on if so."""
        await self._send_set("k", "a", "01")

    async def power_off(self) -> None:
        """Turn the TV off."""
        await self._send_set("k", "a", "00")

    async def query_power(self) -> PowerState:
        """Query the TV's power state."""
        resp = await self._query("k", "a")
        return self._parse_power(resp.data)

    # -- Input ---------------------------------------------------------------

    async def select_input_source(self, source: InputSource) -> None:
        """Select an input source using the modern ``xb`` command."""
        await self._send_set("x", "b", source.value)

    async def query_input_source(self) -> InputSource:
        """Query the active input source (modern ``xb`` command)."""
        resp = await self._query("x", "b")
        return InputSource(resp.data.lower())

    async def select_legacy_input_source(self, source: LegacyInputSource) -> None:
        """Select an input source using the legacy ``kb`` command."""
        await self._send_set("k", "b", source.value)

    # -- Aspect / screen -----------------------------------------------------

    async def set_aspect_ratio(self, ratio: AspectRatio) -> None:
        await self._send_set("k", "c", ratio.value)

    async def query_aspect_ratio(self) -> AspectRatio:
        resp = await self._query("k", "c")
        return AspectRatio(resp.data.lower())

    async def set_screen_mute(self, mute: ScreenMute) -> None:
        await self._send_set("k", "d", mute.value)

    async def query_screen_mute(self) -> ScreenMute:
        resp = await self._query("k", "d")
        return ScreenMute(resp.data.lower())

    # -- Volume / mute -------------------------------------------------------

    async def mute_on(self) -> None:
        await self._send_set("k", "e", "00")

    async def mute_off(self) -> None:
        await self._send_set("k", "e", "01")

    async def query_mute(self) -> bool:
        """Query mute. Returns True when audio is muted."""
        resp = await self._query("k", "e")
        # 00 = mute on, 01 = mute off  (per spec)
        return resp.data == "00"

    async def set_volume(self, percent: int) -> None:
        """Set volume to a 0..100 percent."""
        await self._send_set("k", "f", percent_to_data(percent))

    async def query_volume(self) -> int:
        resp = await self._query("k", "f")
        return data_to_percent(resp.data)

    # -- Picture controls (all 0..100) --------------------------------------

    async def set_contrast(self, percent: int) -> None:
        await self._send_set("k", "g", percent_to_data(percent))

    async def query_contrast(self) -> int:
        return data_to_percent((await self._query("k", "g")).data)

    async def set_brightness(self, percent: int) -> None:
        await self._send_set("k", "h", percent_to_data(percent))

    async def query_brightness(self) -> int:
        return data_to_percent((await self._query("k", "h")).data)

    async def set_color(self, percent: int) -> None:
        await self._send_set("k", "i", percent_to_data(percent))

    async def query_color(self) -> int:
        return data_to_percent((await self._query("k", "i")).data)

    async def set_tint(self, percent: int) -> None:
        await self._send_set("k", "j", percent_to_data(percent))

    async def query_tint(self) -> int:
        return data_to_percent((await self._query("k", "j")).data)

    async def set_sharpness(self, percent: int) -> None:
        await self._send_set("k", "k", percent_to_data(percent))

    async def query_sharpness(self) -> int:
        return data_to_percent((await self._query("k", "k")).data)

    async def set_backlight(self, percent: int) -> None:
        await self._send_set("m", "g", percent_to_data(percent))

    async def query_backlight(self) -> int:
        return data_to_percent((await self._query("m", "g")).data)

    # -- Audio controls ------------------------------------------------------

    async def set_treble(self, percent: int) -> None:
        await self._send_set("k", "r", percent_to_data(percent))

    async def query_treble(self) -> int:
        return data_to_percent((await self._query("k", "r")).data)

    async def set_bass(self, percent: int) -> None:
        await self._send_set("k", "s", percent_to_data(percent))

    async def query_bass(self) -> int:
        return data_to_percent((await self._query("k", "s")).data)

    async def set_balance(self, percent: int) -> None:
        await self._send_set("k", "t", percent_to_data(percent))

    async def query_balance(self) -> int:
        return data_to_percent((await self._query("k", "t")).data)

    # -- Modes ---------------------------------------------------------------

    async def set_color_temperature(self, temp: ColorTemperature) -> None:
        await self._send_set("k", "u", temp.value)

    async def query_color_temperature(self) -> ColorTemperature:
        return ColorTemperature((await self._query("k", "u")).data.lower())

    async def set_energy_saving(self, level: EnergySaving) -> None:
        await self._send_set("j", "q", level.value)

    async def query_energy_saving(self) -> EnergySaving:
        return EnergySaving((await self._query("j", "q")).data.lower())

    async def set_picture_mode(self, mode: PictureMode) -> None:
        await self._send_set("d", "x", mode.value)

    async def query_picture_mode(self) -> PictureMode:
        return PictureMode((await self._query("d", "x")).data.lower())

    async def set_sound_mode(self, mode: SoundMode) -> None:
        await self._send_set("d", "y", mode.value)

    async def query_sound_mode(self) -> SoundMode:
        return SoundMode((await self._query("d", "y")).data.lower())

    # -- OSD / remote lock --------------------------------------------------

    async def osd_on(self) -> None:
        await self._send_set("k", "l", "01")

    async def osd_off(self) -> None:
        await self._send_set("k", "l", "00")

    async def query_osd(self) -> bool:
        resp = await self._query("k", "l")
        return resp.data == "01"

    async def remote_lock_on(self) -> None:
        await self._send_set("k", "m", "01")

    async def remote_lock_off(self) -> None:
        await self._send_set("k", "m", "00")

    async def query_remote_lock(self) -> bool:
        resp = await self._query("k", "m")
        return resp.data == "01"

    # -- Remote key (mc) ----------------------------------------------------

    async def send_remote_key(self, key: RemoteKey) -> None:
        """Send a single IR remote key code via the ``mc`` command."""
        await self._send_set("m", "c", key.value)

    async def send_remote_key_code(self, code: int) -> None:
        """Send an arbitrary 0..0xFF IR remote key code."""
        await self._send_set("m", "c", int_to_hex_byte(code))

    # -- Internals ----------------------------------------------------------

    async def _send_set(self, command1: str, command2: str, data: str) -> Response:
        """Send a 'set' command and wait for the ack (or raise CommandRejected)."""
        return await self._send_and_wait(command1, command2, data)

    async def _query(self, command1: str, command2: str) -> Response:
        """Send a 'ff' query and return the parsed response."""
        return await self._send_and_wait(command1, command2, _QUERY_DATA)

    async def _send_and_wait(
        self,
        command1: str,
        command2: str,
        data: str,
        timeout: float = COMMAND_TIMEOUT,
    ) -> Response:
        if self._writer is None:
            raise ConnectionError("Not connected")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Response] = loop.create_future()
        pending = PendingCommand(
            command2=command2,
            set_id=self._set_id,
            future=future,
        )
        self._pending.append(pending)
        try:
            msg = encode_command(command1, command2, self._set_id, data)
            _LOGGER.debug("Sending: %r", msg)
            try:
                async with self._write_lock:
                    self._writer.write(msg)
                    await self._writer.drain()
            except Exception:
                _LOGGER.exception("Error writing to serial port")
                await self._teardown()
                raise
            response = await asyncio.wait_for(future, timeout=timeout)
        finally:
            if pending in self._pending:
                self._pending.remove(pending)

        self._update_state_from_response(command1, command2, response)
        response.raise_for_status()
        return response

    async def _teardown(self) -> None:
        if not self._connected:
            return
        self._connected = False

        current = asyncio.current_task()
        if self._read_task is not None and self._read_task is not current:
            self._read_task.cancel()
            try:
                await self._read_task
            except asyncio.CancelledError:
                pass
        self._read_task = None

        if self._writer is not None:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:
                pass
            self._writer = None
            self._reader = None

        for pending in self._pending:
            if not pending.future.done():
                pending.future.set_exception(ConnectionError("Connection lost"))
        self._pending.clear()

        self._notify_subscribers()

    # Match a single complete response in the buffer:
    #   [c2] [setid] (OK|NG)[data]x
    # Anchored to end-of-pattern 'x'. The data can be 1+ hex chars; we use
    # a non-greedy match and require the terminating 'x' to be the literal
    # byte 'x' (0x78). Stray junk before the response (e.g. CR/LF) is
    # consumed by the leading skip in the loop below.
    _RESPONSE_PATTERN = re.compile(
        rb"([a-zA-Z])\s+([0-9a-fA-F]{1,2})\s+(OK|NG)([0-9a-fA-F]+)x"
    )

    async def _read_loop(self) -> None:
        assert self._reader is not None
        buf = b""
        while self._connected:
            try:
                data = await self._reader.read(256)
            except Exception:
                if not self._connected:
                    return
                _LOGGER.exception("Error reading from serial port")
                await self._teardown()
                return

            if not data:
                _LOGGER.warning("Serial connection closed")
                await self._teardown()
                return

            buf += data
            while True:
                match = self._RESPONSE_PATTERN.search(buf)
                if match is None:
                    # Trim noise from the front but keep tail in case the
                    # next read completes a response.
                    if ACK_TERMINATOR in buf:
                        # Discard everything up through the stray 'x'
                        buf = buf.split(ACK_TERMINATOR, 1)[1]
                    break
                message = match.group(0)[:-1].decode("ascii", errors="replace")
                buf = buf[match.end() :]
                self._handle_message(message)

    def _handle_message(self, message: str) -> None:
        _LOGGER.debug("Received: %r", message)
        try:
            response = parse_response(message)
        except Exception:
            _LOGGER.warning("Could not parse response: %r", message)
            return

        for pending in self._pending:
            if (
                pending.command2 == response.command2
                and pending.set_id == response.set_id
                and not pending.future.done()
            ):
                pending.future.set_result(response)
                return

        _LOGGER.debug("Unsolicited response: %r", message)

    # -- State updates ------------------------------------------------------

    def _set_state(self, attr: str, value: object) -> bool:
        if getattr(self._state, attr) == value:
            return False
        setattr(self._state, attr, value)
        return True

    def _update_state_from_response(
        self, command1: str, command2: str, response: Response
    ) -> None:
        """Update ``self._state`` from a successful response."""
        if not response.ok:
            return
        data = response.data.lower()
        changed = False

        try:
            if command1 == "k" and command2 == "a":
                changed = self._set_state("power", self._parse_power(data))
            elif command1 == "x" and command2 == "b":
                changed = self._set_state("input_source", InputSource(data))
            elif command1 == "k" and command2 == "c":
                changed = self._set_state("aspect_ratio", AspectRatio(data))
            elif command1 == "k" and command2 == "d":
                changed = self._set_state("screen_mute", ScreenMute(data))
            elif command1 == "k" and command2 == "e":
                changed = self._set_state("volume_mute", data == "00")
            elif command1 == "k" and command2 == "f":
                changed = self._set_state("volume", data_to_percent(data))
            elif command1 == "k" and command2 == "g":
                changed = self._set_state("contrast", data_to_percent(data))
            elif command1 == "k" and command2 == "h":
                changed = self._set_state("brightness", data_to_percent(data))
            elif command1 == "k" and command2 == "i":
                changed = self._set_state("color", data_to_percent(data))
            elif command1 == "k" and command2 == "j":
                changed = self._set_state("tint", data_to_percent(data))
            elif command1 == "k" and command2 == "k":
                changed = self._set_state("sharpness", data_to_percent(data))
            elif command1 == "k" and command2 == "l":
                changed = self._set_state("osd_enabled", data == "01")
            elif command1 == "k" and command2 == "m":
                changed = self._set_state("remote_lock", data == "01")
            elif command1 == "k" and command2 == "r":
                changed = self._set_state("treble", data_to_percent(data))
            elif command1 == "k" and command2 == "s":
                changed = self._set_state("bass", data_to_percent(data))
            elif command1 == "k" and command2 == "t":
                changed = self._set_state("balance", data_to_percent(data))
            elif command1 == "k" and command2 == "u":
                changed = self._set_state("color_temperature", ColorTemperature(data))
            elif command1 == "j" and command2 == "q":
                changed = self._set_state("energy_saving", EnergySaving(data))
            elif command1 == "m" and command2 == "g":
                changed = self._set_state("backlight", data_to_percent(data))
            elif command1 == "d" and command2 == "x":
                changed = self._set_state("picture_mode", PictureMode(data))
            elif command1 == "d" and command2 == "y":
                changed = self._set_state("sound_mode", SoundMode(data))
        except (ValueError, KeyError) as err:
            _LOGGER.debug(
                "Could not parse response data %r for %s%s: %s",
                data,
                command1,
                command2,
                err,
            )
            return

        if changed:
            if self._batching:
                self._batch_changed = True
            else:
                self._notify_subscribers()

    @staticmethod
    def _parse_power(data: str) -> PowerState:
        if data == "00":
            return PowerState.OFF
        if data == "01":
            return PowerState.ON
        raise ValueError(f"Unknown power data: {data!r}")

    def _notify_subscribers(self) -> None:
        snapshot = self._state.copy() if self._connected else None
        for callback in list(self._subscribers):
            try:
                callback(snapshot)
            except Exception:
                _LOGGER.exception("Error in state callback %s", callback)
