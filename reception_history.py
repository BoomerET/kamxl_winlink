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
        """)

        # Added later: the packet's info text, so the map can be
        # rebuilt after a restart. Older rows have NULL here.
        columns = {
            row[1] for row in self._connection.execute(
                "PRAGMA table_info(received_packets)"
            )
        }
        if "payload" not in columns:
            self._connection.execute(
                "ALTER TABLE received_packets ADD COLUMN payload TEXT"
            )

        self._connection.commit()

    def record(
        self,
        packet: Packet,
        received_at: Optional[int] = None,
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
                    payload
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    timestamp,
                    packet.source,
                    packet.destination,
                    packet.frame_type,
                    packet.port,
                    json.dumps(packet.digipeaters),
                    packet.payload,
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
