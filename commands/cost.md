---
description: Review the cost ledger and report spend trends
---

Run the ledger's report mode:

    python3 "$CLAUDE_PLUGIN_ROOT/hooks/token-ledger.py" --report

Read its output and report back, in plain text:

1. Total estimated spend and total sessions logged.
2. Average cost per session, and the 3 most expensive sessions.
3. Spend by model tier and each tier's share of the total.
4. **Delegation ratio** -- how much of the spend sits on cheaper tiers versus
   the main-loop tier. A high main-loop share isn't automatically a problem
   -- deliberate, deep work legitimately runs there -- but it's worth naming
   out loud so it's a choice, not a default.
5. Whether the delegation-alarm log (`CLAUDE_COST_ALARM_LOG`, default
   `~/.claude/cost-alarms.log`) has any entries, and what they say.
6. One concrete observation grounded in what the data actually shows. Don't
   invent a trend from a single session.

If the ledger doesn't exist yet (`CLAUDE_COST_LEDGER`, default
`~/.claude/cost-ledger.md`), say so plainly -- it populates as sessions end
via the SessionEnd hook, or when `token-ledger.py` is run manually against a
transcript file.

Keep it short and decision-oriented. This is a measurement tool, not a
billing source of truth -- for actual charges, point back to the Anthropic
Console.
