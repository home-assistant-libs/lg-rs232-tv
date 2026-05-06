# lg-tv-rs232

Async Python library to control LG TVs over RS232 serial, built on
[serialx](https://github.com/puddly/serialx).

## Installation

```bash
pip install lg-tv-rs232

# To talk to a TV over an ESPHome serial proxy:
pip install 'lg-tv-rs232[esphome]'
```

Requires Python 3.12+.

## Quick start

```python
import asyncio
from lg_tv_rs232 import LGTV, InputSource

async def main():
    tv = LGTV("/dev/ttyUSB0")
    await tv.connect()
    await tv.query_state()

    print(f"Power:  {tv.state.power}")
    print(f"Input:  {tv.state.input_source}")
    print(f"Volume: {tv.state.volume}%")

    await tv.set_volume(20)
    await tv.select_input_source(InputSource.HDMI1)

    await tv.disconnect()

asyncio.run(main())
```

## CLI

A built-in CLI lets you quickly test your serial connection:

```bash
# Query and print TV status
python -m lg_tv_rs232 /dev/ttyUSB0

# Talk to a TV via an ESPHome serial proxy ("TTL" port)
python -m lg_tv_rs232 'esphome://192.168.1.29/?port_name=TTL'

# Talk to a TV over a raw TCP socket (e.g. ser2net)
python -m lg_tv_rs232 socket://192.168.1.29:5000

# Single-shot actions
python -m lg_tv_rs232 /dev/ttyUSB0 --power on
python -m lg_tv_rs232 /dev/ttyUSB0 --power off
python -m lg_tv_rs232 /dev/ttyUSB0 --input HDMI2
python -m lg_tv_rs232 /dev/ttyUSB0 --volume 30
python -m lg_tv_rs232 /dev/ttyUSB0 --mute on
python -m lg_tv_rs232 /dev/ttyUSB0 --aspect R_16_9
python -m lg_tv_rs232 /dev/ttyUSB0 --key MENU

# Use a non-default set ID (when daisy-chaining multiple sets)
python -m lg_tv_rs232 /dev/ttyUSB0 --set-id 2
```

## Features

### Full state after query

`connect()` only opens and verifies the serial connection (by querying
power). Call `query_state()` to populate the current TV state into
`tv.state`.

```python
tv = LGTV("/dev/ttyUSB0")
await tv.connect()
await tv.query_state()

state = tv.state
state.power           # PowerState.ON / PowerState.OFF
state.input_source    # InputSource enum
state.aspect_ratio    # AspectRatio enum
state.volume          # 0..100 percent
state.volume_mute     # bool
state.picture_mode    # PictureMode enum
state.color_temperature  # ColorTemperature enum
# ...etc
```

> **Note:** Most LG TVs only respond to status queries (other than power)
> when the set is **on**. While in standby, only `ka` (power) is answered.

### Event subscription

Subscribe to state changes to react in real-time. Callbacks receive a
`TVState` snapshot, or `None` when the connection is lost.

```python
def on_state_change(state):
    if state is None:
        print("Disconnected!")
        return
    print(f"Volume: {state.volume}%, Source: {state.input_source}")

unsub = tv.subscribe(on_state_change)
# Later:
unsub()
```

### Power

```python
await tv.power_on()    # often ignored when in standby; use IR/WoL instead
await tv.power_off()
power = await tv.query_power()  # PowerState.ON / PowerState.OFF
```

### Input source

```python
from lg_tv_rs232 import InputSource, LegacyInputSource

# Modern xb command (~2010+)
await tv.select_input_source(InputSource.HDMI1)
source = await tv.query_input_source()  # InputSource enum

# Legacy kb command (older sets)
await tv.select_legacy_input_source(LegacyInputSource.HDMI1)
```

Available modern sources: `DTV_ANTENNA`, `DTV_CABLE`, `ANALOG_ANTENNA`,
`ANALOG_CABLE`, `AV1`, `AV2`, `COMPONENT1`-`3`, `RGB_PC`, `HDMI1`-`4`.

### Volume / mute

```python
await tv.set_volume(30)       # 0..100
await tv.mute_on()
await tv.mute_off()
volume = await tv.query_volume()  # int 0..100
muted = await tv.query_mute()     # True if muted
```

### Picture controls

All on a 0..100 scale.

```python
await tv.set_contrast(70)
await tv.set_brightness(50)
await tv.set_color(50)
await tv.set_tint(50)
await tv.set_sharpness(50)
await tv.set_backlight(80)
```

### Audio controls

```python
await tv.set_treble(50)
await tv.set_bass(50)
await tv.set_balance(50)
```

### Modes

```python
from lg_tv_rs232 import (
    AspectRatio, ColorTemperature, EnergySaving, PictureMode, SoundMode
)

await tv.set_aspect_ratio(AspectRatio.R_16_9)
await tv.set_color_temperature(ColorTemperature.WARM)
await tv.set_energy_saving(EnergySaving.MEDIUM)
await tv.set_picture_mode(PictureMode.CINEMA)
await tv.set_sound_mode(SoundMode.MUSIC)
```

### Screen mute / OSD / remote lock

```python
from lg_tv_rs232 import ScreenMute

await tv.set_screen_mute(ScreenMute.SCREEN_ON)  # picture off, audio on
await tv.set_screen_mute(ScreenMute.OFF)        # back to normal

await tv.osd_on()
await tv.osd_off()

await tv.remote_lock_on()
await tv.remote_lock_off()
```

### Remote control keys

Send any IR remote key code over RS232 with the `mc` command:

```python
from lg_tv_rs232 import RemoteKey

await tv.send_remote_key(RemoteKey.MENU)
await tv.send_remote_key(RemoteKey.HOME)
await tv.send_remote_key(RemoteKey.PLAY)
await tv.send_remote_key_code(0x08)   # arbitrary hex code
```

### Connection handling

- If the TV doesn't respond during `connect()`, a `ConnectionError` is raised.
- If the serial connection is lost, subscribers receive `None` and `connected` becomes `False`.
- Commands return a `Response`; an NG (not-good) acknowledgement raises `CommandRejected`.

```python
from lg_tv_rs232 import CommandRejected

try:
    await tv.connect()
except ConnectionError:
    print("TV not responding")

try:
    await tv.set_volume(50)
except CommandRejected as err:
    print(f"TV rejected command: {err}")
```

## Multiple sets / set ID

When multiple TVs are daisy-chained on the same RS232 bus, each set is
addressed by its set ID (1..99). Pass `set_id=` at construction time:

```python
tv1 = LGTV("/dev/ttyUSB0", set_id=1)
tv2 = LGTV("/dev/ttyUSB0", set_id=2)
```

## Serial connection

The library uses [serialx](https://github.com/puddly/serialx). LG TVs use
**9600 baud, 8 data bits, no parity, 1 stop bit**.

Most LG TVs use a DE-9 male connector (requires a null-modem cable).
Some sets expose RS232 on a 3.5mm phone jack instead. The library accepts
any serialx-compatible URL:

| URL form                                           | Use case                            |
| -------------------------------------------------- | ----------------------------------- |
| `/dev/ttyUSB0`                                     | local USB-serial adapter            |
| `socket://host:port`                               | raw TCP serial bridge (ser2net)     |
| `esphome://host/?port_name=TTL`                    | ESPHome serial proxy component      |
| `esphome://host/?port_name=RS-232`                 | ESPHome serial proxy component      |

## Protocol

LG TVs use a simple ASCII request/response protocol:

```
Transmission:    [Command1][Command2] [SetID] [Data]<CR>
                 e.g. "ka 01 ff\r"  (query power on set 1)

Response:        [Command2] [SetID] (OK|NG)[Data]x
                 e.g. "a 01 OK01x"  (set 1 acks: power = on)
```

Note that responses are terminated by the literal ASCII character `x`,
**not** by a carriage return.

`FF` data sent to a setter command means "query current value". The
acknowledgement contains the current value as the data byte. The library
exposes both a `set_*` and a `query_*` method for each attribute.

## Development

```bash
# Install dev dependencies
uv sync

# Run tests
uv run pytest
```

## License

MIT
