# Examples

Told apart by filename prefix:

- **`offline_*.py`** -- run with no hardware at all. `offline_typed_commands.py`
  uses the same scripted fake serial connection as the offline unit test
  suite (`tests/fakes.py`); `offline_packet_parsing.py` needs nothing but
  `packet.py` and some canned text. Good for getting a feel for the API
  before you have a KAM-XL wired up, or for CI.
- **`hardware_*.py`** -- need a real Kantronics KAM-XL on a serial port.
  Edit the `PORT`/callsign constants at the top of each one before
  running. These follow the same pattern as the `*Test.py` scripts in
  [tools/](../tools/), just trimmed down and commented for reading
  rather than testing.
- **`winlink_api_check.py`** -- a third category, fitting neither
  bucket above: no KAM-XL involved, but it does need real network
  access and a real Winlink API key (`$WINLINK_API_KEY` or a `.env`
  file, same as `tncstation_daemon.py` -- see
  [docs/daemon.md](../docs/daemon.md#winlink-api-key)). Makes exactly
  one cheap `account_exists()` call to confirm a key actually works --
  see the script's own docstring for why it deliberately doesn't also
  exercise `get_gateway_status()`/`nearby_gateways()` by default.

Run any of them directly (from the repo root, or after `pip install -e .`):

```
python3 examples/offline_packet_parsing.py
python3 examples/offline_typed_commands.py
python3 examples/hardware_basic_terminal.py     # needs real hardware
python3 examples/hardware_connect_and_chat.py   # needs real hardware
python3 examples/hardware_monitor_packets.py    # needs real hardware
python3 examples/winlink_api_check.py [CALLSIGN]  # needs a real WINLINK_API_KEY
```

See [`docs/quickstart.md`](../docs/quickstart.md) for the same material
as a walkthrough instead of standalone scripts.
