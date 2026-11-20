"""delivery_outbox state machine + DB operations.

States: pending → sent → ack_missing → acked | failed

pending      — enqueued, not delivered yet.
sent         — HTTP 2xx from the agent gateway: accepted FOR DELIVERY, not processed.
acked        — the receiving agent called ack() after actually handling the payload.
ack_missing  — delivered, but no ack within ACK_TIMEOUT_MINUTES (worker sweep). This is
               the "delivered into the void" signal: session died, context overflowed,
               or the agent ignored it.
failed       — permanent delivery failure (4xx, or retries exhausted).

Until 2026-08-19 the worker collapsed sent+acked into one status on HTTP 2xx. Consequence:
an honest ack() from the agent found no updatable row and returned false, ack_missing never
occurred, and stats reported delivery as processing. Un-collapsed in task 145.
"""
import logging
import os
import secrets
from typing import Any

import asyncpg

logger = logging.getLogger(__name__)

BACKOFF_BASE_SEC = 10
BACKOFF_CAP_SEC = 300

# A row sitting in 'sent' longer than this is swept to 'ack_missing'.
ACK_TIMEOUT_MINUTES = int(os.environ.get("SWARM_ACK_TIMEOUT_MIN", "30"))

# Statuses an agent still owns: delivered or queued, but not yet handled.
OPEN_STATUSES = ("pending", "sent", "ack_missing")
ALL_STATUSES = ("pending", "sent", "ack_missing", "acked", "failed")


def _rowcount(result: str) -> int:
    """asyncpg execute() returns a command tag like 'UPDATE 1'."""
    return int(result.split()[-1]) if result.startswith("UPDATE ") else 0


def compute_backoff_seconds(attempts: int) -> int:
    """Exponential backoff: 10s × 2^(n-1), capped at 300s."""
    if attempts <= 0:
        return 0
    delay = BACKOFF_BASE_SEC * (2 ** (attempts - 1))
    return min(delay, BACKOFF_CAP_SEC)


def make_task_id(from_agent: str, to_agent: str) -> str:
    """Generate idempotency-friendly task_id."""
    nonce = secrets.token_hex(8)
    return f"{from_agent}::{to_agent}::{nonce}"


async def enqueue(
    pool: asyncpg.Pool,
    from_agent: str,
    to_agent: str,
    payload: dict[str, Any],
    task_id: str | None = None,
    max_attempts: int = 5,
) -> str:
    """Insert a pending delivery row. Returns task_id (idempotent on conflict)."""
    if task_id is None:
        task_id = make_task_id(from_agent, to_agent)

    row = await pool.fetchrow(
        """
        INSERT INTO delivery_outbox
          (task_id, from_agent, to_agent, payload, max_attempts, next_retry_at)
        VALUES ($1, $2, $3, $4::jsonb, $5, now())
        ON CONFLICT (task_id) DO UPDATE
          SET updated_at = now()
        RETURNING task_id, status, attempts
        """,
        task_id,
        from_agent,
        to_agent,
        _json_dumps(payload),
        max_attempts,
    )
    logger.info(
        "outbox.enqueue task_id=%s to=%s status=%s attempts=%d",
        row["task_id"],
        to_agent,
        row["status"],
        row["attempts"],
    )
    return row["task_id"]


async def fetch_due(
    pool: asyncpg.Pool,
    limit: int = 20,
) -> list[asyncpg.Record]:
    """Fetch up to N pending rows whose next_retry_at <= now()."""
    return await pool.fetch(
        """
        SELECT id, task_id, from_agent, to_agent, payload::text AS payload_json,
               attempts, max_attempts
        FROM delivery_outbox
        WHERE status = 'pending'
          AND next_retry_at <= now()
        ORDER BY created_at
        LIMIT $1
        FOR UPDATE SKIP LOCKED
        """,
        limit,
    )


async def mark_sent(conn: asyncpg.Connection, task_id: str) -> bool:
    """Mark row as handed to the gateway (HTTP 2xx), now awaiting the agent's ack.

    Only 'pending' rows move. A row the agent already acked must not be dragged back:
    the agent can ack while the worker transaction is still open.
    """
    result = await conn.execute(
        """
        UPDATE delivery_outbox
        SET status = 'sent', updated_at = now()
        WHERE task_id = $1 AND status = 'pending'
        """,
        task_id,
    )
    rowcount = _rowcount(result)
    if rowcount > 0:
        logger.info("outbox.mark_sent task_id=%s", task_id)
    return rowcount > 0


async def mark_acked(conn: asyncpg.Connection, task_id: str) -> bool:
    """Agent-side ack: the payload was actually handled.

    True  — the row exists and is now acked, INCLUDING a repeat ack on an already
            acked row (idempotent).
    False — no such task_id.

    The discriminator is row existence, not rowcount, so the caller can tell
    "already done" from "wrong task_id" — the two used to be indistinguishable.
    """
    async with conn.transaction():
        row = await conn.fetchrow(
            "SELECT status FROM delivery_outbox WHERE task_id = $1 FOR UPDATE",
            task_id,
        )
        if row is None:
            logger.info("outbox.mark_acked task_id=%s unknown_task_id", task_id)
            return False
        prev = row["status"]
        if prev == "acked":
            logger.info("outbox.mark_acked task_id=%s already_acked (idempotent)", task_id)
            return True
        if prev == "failed":
            # The agent demonstrably holds the payload; trust the agent over the transport.
            logger.warning("outbox.mark_acked task_id=%s acked a failed delivery", task_id)
        await conn.execute(
            "UPDATE delivery_outbox SET status = 'acked', updated_at = now() WHERE task_id = $1",
            task_id,
        )
        logger.info("outbox.mark_acked task_id=%s from=%s", task_id, prev)
        return True


async def sweep_ack_missing(
    pool: asyncpg.Pool,
    timeout_minutes: int = ACK_TIMEOUT_MINUTES,
) -> int:
    """Move 'sent' rows older than the timeout to 'ack_missing'. Returns the count.

    updated_at is the delivery timestamp (mark_sent sets it) and the sweep bumps it
    again, so each row is swept exactly once.
    """
    result = await pool.execute(
        """
        UPDATE delivery_outbox
        SET status = 'ack_missing', updated_at = now()
        WHERE status = 'sent'
          AND updated_at < now() - ($1 || ' minutes')::interval
        """,
        str(timeout_minutes),
    )
    n = _rowcount(result)
    if n > 0:
        logger.warning(
            "outbox.ack_missing %d deliveries got no ack within %d min", n, timeout_minutes
        )
    return n


async def mark_retry(
    conn: asyncpg.Connection,
    row_id: int,
    attempts: int,
    max_attempts: int,
    last_error: str,
) -> str:
    """Schedule a retry or move to failed if attempts exhausted. Returns new status."""
    if attempts >= max_attempts:
        await conn.execute(
            """
            UPDATE delivery_outbox
            SET status = 'failed',
                attempts = $2,
                updated_at = now()
            WHERE id = $1
            """,
            row_id,
            attempts,
        )
        logger.warning("outbox.failed id=%d attempts=%d last_error=%s", row_id, attempts, last_error[:200])
        return "failed"

    delay = compute_backoff_seconds(attempts)
    await conn.execute(
        """
        UPDATE delivery_outbox
        SET attempts = $2,
            next_retry_at = now() + ($3 || ' seconds')::interval,
            updated_at = now()
        WHERE id = $1
        """,
        row_id,
        attempts,
        str(delay),
    )
    logger.info(
        "outbox.retry id=%d attempts=%d/%d delay=%ds last_error=%s",
        row_id, attempts, max_attempts, delay, last_error[:120],
    )
    return "pending"


async def bootstrap_recovery(pool: asyncpg.Pool) -> int:
    """Report deliveries waiting for an agent ack on startup. Mutates nothing.

    Until task 145 this reset 'sent'/'ack_missing' back to 'pending'. Harmless while
    those statuses never occurred — destructive now that the worker really marks 'sent':
    every restart would re-POST already delivered payloads and the receiving agent would
    see each task twice. A worker that dies mid-flight leaves its row 'pending' (mark_sent
    had not committed yet), so the ordinary retry path still covers the crash case.
    """
    row = await pool.fetchrow(
        """
        SELECT count(*)::int AS c FROM delivery_outbox
        WHERE status IN ('sent', 'ack_missing')
        """
    )
    n = int(row["c"]) if row else 0
    if n > 0:
        logger.info("outbox.bootstrap %d deliveries awaiting agent ack (left untouched)", n)
    return n


async def get_stats(pool: asyncpg.Pool) -> dict[str, int]:
    """Return counts per status for observability.

    Every known status is present even at zero, so a monitor can see that
    'sent'/'ack_missing' exist as concepts instead of reading a missing key as
    "no such problem". 'acked' now means processed by the agent, not delivered.
    """
    rows = await pool.fetch(
        "SELECT status, count(*)::int AS c FROM delivery_outbox GROUP BY status"
    )
    stats = {status: 0 for status in ALL_STATUSES}
    for row in rows:
        stats[row["status"]] = row["c"]
    return stats


async def list_recent(
    pool: asyncpg.Pool,
    limit: int = 50,
    status_filter: str | None = None,
) -> list[dict[str, Any]]:
    """Return most recent deliveries across all agents (for Kanban dashboard).

    Read-only — does not mutate any rows. Sorted by created_at DESC.
    status_filter: None → all statuses; 'pending' / 'acked' / 'failed' to scope.
    """
    import json as _json
    if status_filter:
        rows = await pool.fetch(
            """
            SELECT task_id, from_agent, to_agent, status, attempts, max_attempts,
                   payload::text AS payload_json, created_at, updated_at
            FROM delivery_outbox
            WHERE status = $1
            ORDER BY created_at DESC
            LIMIT $2
            """,
            status_filter, limit,
        )
    else:
        rows = await pool.fetch(
            """
            SELECT task_id, from_agent, to_agent, status, attempts, max_attempts,
                   payload::text AS payload_json, created_at, updated_at
            FROM delivery_outbox
            ORDER BY created_at DESC
            LIMIT $1
            """,
            limit,
        )
    return [
        {
            "task_id": r["task_id"],
            "from_agent": r["from_agent"],
            "to_agent": r["to_agent"],
            "status": r["status"],
            "attempts": r["attempts"],
            "max_attempts": r["max_attempts"],
            "payload": _json.loads(r["payload_json"]),
            "created_at": r["created_at"].isoformat(),
            "updated_at": r["updated_at"].isoformat(),
        }
        for r in rows
    ]


async def list_pending_for(pool: asyncpg.Pool, to_agent: str, limit: int = 20) -> list[dict[str, Any]]:
    """Return the agent's open inbox: everything addressed to it and not yet acked.

    Covers 'pending' (not delivered yet), 'sent' (delivered, awaiting this agent's ack)
    and 'ack_missing' (delivered, ack overdue). 'sent' is the important one: an agent
    that restarted between delivery and ack would otherwise find an empty inbox and the
    task would be lost silently.
    """
    import json as _json
    rows = await pool.fetch(
        """
        SELECT task_id, from_agent, status, payload::text AS payload_json,
               created_at, updated_at, attempts
        FROM delivery_outbox
        WHERE to_agent = $1 AND status = ANY($2::text[])
        ORDER BY created_at
        LIMIT $3
        """,
        to_agent, list(OPEN_STATUSES), limit,
    )
    return [
        {
            "task_id": r["task_id"],
            "from_agent": r["from_agent"],
            "status": r["status"],
            "payload": _json.loads(r["payload_json"]),
            "created_at": r["created_at"].isoformat(),
            "updated_at": r["updated_at"].isoformat(),
            "attempts": r["attempts"],
        }
        for r in rows
    ]


async def get_row(pool: asyncpg.Pool, task_id: str) -> dict[str, Any] | None:
    """Fetch a single delivery row by task_id."""
    row = await pool.fetchrow(
        """
        SELECT task_id, from_agent, to_agent, status, attempts, max_attempts,
               payload::text AS payload_json, created_at, updated_at,
               next_retry_at
        FROM delivery_outbox
        WHERE task_id = $1
        """,
        task_id,
    )
    if row is None:
        return None
    import json as _json
    return {
        "task_id": row["task_id"],
        "from_agent": row["from_agent"],
        "to_agent": row["to_agent"],
        "status": row["status"],
        "attempts": row["attempts"],
        "max_attempts": row["max_attempts"],
        "payload": _json.loads(row["payload_json"]),
        "created_at": row["created_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
        "next_retry_at": row["next_retry_at"].isoformat(),
    }


def _json_dumps(payload: dict[str, Any]) -> str:
    import json
    return json.dumps(payload, ensure_ascii=False, default=str)
