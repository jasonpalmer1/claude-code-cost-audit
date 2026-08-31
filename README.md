# Claude Code Cost Audit

I built my own cost accounting over Claude Code transcripts. It silently
missed nested workflow subagent transcripts. 13 sessions under-reported;
correcting it surfaced roughly 8% of spend I thought I was already
measuring.

That was a bug in my own parser, not a claim about Claude Code or about
Anthropic's billing. Nothing here says anyone was overcharged, and nothing
here should be read that way. The only claim this repo makes is narrower and
more useful: if you roll your own transcript-based cost accounting, there is
a specific, easy-to-miss failure mode where it quietly stops counting a whole
class of subagent work. This is that fix, generalized, plus the guard rail
that made the underlying delegation habit worth measuring in the first
place.

## What this is

A small Claude Code plugin, two hooks and a slash command:

- **`token-ledger.py`** (`SessionEnd`) -- parses a session's transcript,
  and every subagent transcript that belongs to it, into a per-model token
  count and an estimated dollar cost, and appends or updates one row in a
  Markdown ledger. Pure parsing, no model call, costs nothing to run.
- **`model-guard.py`** (`PreToolUse` on `Agent`/`Task`/`Workflow`) -- blocks
  a subagent spawn that doesn't state its model explicitly, and prints a
  non-blocking warning when a `Workflow` script spawns agents without a
  model option.
- **`/cost`** -- runs the ledger in report mode and asks Claude to summarize
  spend trends and the delegation ratio from it.

Nothing here makes a model call to do its accounting. It reads transcript
JSONL files that already exist on disk and does arithmetic on them.

## The bug

Claude Code writes each session's transcript to a JSONL file, and each
subagent that session spawns gets its own transcript file. A first-pass glob
for "this session plus all its subagents" looks like:

```
<dir-of-main-transcript>/<session-id>/subagents/*.jsonl
```

one level deep, in the one project directory the main transcript happens to
live in. That misses two layouts that show up in real use:

- **Workflow-spawned agents nest one level deeper.** An agent spawned by a
  `Workflow` script lands at
  `<session-id>/subagents/workflows/wf_*/agent-*.jsonl`, not directly under
  `subagents/`. A one-level glob never sees it.
- **A session that changes directory gets a second project directory.**
  Claude Code keys a session's on-disk project directory off the working
  directory it started in. If that session later `cd`s into a different
  project, subsequent subagent transcripts for the same session id can land
  under a *second* project directory, not the one the main transcript is in.
  A glob scoped to one project directory never looks there.

The fix is a recursive glob across every project directory the session id
appears under, deduped by real path, skipping the workflow's own
`journal.jsonl` bookkeeping file (it has no usage data to parse):

```
<projects-root>/*/<session-id>/**/*.jsonl        # recursive, all project dirs
```

`token-ledger.py` in this repo derives `<projects-root>` from the transcript
path it's given rather than assuming a fixed location, so it works under any
home directory or `CLAUDE_CONFIG_DIR`, not just the machine it was written
on.

### The second-order consequence

The subagent transcripts a one-level glob drops are usually the cheap
tiers, not the expensive one -- that's the whole point of delegating
work to them. So under-counting them doesn't just under-report the total,
it specifically under-reports the *cheap* side of the ledger. Any
delegation ratio computed from the broken ledger reads more pessimistic
than reality: the main-loop tier's share of spend looks larger than it
actually was, because the denominator is missing real (cheap) work that
happened. If you've been using a ledger like this to judge how well you're
delegating, treat every ratio computed before this fix as a lower bound on
how well you were actually doing, not the real number.

## Install

**Via marketplace:**

```
/plugin marketplace add jasonpalmer1/claude-code-cost-audit
/plugin install claude-code-cost-audit@claude-code-cost-audit
```

**By hand**, if you'd rather not add a marketplace:

1. Copy `hooks/token-ledger.py` and `hooks/model-guard.py` to `~/.claude/hooks/`.
2. Copy `commands/cost.md` to `~/.claude/commands/`.
3. Add to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "SessionEnd": [
      {
        "hooks": [
          { "type": "command", "command": "python3 ~/.claude/hooks/token-ledger.py", "timeout": 10 }
        ]
      }
    ],
    "PreToolUse": [
      {
        "matcher": "Agent|Task|Workflow",
        "hooks": [
          { "type": "command", "command": "python3 ~/.claude/hooks/model-guard.py", "timeout": 10 }
        ]
      }
    ]
  }
}
```

Both hooks are stdlib-only Python 3. Nothing to `pip install`.

## What you get

A Markdown ledger, one row per session, updated in place if a session is
resumed and ends again later. The numbers below are made-up demo data, not
real sessions -- illustrating the shape of the table, nothing more:

| Date | Session | Input | Output | CacheWrite | CacheRead | HitRate | Est.Cost | By model |
|------|---------|-------|--------|-----------|-----------|---------|----------|----------|
| 2026-01-05 | demo0001 | 128,400 | 9,200 | 42,000 | 610,000 | 76% | $4.12 | haiku=$0.41 sonnet=$3.71 |
| 2026-01-06 | demo0002 | 55,000 | 3,100 | 12,000 | 88,000 | 55% | $0.98 | haiku=$0.98 |
| 2026-01-07 | demo0003 | 900,000 | 61,000 | 210,000 | 1,450,000 | 63% | $18.60 | opus=$14.02 sonnet=$4.58 |
| 2026-01-08 | demo0004 | 210,000 | 14,500 | 60,000 | 340,000 | 58% | $67.30 | sonnet=$67.30 |

Run `token-ledger.py --report` (or `/cost` inside Claude Code) to print an
overview instead: total spend, sessions logged, spend by tier, the most
expensive sessions, and whether the delegation alarm has fired.

Example output (synthetic data -- the four demo rows above, run through
`--report` unmodified; this is real output from the script, not a mockup):

```
$ python3 token-ledger.py --report
Ledger: ~/.claude/cost-ledger.md
Sessions: 4   Total: $91.00   Avg/session: $22.75

By tier:
  sonnet   $75.59  (83%)
  opus     $14.02  (15%)
  haiku    $1.39  (2%)

Most expensive sessions:
  2026-01-08  demo0004  $67.30
  2026-01-07  demo0003  $18.60
  2026-01-05  demo0001  $4.12

Delegation alarms logged: 1 (see ~/.claude/cost-alarms.log)
```

`demo0004` is there specifically to trip the delegation alarm (single-tier
cost over the default $50 threshold) so the report's last line has
something to show -- `demo0001` through `demo0003` alone never cross it.

## The delegation alarm

`model-guard.py` blocks a subagent spawn with no explicit model at the
moment it's about to happen. `token-ledger.py` adds a second, softer check
after the fact: if a session costs more than a threshold and one model tier
(whichever one actually dominated that session's main transcript, not a
name hardcoded in the script) accounts for more than a threshold share of
the total, it appends a line to a log file. It does not block anything --
by the time it fires, the session is already over. It's a prompt to look,
not a verdict: plenty of sessions legitimately run deep, expensive work on
one tier the whole time, and that's a fine outcome, not a violation.

## Configuration

All environment variables, all optional:

| Variable | Default | What it does |
|---|---|---|
| `CLAUDE_COST_LEDGER` | `~/.claude/cost-ledger.md` | Where the per-session ledger is written. |
| `CLAUDE_COST_ALARM_USD` | `50` | Minimum session cost (USD) before the delegation alarm is even considered. |
| `CLAUDE_COST_ALARM_SHARE` | `0.60` | Share of a session's cost on its dominant tier that trips the alarm. |
| `CLAUDE_COST_ALARM_LOG` | `~/.claude/cost-alarms.log` | Where `model-guard.py` logs blocks/warnings and where the delegation alarm appends its lines. |
| `CLAUDE_COST_ERROR_LOG` | `~/.claude/cost-audit-errors.log` | Best-effort log for unexpected parse failures; the hook itself never raises. |
| `CLAUDE_COST_NO_ALARM` | unset | Set to any value to recompute a ledger row without re-triggering its delegation alarm -- useful when backfilling old transcripts so a corrected row doesn't inject a stale "alarm" nobody will review. |

## Caveats

- **Prices are hardcoded, as of 2026-07, and will go stale.** The price
  table lives at the top of `token-ledger.py` as a plain constant. Nothing
  fetches current pricing. Check it against Anthropic's published pricing
  or your own Console billing before trusting a dollar figure this script
  prints, and update it yourself when prices change.
- **This estimates cost from transcript token counts. It is not a billing
  source of truth.** Reconcile against the Anthropic Console for real
  numbers. Treat everything this tool prints as an estimate for spotting
  trends, not as an invoice.
- **Tier matching is a substring match** against the model id string found
  in the transcript (see `tier()` in `token-ledger.py`). If your account
  uses a model id or alias that doesn't contain `opus`, `sonnet`, or
  `haiku`, add a row to the price table -- an unmatched model is silently
  skipped, not guessed at.
- **The recursive subagent glob assumes the standard Claude Code
  project/session transcript layout.** It derives the projects root from
  the transcript path it's handed, so a non-default `CLAUDE_CONFIG_DIR`
  should still work, but this hasn't been tested against every possible
  installation layout.

## Related

This plugin is a narrow extraction from a larger personal Claude Code setup
(tiered memory, delegation rules, headless routines, more slash commands).
The fuller version is at
[github.com/jasonpalmer1/claude-code-setup](https://github.com/jasonpalmer1/claude-code-setup)
if any of this is useful and you want the rest of it.
