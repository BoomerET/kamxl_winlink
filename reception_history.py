"""Persistent SQLite storage for received AX.25 packet metadata."""

import json
import sqlite3
import time
from pathlib import Path
from typing import Optional

from packet import Packet
from aprs import parse_aprs_message, unwrap_third_party


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
        """)

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
                    digipeaters
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    timestamp,
                    packet.source,
                    packet.destination,
                    packet.frame_type,
                    packet.port,
                    json.dumps(packet.digipeaters),
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
