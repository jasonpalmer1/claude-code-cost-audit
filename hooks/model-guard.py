#!/usr/bin/env python3
"""PreToolUse guard: blocks any Agent/Task subagent spawn that doesn't state
its model explicitly, and warns (non-blocking) when a Workflow script spawns
agents with no model option.

The problem this closes: leaving `model` unset on a subagent call silently
inherits whatever model the calling session happens to be running. That's
easy to not notice in the moment and easy to notice on the bill later --
one unspecified spawn is a rounding error, dozens of them in a big session
or a scheduled workflow are not. Choosing a model deliberately, including
one MORE expensive than the caller, is fine; the thing being prevented is
inheriting one by accident.

Exit 2 = block (stderr goes back to Claude, which can retry with a model
set). This hook must never block on its OWN failure -- any unexpected error
here is swallowed and the tool call is allowed through unmodified.
"""
import json, sys, os, datetime, re

LOG = os.path.expanduser(
    os.environ.get("CLAUDE_COST_ALARM_LOG") or "~/.claude/cost-alarms.log"
)

def log(line):
    # Best-effort logging -- must never itself raise or block the hook.
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        ts = datetime.datetime.now().isoformat(timespec="seconds")
        with open(LOG, "a") as f:
            f.write(f"{ts} model-guard: {line}\n")
    except Exception:
        pass

try:
    data = json.load(sys.stdin)
    tool = data.get("tool_name") or ""
    tin = data.get("tool_input") or {}

    if tool == "Workflow":
        # Advisory only: a regex can't reliably tell which agent() calls in
        # an arbitrary script omit opts.model, and a false block would kill
        # a whole workflow run over a false positive.
        script = tin.get("script") or ""
        if script:
            calls = len(re.findall(r"\bagent\s*\(", script))
            models = len(re.findall(r"\bmodel\s*:", script))
            if calls and models < calls:
                log(f"WARN workflow {calls} agent() calls / {models} model: opts")
                print(
                    f"Advisory: this workflow has {calls} agent() call(s) but only "
                    f"{models} explicit model: option(s). Every agent() call without "
                    "opts.model inherits whatever model is running the calling "
                    "session -- set opts.model per stage (any tier, deliberately "
                    "chosen) unless inheritance is what you actually want here.",
                    file=sys.stderr,
                )
        sys.exit(0)

    if not tin.get("model"):
        log(f"BLOCKED {str(tin.get('description', '?'))[:80]}")
        print(
            "Blocked: subagent calls (Agent/Task) must state model: explicitly. "
            "Pick a model by task shape, not by price -- a cheap tier for "
            "read-shaped work (search, extract, summarize, mechanical edits), a "
            "mid tier for code-shaped work (fixes, refactors, tests, configs), and "
            "any tier above the caller's own when a sub-task genuinely needs it. "
            "What's blocked is leaving it unset, which silently inherits the "
            "calling session's model. Re-issue this call with a model parameter.",
            file=sys.stderr,
        )
        sys.exit(2)
except SystemExit:
    raise
except Exception:
    pass
sys.exit(0)
