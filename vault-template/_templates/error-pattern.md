---
type: error-pattern
created: 'YYYY-MM-DDTHH:MM:SSZ'
updated: 'YYYY-MM-DDTHH:MM:SSZ'
agent: your-agent
tags: []
related: []
priority: P2
category: infra | runtime | security | integration | automation | config
severity: low | medium | high | critical
trigger_condition: >-
  The observable sign, verbatim: the log line, the service behaviour, the error
  text. This is the field future agents match on — an entry without it will not
  be found. Describe what is seen, not what is wrong.
prevention_rule: >-
  The rule that stops this recurring. Write it as an instruction to a future
  agent, not as a description of the fix that was applied.
---

# Error pattern title

Опиши случай прозой: когда, у кого, как проявилось. Одна страница — потолок.

> **Признак и правило живут в шапке, а не в теле.** `trigger_condition` и
> `prevention_rule` — обязательные параметры `create_error_pattern_note`,
> сервер сам кладёт их во frontmatter. Не дублируй их разделами в теле:
> поиск идёт по шапке, и запись с признаком только в прозе не находится.

## Root cause

What was actually wrong? Reference file paths and line numbers when applicable.
Distinguish between «the proximate trigger» and «the underlying design flaw».

## Fix

What did you change? Commit hash if available:

```
commit abc123 — file.py: brief description
```

If the fix is multi-file or non-obvious, describe the shape, not just the diff.

## Detection

How would you notice this happening again? An alert? A log line? A metric
threshold? A test? This is narrower than `trigger_condition`: the trigger is
what a human or agent sees, detection is what the system could catch by itself.

## References

- PR / commit links
- Related decisions
- Related error patterns
- External writeups (provider status pages, GitHub issues, etc.)

<!--
Заметки пишутся инструментом, не руками: mcp__gbrain-memory__create_error_pattern_note.
Обязательные параметры: title, category, severity, trigger_condition,
prevention_rule, body, tags. Ручные правки файлов в vault затираются.
Этот шаблон описывает результат, который выдаёт инструмент, — держать в
соответствии с ним, а не наоборот.
-->
