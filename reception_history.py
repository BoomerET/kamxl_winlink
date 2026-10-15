"""Persistent SQLite storage for received AX.25 packet metadata."""

import json
import sqlite3
import time
from pathlib import Path
from typing import Optional

from packet import Packet


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

    def close(self) -> None:
        self._connection.close()
