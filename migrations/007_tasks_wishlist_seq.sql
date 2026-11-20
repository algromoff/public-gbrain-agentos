-- gbrain task-board: separate id range for wishlist tasks (8000+).
--
-- Operator rule 2026-08-12: the wishlist lives on the board and is numbered
-- from 8000, so a wishlist item is recognisable by its number alone.
-- Operational tasks keep using tasks_id_seq (1..N) untouched.
--
-- Idempotent: safe to re-run (guarded by metadata.legacy_id).

-- Wishlist sequence ---------------------------------------------------------

CREATE SEQUENCE IF NOT EXISTS tasks_wishlist_id_seq
    AS bigint
    START WITH 8000
    MINVALUE 8000
    INCREMENT BY 1
    NO CYCLE;

GRANT ALL PRIVILEGES ON SEQUENCE tasks_wishlist_id_seq TO gbrain;

-- Renumbering support -------------------------------------------------------
-- task_history.task_id is the only FK onto tasks(id). Without ON UPDATE
-- CASCADE any renumbering of a task aborts with a FK violation and its audit
-- trail would have to be rebuilt by hand.

ALTER TABLE task_history DROP CONSTRAINT IF EXISTS task_history_task_id_fkey;
ALTER TABLE task_history
    ADD CONSTRAINT task_history_task_id_fkey
    FOREIGN KEY (task_id) REFERENCES tasks (id)
    ON UPDATE CASCADE ON DELETE RESTRICT;

-- Move the existing wishlist tasks into the new range ------------------------
-- Six tasks were tagged metadata.type=wishlist by jarvis on 2026-08-12:
-- 18, 22, 34, 53, 70, 107. Numbered by ascending old id, so the mapping is
-- 18->8000, 22->8001, 34->8002, 53->8003, 70->8004, 107->8005.
--
-- The old number is kept in metadata.legacy_id: external mentions ("id=53")
-- live in gbrain notes, handoff files and decisions.md, which this migration
-- does not rewrite. legacy_id is what makes them resolvable afterwards.
-- It doubles as the idempotency guard: an already moved task is skipped.

WITH to_move AS (
    SELECT id AS old_id,
           7999 + row_number() OVER (ORDER BY id) AS new_id
    FROM tasks
    WHERE id IN (18, 22, 34, 53, 70, 107)
      AND metadata->>'type' = 'wishlist'
      AND NOT (metadata ? 'legacy_id')
)
UPDATE tasks t
SET id = m.new_id,
    metadata = t.metadata || jsonb_build_object('legacy_id', t.id)
FROM to_move m
WHERE t.id = m.old_id;

-- Park the sequence past whatever is now occupied, so the next wishlist task
-- gets a free number even if this migration moved nothing.

SELECT setval(
    'tasks_wishlist_id_seq',
    COALESCE((SELECT max(id) FROM tasks WHERE id >= 8000), 7999) + 1,
    false
);
