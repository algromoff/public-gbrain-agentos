# 80-error-patterns

Incidents + fixes. **Weighted 3.0× in recall** — this folder is the team's institutional muscle memory.

## What goes here

- Bugs you hit, with the symptom you observed, the root cause, and the fix
- Production incidents, even minor ones
- «I spent two hours on this, no one else should» moments
- Provider quirks that bit you (rate limits, undocumented behavior, breaking changes)
- Recurring failure modes (write the pattern, not just the instance)

## What does NOT go here

- Step-by-step procedures — those go to `70-runbooks/`
- Decisions made in response — `30-decisions/`
- General knowledge — `50-knowledge/`

## Why weight 3.0×

When an agent searches the vault, results from this folder are ranked higher than the same text in other folders. The reasoning: if you have ever solved this exact problem, you want to surface that fact **before** you start fresh debugging. The 3.0× multiplier is tuned in `services/recall_mcp/source_weights.py` — adjust if your team's error-pattern signal is too noisy or too weak.

Note that 3.0 is a folder-level recall weight, not a field of the note. Individual entries carry `priority` and `category` instead.

## What a good error-pattern entry looks like

1. **trigger_condition** — exactly what you observed (logs, error messages, user-facing behavior). Lives in the frontmatter, not in the body: this is the field future agents match on. An entry whose sign exists only in prose does not get found.
2. **prevention_rule** — the rule that stops this recurring, written as an instruction to a future agent. Also frontmatter.
3. **Root cause** — the actual bug, with file / line references when applicable
4. **Fix** — what you changed, with a commit hash if possible
5. **Detection** — how the system itself could catch this next time (alert, log line, metric, test). Narrower than `trigger_condition`, which is what a human or agent sees.

The format is intentionally similar to a postmortem, but lighter — a one-page write-up is fine.

## Frontmatter

```yaml
---
type: error-pattern
created: '2026-05-16T10:00:00Z'
updated: '2026-05-16T10:00:00Z'
agent: coder-agent
tags: [mcp, auth, asgi]
related: ['30-decisions/2026-05-16-drop-fallback-token.md']
priority: P2
category: integration
severity: high
trigger_condition: 'MCP client sends the auth header, the server sees none; requests 401 only on stateless transport'
prevention_rule: 'Any ASGI middleware reading request headers must run before the transport strips them — assert it in a test, not by inspection'
---
```

`title`, `category`, `severity`, `trigger_condition`, `prevention_rule`, `body` and `tags` are **required parameters** of `mcp__gbrain-memory__create_error_pattern_note`. The server writes them into the frontmatter itself — you do not hand-write the file. `created`, `updated`, `type`, `agent` and `priority` are filled in by the server too.

## Naming

```
2026-05-16-mcp-stateless-headers-lost.md
2026-05-16-pgvector-hnsw-build-oom.md
2026-05-16-caddy-sse-buffering-broke-streaming.md
```

## Template

See `_templates/error-pattern.md`. The template describes what the tool produces — keep it in step with the tool, not the other way round.

## Typical authors

Every agent. Whoever found and fixed the bug writes the entry.

## Cadence

- Add: immediately after a fix lands, while context is fresh — do not defer
- Read: at the start of any debugging session, recall returns these first
- Update: only to add `related:` links when a similar incident appears

## Reading all signs at once

```bash
grep -h "^trigger_condition:" /opt/gbrain/vault/80-error-patterns/*.md
```
