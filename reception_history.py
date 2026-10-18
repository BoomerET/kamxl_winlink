"""Persistent SQLite storage for received AX.25 packet metadata."""

import json
import sqlite3
import time
from pathlib import Path
from typing import Optional

from packet import Packet
from aprs import bulletin_kind, parse_aprs_message, unwrap_third_party


class ReceptionHistory:
    """Store received packet metadata across daemon restarts."""

    def __init__(self, database_path: str) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)

        self._connection = sqlite3.connect(
            str(self.database_path),
            timeout=5.0,
        )

        self._connection.execute("PRAGMA busy_timeout = 5000")
        self._connection.execute("PRAGMA journal_mode = WAL")

        self._connection.executescript("""
            CREATE TABLE IF NOT EXISTS received_packets (
                id INTEGER PRIMARY KEY,
                received_at INTEGER NOT NULL,
                source TEXT NOT NULL,
                destination TEXT,
                frame_type TEXT,
                port INTEGER,
                digipeaters TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_packets_received_at
                ON received_packets(received_at);

            CREATE INDEX IF NOT EXISTS idx_packets_source_time
                ON received_packets(source, received_at);

            CREATE TABLE IF NOT EXISTS aprs_messages (
                id INTEGER PRIMARY KEY,
                received_at INTEGER NOT NULL,
                source TEXT NOT NULL,
                addressee TEXT NOT NULL,
                message_text TEXT NOT NULL,
                message_id TEXT,
                kind TEXT NOT NULL,
                port INTEGER NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_aprs_messages_received_at
                ON aprs_messages(received_at);

            CREATE INDEX IF NOT EXISTS idx_aprs_messages_addressee_time
                ON aprs_messages(addressee, received_at);

            -- Messages this station sent, with ack/resend state, so the
            -- dashboard's conversations survive a daemon restart. One
            -- row per message ID (updated in place); the record itself
            -- is stored as JSON so new fields don't need migrations.
            CREATE TABLE IF NOT EXISTS aprs_outbound (
                message_id TEXT PRIMARY KEY,
                sent_at REAL NOT NULL,
                record TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_aprs_outbound_sent_at
                ON aprs_outbound(sent_at);

            -- Small persistent settings (e.g. the current station setup).
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );

            -- Coverage: every transmission we made (beacon, message,
            -- resend) with the station setup in use at the time ...
            CREATE TABLE IF NOT EXISTS own_tx (
                id INTEGER PRIMARY KEY,
                sent_at REAL NOT NULL,
                kind TEXT NOT NULL,
                ref TEXT NOT NULL,
                path TEXT,
                setup TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_own_tx_sent_at
                ON own_tx(sent_at);

            -- ... and each time one came back: repeated by a
            -- digipeater (via='digi', station = the first digipeater,
            -- the one that heard us; '' if unidentified) or sent back
            -- from APRS-IS by an iGate (via='igate').
            CREATE TABLE IF NOT EXISTS own_heard (
                id INTEGER PRIMARY KEY,
                tx_id INTEGER NOT NULL,
                heard_at REAL NOT NULL,
                via TEXT NOT NULL,
                station TEXT NOT NULL,
                UNIQUE (tx_id, via, station)
            );
        """)

        # Added later: the packet's info text, so the map can be
        # rebuilt after a restart. Older rows have NULL here.
        columns = {
            row[1] for row in self._connection.execute(
                "PRAGMA table_info(received_packets)"
            )
        }
        # Also added later: the station setup in use when it was heard.
        for column in ("payload", "setup"):
            if column not in columns:
                self._connection.execute(
                    f"ALTER TABLE received_packets ADD COLUMN {column} TEXT"
                )

        self._connection.commit()

    def record(
        self,
        packet: Packet,
        received_at: Optional[int] = None,
        setup: Optional[str] = None,
    ) -> None:
        """Persist metadata for one successfully parsed packet."""

        timestamp = (
            int(time.time())
            if received_at is None
            else int(received_at)
        )

        # Messages gated from APRS-IS arrive wrapped in a third-party
        # header; store the real originator, not the iGate.
        message_source, message_payload = unwrap_third_party(
            packet.source, packet.payload
        )

        aprs_message = (
            parse_aprs_message(message_payload)
            if packet.port == 2
            and packet.frame_type in ("UI", None)
            else None
        )

        with self._connection:
            self._connection.execute(
                """
                INSERT INTO received_packets (
                    received_at,
                    source,
                    destination,
                    frame_type,
                    port,
                    digipeaters,
                    payload,
                    setup
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    timestamp,
                    packet.source,
                    packet.destination,
                    packet.frame_type,
                    packet.port,
                    json.dumps(packet.digipeaters),
                    packet.payload,
                    setup,
                ),
            )
            if aprs_message is not None:
                self._connection.execute(
                    """
                    INSERT INTO aprs_messages (
                        received_at,
                        source,
                        addressee,
                        message_text,
                        message_id,
                        kind,
                        port
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        timestamp,
                        message_source,
                        aprs_message.addressee,
                        aprs_message.text,
                        aprs_message.message_id,
                        aprs_message.kind,
                        packet.port,
                    ),
                )

    def prune_packets(self, older_than: int, batch_size: int = 5000) -> int:
        """
        Delete received_packets rows received before ``older_than``
        (Unix seconds). Works in small batches so the monitor's own
        inserts never wait long on the database lock. Received and
        sent *messages* are left alone (the dashboard has Clear for
        those). Returns the number of rows deleted.
        """
        deleted = 0

        while True:
            with self._connection:
                cursor = self._connection.execute(
                    """
                    DELETE FROM received_packets
                    WHERE id IN (
                        SELECT id FROM received_packets
                        WHERE received_at < ?
                        LIMIT ?
                    )
                    """,
                    (int(older_than), int(batch_size)),
                )

            deleted += cursor.rowcount

            if cursor.rowcount < batch_size:
                return deleted

    def packets_since(self, since: int, limit: int = 20000):
        """
        Packets received at or after ``since`` (Unix seconds) that have
        their payload stored, oldest first, as (Packet, received_at)
        pairs -- for replaying into the station tracker at startup.
        ``raw`` isn't stored, so it comes back empty. If there are more
        than ``limit``, the newest ``limit`` are returned.
        """
        rows = self._connection.execute(
            """
            SELECT received_at, source, destination, frame_type, port,
                   digipeaters, payload
            FROM (
                SELECT * FROM received_packets
                WHERE received_at >= ? AND payload IS NOT NULL
                ORDER BY received_at DESC, id DESC
                LIMIT ?
            )
            ORDER BY received_at, id
            """,
            (int(since), int(limit)),
        ).fetchall()

        packets = []
        for received_at, source, dest, frame_type, port, digis, payload in rows:
            try:
                digipeaters = tuple(json.loads(digis or "[]"))
            except ValueError:
                digipeaters = ()
            packets.append((
                Packet(
                    source=source,
                    destination=dest or "",
                    digipeaters=digipeaters,
                    port=int(port) if port is not None else 0,
                    payload=payload,
                    raw="",
                    frame_type=frame_type,
                ),
                int(received_at),
            ))
        return packets

    # -- packet log search ----------------------------------------------

    @staticmethod
    def _callsign_clause(column: str, callsign: str):
        """
        SQL matching ``column`` against a callsign search term:
        "W5NGU-3" exact, "W5NGU" any SSID, "W5N*" prefix.
        """
        call = callsign.strip().upper()
        if call.endswith("*"):
            prefix = call.rstrip("*").replace("\\", "").replace("%", "").replace("_", "\\_")
            return f"{column} LIKE ? ESCAPE '\\'", [prefix + "%"]
        if "-" in call:
            return f"{column} = ?", [call]
        return f"({column} = ? OR {column} LIKE ? ESCAPE '\\')", [
            call, call.replace("_", "\\_") + "-%"
        ]

    def _search_where(self, callsign=None, text=None, via=None, since=None,
                      until=None, setup=None):
        clauses, args = [], []
        if since is not None:
            clauses.append("received_at >= ?")
            args.append(int(since))
        if until is not None:
            clauses.append("received_at < ?")
            args.append(int(until))
        if callsign:
            clause, a = self._callsign_clause("source", callsign)
            # Also messages gated from APRS-IS, where the originator
            # is inside a third-party header: "}W1ABC-9>APRS,...".
            inner, b = self._callsign_clause(
                "substr(payload, 2, instr(payload, '>') - 2)", callsign
            )
            clauses.append(
                f"({clause} OR (payload LIKE '}}%' AND {inner}))"
            )
            args += a + b
        if via:
            clause, a = self._callsign_clause(
                "upper(rtrim(json_each.value, '*'))", via
            )
            clauses.append(
                "EXISTS (SELECT 1 FROM json_each(received_packets.digipeaters)"
                f" WHERE {clause})"
            )
            args += a
        if text:
            escaped = (text.replace("\\", "\\\\").replace("%", "\\%")
                       .replace("_", "\\_"))
            clauses.append("payload LIKE ? ESCAPE '\\'")
            args.append("%" + escaped + "%")
        if setup is not None:
            clauses.append("COALESCE(setup, '') = ?")
            args.append(setup)
        return (" AND ".join(clauses) or "1"), args

    def search_packets(self, callsign=None, text=None, via=None, since=None,
                       until=None, setup=None, before_id=None, limit=200):
        """
        Stored packets matching every given filter, newest first.

        ``callsign`` matches the sender (or the originator of a gated
        third-party packet); ``via`` a digipeater in the path; ``text``
        a case-insensitive substring of the payload. Page with
        ``before_id`` (the last id of the previous page). Returns
        (rows, more).
        """
        where, args = self._search_where(
            callsign, text, via, since, until, setup
        )
        if before_id is not None:
            where += " AND id < ?"
            args.append(int(before_id))
        limit = max(1, min(int(limit), 1000))
        cursor = self._connection.execute(
            f"""
            SELECT id, received_at, source, destination, frame_type, port,
                   digipeaters, payload, setup
            FROM received_packets
            WHERE {where}
            ORDER BY id DESC
            LIMIT ?
            """,
            args + [limit + 1],
        )
        rows = []
        for (row_id, received_at, source, dest, frame_type, port, digis,
             payload, setup_value) in cursor:
            try:
                digipeaters = json.loads(digis or "[]")
            except ValueError:
                digipeaters = []
            rows.append({
                "id": row_id,
                "received_at": received_at,
                "source": source,
                "destination": dest,
                "frame_type": frame_type,
                "port": port,
                "digipeaters": digipeaters,
                "payload": payload,
                "setup": setup_value,
            })
        more = len(rows) > limit
        return rows[:limit], more

    def station_summary(self, callsign, since=None, until=None):
        """
        When and how a callsign has been heard: packet count, first and
        last heard, how many were heard directly, and which
        digipeaters we heard it through (the station that transmitted
        each copy, see aprs_coverage.last_hop), most frequent first.
        """
        from aprs_coverage import last_hop

        where, args = self._search_where(
            callsign=callsign, since=since, until=until
        )
        count = first = last = 0
        direct = 0
        via = {}
        sources = {}
        for received_at, source, digis in self._connection.execute(
            f"SELECT received_at, source, digipeaters FROM received_packets "
            f"WHERE {where}", args,
        ):
            count += 1
            first = received_at if not first else min(first, received_at)
            last = max(last, received_at)
            sources[source] = sources.get(source, 0) + 1
            try:
                hops = json.loads(digis or "[]")
            except ValueError:
                hops = []
            hop = last_hop(hops)
            if hop is None:
                direct += 1
                continue
            hop = hop or "(unidentified)"
            entry = via.setdefault(hop, [0, 0])
            entry[0] += 1
            entry[1] = max(entry[1], received_at)
        return {
            "callsign": callsign.strip().upper(),
            "packets": count,
            "first_heard": first or None,
            "last_heard": last or None,
            "direct": direct,
            "sources": sorted(
                ({"callsign": c, "packets": n} for c, n in sources.items()),
                key=lambda e: (-e["packets"], e["callsign"]),
            )[:10],
            "via": sorted(
                ({"callsign": c, "packets": n, "last_heard": t}
                 for c, (n, t) in via.items()),
                key=lambda e: (-e["packets"], e["callsign"]),
            )[:10],
        }

    # -- settings and coverage ------------------------------------------

    def get_setting(self, key: str) -> Optional[str]:
        row = self._connection.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
        return None if row is None else row[0]

    def set_setting(self, key: str, value: Optional[str]) -> None:
        with self._connection:
            self._connection.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def log_own_tx(self, sent_at, kind, ref, path, setup) -> int:
        with self._connection:
            cursor = self._connection.execute(
                "INSERT INTO own_tx (sent_at, kind, ref, path, setup) "
                "VALUES (?, ?, ?, ?, ?)",
                (float(sent_at), kind, ref or "", path or "", setup or ""),
            )
        return cursor.lastrowid

    def record_own_heard(self, heard_at, kind, ref, via, station) -> bool:
        """
        Credit a heard-back copy to the latest matching transmission
        sent within MATCH_WINDOW_SECONDS before it. Returns True if it
        was recorded (False: no matching transmission, or that
        digipeater/iGate was already credited for it).
        """
        from aprs_coverage import MATCH_WINDOW_SECONDS

        row = self._connection.execute(
            """
            SELECT id FROM own_tx
            WHERE kind = ? AND ref = ? AND sent_at <= ? AND sent_at >= ?
            ORDER BY sent_at DESC, id DESC LIMIT 1
            """,
            (kind, ref or "", float(heard_at) + 5,
             float(heard_at) - MATCH_WINDOW_SECONDS),
        ).fetchone()
        if row is None:
            return False
        with self._connection:
            cursor = self._connection.execute(
                "INSERT OR IGNORE INTO own_heard "
                "(tx_id, heard_at, via, station) VALUES (?, ?, ?, ?)",
                (row[0], float(heard_at), via, station or ""),
            )
        return cursor.rowcount > 0

    def latest_position(self, callsign: str, scan: int = 50):
        """Most recent stored position report from ``callsign``, or None."""
        from aprs import parse_position

        rows = self._connection.execute(
            """
            SELECT payload, destination FROM received_packets
            WHERE source = ? AND payload IS NOT NULL
            ORDER BY received_at DESC, id DESC LIMIT ?
            """,
            (callsign, int(scan)),
        )
        for payload, destination in rows:
            position = parse_position(payload, destination=destination)
            if position is not None:
                return position
        return None

    def coverage_report(self, since, home=None, is_own=None):
        """
        Per-setup coverage since ``since`` (Unix seconds).

        ``home`` is (latitude, longitude) for distances, or None.
        ``is_own(callsign)`` marks our own stations (left out of the
        receive-side numbers). Newest-used setup first.
        """
        from aprs import parse_position
        from aprs_coverage import distance_miles

        is_own = is_own or (lambda _c: False)
        since = float(since)
        setups = {}

        def entry(name):
            name = name or ""
            if name not in setups:
                setups[name] = {
                    "setup": name,
                    "first_used": None,
                    "last_used": None,
                    "tx": {"total": 0, "beacons": 0, "messages": 0,
                           "heard": 0, "digipeated": 0, "igated": 0},
                    "digipeaters": [],
                    "igates": [],
                    "rx": {"hours": 0, "packets": 0, "packets_per_hour": 0.0,
                           "stations": 0, "direct_stations": 0,
                           "farthest_direct": []},
                }
            return setups[name]

        def seen(e, t):
            e["first_used"] = t if e["first_used"] is None else min(e["first_used"], t)
            e["last_used"] = t if e["last_used"] is None else max(e["last_used"], t)

        # -- getting out ------------------------------------------------
        tx_rows = self._connection.execute(
            "SELECT id, sent_at, kind, setup FROM own_tx WHERE sent_at >= ?",
            (since,),
        ).fetchall()
        heard_rows = self._connection.execute(
            """
            SELECT h.tx_id, h.heard_at, h.via, h.station
            FROM own_heard h JOIN own_tx t ON t.id = h.tx_id
            WHERE t.sent_at >= ?
            """,
            (since,),
        ).fetchall()

        heard_by_tx = {}
        for tx_id, heard_at, via, station in heard_rows:
            heard_by_tx.setdefault(tx_id, []).append((via, station, heard_at))

        per_station = {}  # (setup, via, station) -> [count, last_heard]
        for tx_id, sent_at, kind, setup in tx_rows:
            e = entry(setup)
            seen(e, sent_at)
            tx = e["tx"]
            tx["total"] += 1
            tx["beacons" if kind == "beacon" else "messages"] += 1
            heard = heard_by_tx.get(tx_id, [])
            if heard:
                tx["heard"] += 1
            if any(v == "digi" for v, _, _ in heard):
                tx["digipeated"] += 1
            if any(v == "igate" for v, _, _ in heard):
                tx["igated"] += 1
            for via, station, heard_at in heard:
                key = (e["setup"], via, station)
                count = per_station.setdefault(key, [0, 0.0])
                count[0] += 1
                count[1] = max(count[1], heard_at)

        positions = {}
        for (setup, via, station), (count, last_heard) in per_station.items():
            e = setups[setup]
            item = {
                "callsign": station,
                "heard": count,
                "pct": round(100.0 * count / e["tx"]["total"], 1),
                "last_heard": last_heard,
            }
            if via == "digi":
                if station and station not in positions:
                    positions[station] = self.latest_position(station)
                pos = positions.get(station)
                item["latitude"] = pos.latitude if pos else None
                item["longitude"] = pos.longitude if pos else None
                item["distance_mi"] = (
                    round(distance_miles(home[0], home[1],
                                         pos.latitude, pos.longitude), 1)
                    if pos and home else None
                )
                e["digipeaters"].append(item)
            else:
                e["igates"].append(item)

        for e in setups.values():
            for key in ("digipeaters", "igates"):
                e[key].sort(key=lambda i: (-i["heard"], i["callsign"]))

        # -- hearing in -------------------------------------------------
        rows = self._connection.execute(
            """
            SELECT received_at, source, destination, digipeaters, payload,
                   setup
            FROM received_packets
            WHERE received_at >= ? AND port = 2
            """,
            (int(since),),
        )
        parsed = {}
        rx = {}  # setup -> accumulators
        for received_at, source, dest, digis, payload, setup in rows:
            if is_own(source):
                continue
            acc = rx.setdefault(setup or "", {
                "hours": set(), "packets": 0, "stations": set(),
                "direct": {},
            })
            acc["hours"].add(int(received_at) // 3600)
            acc["packets"] += 1
            acc["stations"].add(source)
            if digis and "*" in digis:
                continue  # digipeated: not heard directly
            best = acc["direct"].get(source)
            if payload is None or home is None:
                if best is None:
                    acc["direct"][source] = (None, received_at)
                continue
            key = (payload, dest)
            if key not in parsed:
                parsed[key] = parse_position(payload, destination=dest)
            pos = parsed[key]
            miles = (
                distance_miles(home[0], home[1], pos.latitude, pos.longitude)
                if pos is not None else None
            )
            if best is None or (miles is not None and (
                    best[0] is None or miles > best[0])):
                acc["direct"][source] = (miles, received_at)

        for setup, acc in rx.items():
            e = entry(setup)
            hours = len(acc["hours"])
            for hour in acc["hours"]:
                seen(e, hour * 3600)
            far = sorted(
                ((c, m, t) for c, (m, t) in acc["direct"].items()
                 if m is not None),
                key=lambda x: -x[1],
            )[:5]
            e["rx"] = {
                "hours": hours,
                "packets": acc["packets"],
                "packets_per_hour": round(acc["packets"] / hours, 1) if hours else 0.0,
                "stations": len(acc["stations"]),
                "direct_stations": len(acc["direct"]),
                "farthest_direct": [
                    {"callsign": c, "distance_mi": round(m, 1), "heard_at": t}
                    for c, m, t in far
                ],
            }

        return sorted(
            setups.values(), key=lambda e: -(e["last_used"] or 0)
        )

    def oldest_packet_at(self) -> Optional[int]:
        row = self._connection.execute(
            "SELECT MIN(received_at) FROM received_packets"
        ).fetchone()
        return None if row[0] is None else int(row[0])

    def total_packets(self) -> int:
        """Return the number of persisted packet records."""

        row = self._connection.execute(
            "SELECT COUNT(*) FROM received_packets"
        ).fetchone()

        return int(row[0])

    def hourly_counts(self, since: Optional[int] = None):
        """Return hourly UTC packet counts in chronological order."""

        query = """
            SELECT
                (received_at / 3600) * 3600 AS hour_start,
                COUNT(*) AS packet_count
            FROM received_packets
        """

        parameters = ()

        if since is not None:
            query += " WHERE received_at >= ?"
            parameters = (int(since),)

        query += " GROUP BY hour_start ORDER BY hour_start"

        return [
            {
                "hour_start": row[0],
                "packet_count": row[1],
            }
            for row in self._connection.execute(query, parameters)
        ]

    _BULLETIN_WHERE = (
        "(addressee GLOB 'BLN?*' OR addressee GLOB 'NWS[-_]*')"
    )

    def list_bulletins(self, since: Optional[int] = None, limit: int = 200):
        """
        Bulletins/announcements heard, one entry per (source, addressee),
        newest first. Bulletins are re-sent every few minutes, so each
        entry carries the latest text plus first/last heard and a count.
        """
        where = self._BULLETIN_WHERE
        params: list = []
        if since is not None:
            where += " AND received_at >= ?"
            params.append(int(since))

        # Latest text per (source, addressee): an explicit subquery,
        # since SQLite's "bare column" shortcut is undefined once a
        # query uses both MIN() and MAX().
        query = (
            "SELECT g.source, g.addressee, ("
            "  SELECT m.message_text FROM aprs_messages m"
            "  WHERE m.source = g.source AND m.addressee = g.addressee"
            "  ORDER BY m.received_at DESC, m.id DESC LIMIT 1"
            "), g.last, g.first, g.n FROM ("
            "  SELECT source, addressee, MAX(received_at) AS last,"
            "         MIN(received_at) AS first, COUNT(*) AS n"
            "  FROM aprs_messages WHERE " + where +
            "  GROUP BY source, addressee"
            ") g ORDER BY g.last DESC LIMIT ?"
        )
        params.append(int(limit))

        result = []
        for row in self._connection.execute(query, params):
            kind = bulletin_kind(row[1])
            if kind is None:
                continue
            result.append({
                "source": row[0],
                "addressee": row[1],
                "kind": kind,
                "text": row[2],
                "last_heard": row[3],
                "first_heard": row[4],
                "count": row[5],
            })
        return result

    def clear_bulletins(self) -> int:
        with self._connection:
            cursor = self._connection.execute(
                "DELETE FROM aprs_messages WHERE " + self._BULLETIN_WHERE
            )
        return cursor.rowcount

    def prune_bulletins(self, older_than: int) -> int:
        """Bulletins repeat all day; drop copies older than the cutoff."""
        with self._connection:
            cursor = self._connection.execute(
                "DELETE FROM aprs_messages WHERE " + self._BULLETIN_WHERE
                + " AND received_at < ?",
                (int(older_than),),
            )
        return cursor.rowcount

    def list_aprs_messages(
        self,
        addressee: str,
        limit: int = 100,
    ):
        """Return recent APRS messages addressed to a callsign."""

        if not isinstance(addressee, str) or not addressee:
            raise ValueError("An APRS addressee is required")

        if isinstance(limit, bool) or not isinstance(limit, int):
            raise ValueError("Limit must be an integer")

        if not 1 <= limit <= 500:
            raise ValueError("Limit must be between 1 and 500")

        rows = self._connection.execute(
            """
            SELECT
                id,
                received_at,
                source,
                addressee,
                message_text,
                message_id,
                kind,
                port
            FROM aprs_messages
            WHERE addressee = ?
            ORDER BY received_at DESC, id DESC
            LIMIT ?
            """,
            (addressee.strip().upper(), limit),
        ).fetchall()

        return [
            {
                "id": row[0],
                "received_at": row[1],
                "source": row[2],
                "addressee": row[3],
                "message": row[4],
                "message_id": row[5],
                "kind": row[6],
                "port": row[7],
            }
            for row in rows
        ]

    def save_outbound(self, record: dict) -> None:
        """Insert or update one sent-message record."""
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO aprs_outbound (message_id, sent_at, record)
                VALUES (?, ?, ?)
                ON CONFLICT(message_id) DO UPDATE SET
                    sent_at = excluded.sent_at,
                    record = excluded.record
                """,
                (
                    record["message_id"],
                    float(record["sent_at"]),
                    json.dumps(record),
                ),
            )

    def list_outbound(self, limit: int = 200):
        """Most recent sent-message records, newest first."""
        rows = self._connection.execute(
            "SELECT record FROM aprs_outbound "
            "ORDER BY sent_at DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def clear_outbound(self) -> int:
        with self._connection:
            cursor = self._connection.execute("DELETE FROM aprs_outbound")
        return cursor.rowcount

    def clear_aprs_messages(self, addressee: str) -> int:
        """Delete stored APRS messages addressed to a callsign."""

        if not isinstance(addressee, str) or not addressee.strip():
            raise ValueError("An APRS addressee is required")

        with self._connection:
            cursor = self._connection.execute(
                "DELETE FROM aprs_messages WHERE addressee = ?",
                (addressee.strip().upper(),),
            )

        return cursor.rowcount

    def close(self) -> None:
        self._connection.close()
