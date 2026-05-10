# lg-rs232-tv

Async Python library to control LG TVs over RS232 serial.

## Project structure

```
src/lg_rs232_tv/
  __init__.py    -- Re-exports public API
  const.py       -- BAUD_RATE, COMMAND_TIMEOUT, enums (InputSource, AspectRatio, ...)
  protocol.py    -- encode_command, parse_response, percent <-> hex helpers
  state.py       -- TVState dataclass
  tv.py          -- LGTV controller (connect / query / set / subscribe)
  __main__.py    -- CLI: python -m lg_rs232_tv PORT [--power on|off|...] [--diagnose]

tests/
  conftest.py        -- MockSerialConnection fixture, DEFAULT_RESPONSES
  test_protocol.py   -- encode/parse/percent helpers
  test_lg_tv.py      -- LGTV behaviour against mock serial
```

## Architecture

- Built on `serialx` (`open_serial_connection`); supports any serialx URL —
  `/dev/ttyUSB0`, `socket://host:port`, `esphome://host/?port_name=TTL`, etc.
- All LG TVs use **9600 baud, 8N1**.
- LG ASCII protocol:
  - Send: `[c1][c2] [setid] [data]<CR>` e.g. `ka 01 ff\r`
  - Recv: `[c2] [setid] (OK|NG)[data]x` e.g. `a 01 OK01x`
- The response terminator is the literal character **`x`** (0x78), not CR.
  This is awkward because some commands (e.g. `dx` picture mode) have c2='x'
  themselves — the read loop uses a regex to find complete responses rather
  than splitting on 'x'.
- `connect()` opens the port and verifies with a `ka 01 ff` (power query).
- `query_state()` walks every supported attribute. Each query is sequential.
- LG TVs do NOT emit unsolicited events; everything is request/response.
- State is updated automatically on the response of every query AND every
  set command (since sets also produce `OK<data>x` acks).

## Key design decisions

- `set_id` is per-`LGTV` instance (1..99). Set ID 0 is broadcast.
- Each command method has matching `set_*` and `query_*` (where queryable).
- `set_*` methods raise `CommandRejected` on NG; OK responses also update state.
- `subscribe(callback)` notifies on every state change and `None` on disconnect.
- The CLI's `--diagnose` mode opens the port raw (no `connect()` verify) and
  broadcasts power queries to set IDs 0..99 — useful when the cable wiring or
  the TV's set ID is unknown.
- `dx` (picture mode) and `dy` (sound mode) commands are included even though
  their c2 collides with the terminator — the regex-based parser handles them.

## Testing

- `pytest` with `pytest-asyncio`, `asyncio_mode = "auto"`.
- `MockSerialConnection` uses a real `asyncio.StreamReader` plus a mocked
  writer. `_on_write` synchronously feeds the configured ack into the reader.
- Run: `uv run pytest`

## Protocol reference

PDF analysed: https://www.proaudioinc.com/Dealer_Area/RS232.pdf
(LG OWNER'S MANUAL - EXTERNAL CONTROL DEVICE SETUP, EM9600/LM/LS series).
The LG help page at lg.com/ca_en/support links to it but does not include the
command list inline.
