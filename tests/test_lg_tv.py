"""Integration-ish tests for the LGTV controller using the mock serial."""

from __future__ import annotations

from lg_rs232_tv import (
    AspectRatio,
    InputSource,
    PowerState,
    RemoteKey,
    ScreenMute,
)


async def test_connect_queries_power(tv) -> None:
    assert tv.connected
    assert tv.state.power is PowerState.ON


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
