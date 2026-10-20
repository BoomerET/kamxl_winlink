# Roadmap

What's open, roughly in order. For how the project got here and why
things work the way they do, see the [design history](docs/history.md).
Ideas and problem reports are welcome as
[issues](https://github.com/BoomerET/tncstation/issues).

## Next

- **Recovery without a hand on the keyboard.** Unplugging and
  replugging the OpenTNC, rebooting the Pi, restarting Direwolf: each
  should bring the station back by itself -- KISS mode re-entered, the
  map refilled, auto-ACK still on. To be checked on the real stations
  and fixed where it doesn't.
- **A registered APRS identifier.** Transmissions use `APZTNC`, from
  the range [aprs-deviceid](https://github.com/aprsorg/aprs-deviceid)
  sets aside for software in development. Request a permanent one (an
  `APTNC?`-style destination) and make it the default.
- **PyPI.** Publish so installing is `pip install tncstation`; the
  package metadata is already in `pyproject.toml`.

## OpenTNC

- **Terminal Mode connected mode:** record the connect and disconnect
  banners (`tools/opentnc_capture.py --connect`), then the web terminal
  and connected mode can be marked verified.
- **Winlink over the OpenTNC's connected mode,** once the above works.
- **Terminal Mode APRS transmit:** the safety checks read the KAM-XL's
  `STATUS`, which this firmware doesn't have, so it needs its own
  checks. KISS mode transmits today.

## KAM-XL

- **KISS-mode transmit:** confirm the KISS channel to radio port mapping
  on the air (channel 1 = VHF Port 2, per the manual), then enable it.
  Terminal Mode transmits normally, so this only matters for running
  the KAM-XL in KISS.

## Ideas

Not planned yet, but in the spirit of the project:

- More KISS TNCs verified (Mobilinkd, NinoTNC, others in KISS mode).
- An asyncio interface to the library.
- Multiple connected-mode streams as objects.
