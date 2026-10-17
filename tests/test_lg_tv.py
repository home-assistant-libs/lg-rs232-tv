"""Integration-ish tests for the LGTV controller using the mock serial."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest
import serialx
from conftest import DEFAULT_RESPONSES, MockSerialConnection

from lg_rs232_tv import (
    AspectRatio,
    InputSource,
    LGTV,
    LGTVConnectionError,
    PowerState,
    RemoteKey,
    ScreenMute,
    TVNotRespondingError,
)


async def test_connect_queries_power(tv) -> None:
    assert tv.connected
    assert tv.state.power is PowerState.ON


async def test_connect_raises_when_tv_does_not_respond(mock_serial) -> None:
    """connect() raises TVNotRespondingError when no LG TV answers the bus."""
    # The port opens, but the power query is rejected (no working LG TV).
    mock_serial.responses["ka 01 ff"] = "NG:ff"
    tv = LGTV("/dev/ttyUSB0")

    async def fake_open(*args, **kwargs):
        return mock_serial.reader, mock_serial.writer

    with patch(
        "lg_rs232_tv.tv.serialx.open_serial_connection", side_effect=fake_open
    ):
        with pytest.raises(TVNotRespondingError):
            await tv.connect()


async def test_connect_does_not_log_api_key(
    mock_serial, caplog: pytest.LogCaptureFixture
) -> None:
    """The API key in an ESPHome proxy URL stays out of logs and errors."""
    mock_serial.responses["ka 01 ff"] = "01"
    tv = LGTV("esphome://192.168.1.2/?port_name=TTL&key=SECRET")

    async def fake_open(*args, **kwargs):
        return mock_serial.reader, mock_serial.writer

    caplog.set_level("INFO")
    with patch("lg_rs232_tv.tv.serialx.open_serial_connection", side_effect=fake_open):
        await tv.connect()
        await tv.disconnect()

        mock_serial.responses["ka 01 ff"] = "NG:ff"
        with pytest.raises(TVNotRespondingError) as exc_info:
            await tv.connect()

    assert "esphome://192.168.1.2/" in caplog.text
    assert "SECRET" not in caplog.text
    assert "SECRET" not in str(exc_info.value)


async def test_query_state_populates_everything(tv) -> None:
    await tv.query_state()
    s = tv.state
    assert s.power is PowerState.ON
    assert s.input_source is InputSource.HDMI1
    assert s.aspect_ratio is AspectRatio.R_16_9
    assert s.volume == 30
    assert s.contrast == 50
    assert s.brightness == 50
    assert s.osd_enabled is True
    assert s.remote_lock is False


async def test_picture_mode_reply_split_across_reads(tv, mock_serial) -> None:
    """A dx reply starts with 'x', the terminator, and may arrive in parts."""
    mock_serial.responses.pop("dx 01 ff")

    def handler(_cmd: str) -> None:
        mock_serial.feed("x 01 OK0")
        asyncio.get_running_loop().call_later(0.01, mock_serial.feed, "1x")

    mock_serial.command_handler = handler
    assert (await tv.query_picture_mode()).value == "01"


async def test_read_buffer_is_bounded(tv, mock_serial) -> None:
    """Noise without an 'x' must not grow the read buffer without limit."""
    mock_serial.feed("0" * 10_000)
    await asyncio.sleep(0)

    # The buffer is local to the read loop.
    assert len(tv._read_task.get_coro().cr_frame.f_locals["buf"]) <= 64
    await tv.query_power()


async def test_set_volume_sends_correct_command(tv, mock_serial) -> None:
    mock_serial.responses["kf 01 19"] = "19"
    await tv.set_volume(25)
    assert b"kf 01 19\r" in mock_serial.written
    assert tv.state.volume == 25


async def test_select_input_source(tv, mock_serial) -> None:
    mock_serial.responses["xb 01 91"] = "91"
    await tv.select_input_source(InputSource.HDMI2)
    assert b"xb 01 91\r" in mock_serial.written
    assert tv.state.input_source is InputSource.HDMI2


async def test_set_aspect_ratio(tv, mock_serial) -> None:
    mock_serial.responses["kc 01 09"] = "09"
    await tv.set_aspect_ratio(AspectRatio.JUST_SCAN)
    assert tv.state.aspect_ratio is AspectRatio.JUST_SCAN


async def test_screen_mute(tv, mock_serial) -> None:
    mock_serial.responses["kd 01 01"] = "01"
    await tv.set_screen_mute(ScreenMute.SCREEN_ON)
    assert tv.state.screen_mute is ScreenMute.SCREEN_ON


async def test_remote_key(tv, mock_serial) -> None:
    mock_serial.responses["mc 01 43"] = "43"
    await tv.send_remote_key(RemoteKey.MENU)
    assert b"mc 01 43\r" in mock_serial.written


async def test_subscribe(tv, mock_serial) -> None:
    received: list = []
    unsub = tv.subscribe(received.append)
    mock_serial.responses["kf 01 32"] = "32"
    await tv.set_volume(50)
    assert received  # got at least one notification
    assert received[-1].volume == 50
    unsub()


async def test_connect_with_set_id_above_9(mock_serial) -> None:
    """The set ID is sent in hex and matched against the hex reply."""
    mock_serial.responses["ka 0a ff"] = "01"
    tv = LGTV("/dev/ttyUSB0", set_id=10)

    async def fake_open(*args, **kwargs):
        return mock_serial.reader, mock_serial.writer

    with patch("lg_rs232_tv.tv.serialx.open_serial_connection", side_effect=fake_open):
        await tv.connect()

    assert tv.state.power is PowerState.ON
    await tv.disconnect()


async def test_query_power_off(tv, mock_serial) -> None:
    mock_serial.responses["ka 01 ff"] = "00"
    power = await tv.query_power()
    assert power is PowerState.OFF
    assert tv.state.power is PowerState.OFF


async def test_query_only_queries_requested_attributes(tv, mock_serial) -> None:
    mock_serial.written.clear()
    await tv.query(["volume", "input_source"])
    sent = [w.decode("ascii").rstrip("\r") for w in mock_serial.written]
    assert "kf 01 ff" in sent  # volume queried
    assert "xb 01 ff" in sent  # input source queried
    assert "kc 01 ff" not in sent  # aspect ratio not requested
    assert tv.state.volume == 30


async def test_late_reply_not_credited_to_next_command(tv, mock_serial) -> None:
    """Contrast (kg) and backlight (mg) replies both start with 'g'."""
    mock_serial.responses.pop("kg 01 ff")
    mock_serial.responses.pop("mg 01 ff")
    loop = asyncio.get_running_loop()

    def handler(cmd: str) -> None:
        if cmd == "kg 01 ff":
            # Contrast 10, but only after the query timed out.
            loop.call_later(0.15, mock_serial.feed, "g 01 OK0ax")
        else:
            # Backlight 80.
            loop.call_later(0.08, mock_serial.feed, "g 01 OK50x")

    mock_serial.command_handler = handler

    with pytest.raises(TimeoutError):
        await tv.query_contrast()
    assert await tv.query_backlight() == 80
    assert tv.state.backlight == 80


async def test_query_notifies_subscribers_once(tv, mock_serial) -> None:
    received: list = []
    tv.subscribe(received.append)
    await tv.query(["volume", "input_source", "aspect_ratio"])
    assert len(received) == 1  # one batched notification, not one per attribute
    await tv.query(["volume", "input_source", "aspect_ratio"])
    assert len(received) == 1  # nothing changed, so no extra notification


async def test_query_clears_unanswered_attributes(tv, mock_serial) -> None:
    await tv.query(["volume", "balance"])
    assert tv.state.volume == 30
    assert tv.state.balance == 50

    # The TV stops answering the balance query (e.g. audio routed to optical).
    mock_serial.responses["kt 01 ff"] = "NG:ff"
    received: list = []
    tv.subscribe(received.append)
    await tv.query(["volume", "balance"])

    assert tv.state.volume == 30  # still answered, unchanged
    assert tv.state.balance is None  # stale value cleared
    assert received[-1].balance is None


async def _connect(tv: LGTV, mock_serial: MockSerialConnection) -> None:
    async def fake_open(*args, **kwargs):
        return mock_serial.reader, mock_serial.writer

    with patch("lg_rs232_tv.tv.serialx.open_serial_connection", side_effect=fake_open):
        await tv.connect()


def _break_transport(mock_serial: MockSerialConnection) -> OSError:
    """Make the mock transport behave like a serial link that has gone away.

    Writes fail, and so does ``wait_closed()``: serialx re-raises the error
    that broke the connection when the stream is closed afterwards.
    """
    err = OSError(5, "ESPHome API connection closed")
    mock_serial.writer.write.side_effect = err
    mock_serial.writer.wait_closed.side_effect = err
    return err


async def test_write_error_tears_down(tv, mock_serial) -> None:
    received: list = []
    tv.subscribe(received.append)
    err = _break_transport(mock_serial)

    with pytest.raises(LGTVConnectionError) as exc_info:
        await tv.power_on()
    assert exc_info.value.__cause__ is err

    assert not tv.connected
    assert received == [None]

    # Later commands fail fast instead of writing to the dead stream again.
    mock_serial.writer.write.reset_mock()
    with pytest.raises(LGTVConnectionError, match="Not connected"):
        await tv.power_on()
    mock_serial.writer.write.assert_not_called()


async def test_link_loss_fails_command_in_flight(tv, mock_serial) -> None:
    mock_serial.responses.pop("kf 01 ff")
    mock_serial.command_handler = lambda _cmd: mock_serial.reader.feed_eof()

    with pytest.raises(LGTVConnectionError, match="Connection lost"):
        await tv.query(["volume"])
    assert not tv.connected


async def test_reconnect_after_link_loss(tv, mock_serial) -> None:
    _break_transport(mock_serial)
    with pytest.raises(LGTVConnectionError):
        await tv.power_on()

    fresh = MockSerialConnection()
    fresh.responses = dict(DEFAULT_RESPONSES)
    await _connect(tv, fresh)

    assert tv.connected
    fresh.responses["ka 01 00"] = "00"
    await tv.power_off()
    assert tv.state.power is PowerState.OFF


async def test_connect_when_connected_raises(tv, mock_serial) -> None:
    """A second connect() must not replace and leak the open connection."""
    with (
        patch("lg_rs232_tv.tv.serialx.open_serial_connection") as mock_open,
        pytest.raises(RuntimeError, match="Already connected"),
    ):
        await tv.connect()

    mock_open.assert_not_called()
    mock_serial.writer.close.assert_not_called()
    assert tv.connected
    await tv.query_power()


async def test_connect_cleans_up_on_unexpected_error(mock_serial) -> None:
    mock_serial.responses["ka 01 ff"] = "05"  # not a known power state
    tv = LGTV("/dev/ttyUSB0")

    with pytest.raises(ValueError):
        await _connect(tv, mock_serial)

    assert not tv.connected
    mock_serial.writer.close.assert_called_once()


async def test_connect_cleans_up_on_cancel(mock_serial) -> None:
    tv = LGTV("/dev/ttyUSB0")
    task = asyncio.create_task(_connect(tv, mock_serial))
    while not mock_serial.written:
        await asyncio.sleep(0)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not tv.connected
    mock_serial.writer.close.assert_called_once()


async def test_read_loop_crash_tears_down(
    tv, mock_serial, caplog: pytest.LogCaptureFixture
) -> None:
    """An unexpected error in the read loop disconnects instead of hanging."""
    disconnected = asyncio.Event()
    tv.subscribe(lambda state: state is None and disconnected.set())

    with patch.object(tv, "_handle_message", side_effect=RuntimeError("boom")):
        mock_serial.feed("a 01 OK01x")
        await disconnected.wait()

    assert not tv.connected
    assert "Read loop failed" in caplog.text
    mock_serial.writer.close.assert_called_once()


async def test_unsubscribe_twice_is_noop(tv, mock_serial) -> None:
    received: list = []
    unsubscribe = tv.subscribe(received.append)

    unsubscribe()
    unsubscribe()

    mock_serial.responses["ka 01 00"] = "00"
    await tv.power_off()
    assert received == []


async def test_connect_serial_exception_allows_retry(mock_serial) -> None:
    """An unavailable serial port is a connection failure and can be retried."""
    device = LGTV("esphome://proxy.local/?port_name=RS232")
    error = serialx.SerialException("Serial proxy is unavailable")
    mock_serial.responses["ka 01 ff"] = "01"

    with patch(
        "lg_rs232_tv.tv.serialx.open_serial_connection",
        side_effect=[error, (mock_serial.reader, mock_serial.writer)],
    ) as open_connection:
        with pytest.raises(
            LGTVConnectionError, match="Serial proxy is unavailable"
        ) as exc_info:
            await device.connect()

        assert exc_info.value.__cause__ is error
        assert not device.connected
        mock_serial.writer.write.assert_not_called()

        try:
            await device.connect()
            assert device.connected
            assert open_connection.await_count == 2
        finally:
            await device.disconnect()
