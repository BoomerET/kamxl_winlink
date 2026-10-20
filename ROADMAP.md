# Roadmap

What's open, roughly in order. For how the project got here and why
things work the way they do, see the [design history](docs/history.md).
Ideas and problem reports are welcome as
[issues](https://github.com/BoomerET/tncstation/issues).

## Next

- **A registered APRS identifier.** Transmissions use `APZTNC`, from
  the range [aprs-deviceid](https://github.com/aprsorg/aprs-deviceid)
  sets aside for software in development. `APTNC?` was requested in
  [aprs-deviceid#388](https://github.com/aprsorg/aprs-deviceid/issues/388)
  (October 2026); once assigned, it becomes the default.
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

## Done recently

- **Recovery without a hand on the keyboard** (October 2026): unplugging
  the OpenTNC (back on any USB port) and rebooting the Pi both bring the
  station back by itself -- see "What happens if..." in
  [docs/opentnc.md](docs/opentnc.md). Restarting Direwolf is the same
  path (the daemon waits for its KISS port) but untested on the air.

## Ideas

Not planned yet, but in the spirit of the project:

- More KISS TNCs verified (Mobilinkd, NinoTNC, others in KISS mode).
- An asyncio interface to the library.
- Multiple connected-mode streams as objects.
