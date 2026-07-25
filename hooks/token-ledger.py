#!/usr/bin/env python3
"""Cost ledger: parse a Claude Code session transcript and write a usage row.

Pure parsing -- no model call, costs nothing to run. Sums tokens per model
from each assistant message's usage block, estimates cost with the price
table below, and writes one Markdown table row per session to the ledger
file (see CLAUDE_COST_LEDGER below).

A session that is resumed and ends again later re-parses its (now longer)
transcript from scratch and UPDATES its existing row in place (same line,
fresh cumulative totals) rather than appending a duplicate -- /cost and any
other consumer assume exactly one row per session id.

The write is atomic (temp file + os.replace) so a crash or kill mid-write
can never truncate or corrupt the live ledger. Any failure is caught and
appended to the error log instead of raising, so the SessionEnd hook never
blocks on this.

Invoked by the SessionEnd hook with the transcript path on stdin (hook JSON)
or as argv[1]. Safe to run manually:

    token-ledger.py <transcript.jsonl>     # parse one transcript, write a row
    token-ledger.py --report               # print an overview of the ledger
"""
import json, sys, os, glob, datetime, traceback

# ---------------------------------------------------------------------------
# PRICE TABLE -- $ per million tokens: (input, output, cache_write_5m, cache_read)
#
# Prices are current AS OF 2026-07 and WILL drift. Check them against the
# live Anthropic pricing page or your own Console billing page before
# trusting any dollar figure this script prints -- nothing here is fetched
# live, it's a static table someone has to keep up to date.
#
# Tier is matched by substring against the model id string found in each
# transcript line (see tier()), so add a row here for any model or account
# alias not already covered. A model that matches no key here is skipped
# entirely, not guessed at -- see accumulate().
# ---------------------------------------------------------------------------
PRICES = {
    "opus":   (5.00, 25.00, 6.25, 0.50),
    "sonnet": (3.00, 15.00, 3.75, 0.30),
    "haiku":  (1.00,  5.00, 1.25, 0.10),
}

# ---------------------------------------------------------------------------
# Output locations -- all overridable via environment variable, all default
# to a plain path under ~/.claude with no per-user or per-machine detail
# baked in.
# ---------------------------------------------------------------------------
LEDGER = os.path.expanduser(
    os.environ.get("CLAUDE_COST_LEDGER") or "~/.claude/cost-ledger.md"
)
ERROR_LOG = os.path.expanduser(
    os.environ.get("CLAUDE_COST_ERROR_LOG") or "~/.claude/cost-audit-errors.log"
)
ALARM_LOG = os.path.expanduser(
    os.environ.get("CLAUDE_COST_ALARM_LOG") or "~/.claude/cost-alarms.log"
)

# Delegation-alarm thresholds (see the alarm block in main() for what these
# gate): fire only for sessions costing more than ALARM_USD dollars, and
# only when the main-loop tier's share of that cost exceeds ALARM_SHARE.
ALARM_USD = float(os.environ.get("CLAUDE_COST_ALARM_USD") or 50)
ALARM_SHARE = float(os.environ.get("CLAUDE_COST_ALARM_SHARE") or 0.60)

HEADER = [
    "# Cost Ledger\n",
    "\n",
    "Per-session usage, appended by the SessionEnd hook (`token-ledger.py`). "
    "Pure parsing of the transcript -- no model call. Review with `/cost`.\n",
    "\n",
    "| Date | Session | Input | Output | CacheWrite | CacheRead | HitRate | Est.Cost | By model |\n",
    "|------|---------|-------|--------|-----------|-----------|---------|----------|----------|\n",
]

def tier(model: str):
    m = (model or "").lower()
    for key in PRICES:
        if key in m:
            return key
    return None

def read_transcript_path() -> str:
    if len(sys.argv) > 1:
        return sys.argv[1]
    data = sys.stdin.read()
    if not data.strip():
        return ""
    try:
        return json.loads(data).get("transcript_path", "")
    except Exception:
        return data.strip()

def accumulate(path, acc, models):
    with open(path, errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            # Per-line fault isolation: one structurally-bad entry must skip,
            # not zero out the whole session's row or trip a false alarm.
            try:
                msg = obj.get("message") or {}
                usage = msg.get("usage")
                if not usage:
                    continue
                t = tier(msg.get("model", ""))
                if not t:
                    continue
                models.add(msg.get("model"))
                a = acc.setdefault(t, [0, 0, 0, 0])
                a[0] += int(usage.get("input_tokens") or 0)
                a[1] += int(usage.get("output_tokens") or 0)
                a[2] += int(usage.get("cache_creation_input_tokens") or 0)
                a[3] += int(usage.get("cache_read_input_tokens") or 0)
            except Exception:
                continue

def log_error(msg):
    # Best-effort diagnostics -- must never itself raise or block the hook.
    try:
        os.makedirs(os.path.dirname(ERROR_LOG), exist_ok=True)
        ts = datetime.datetime.now().isoformat(timespec="seconds")
        with open(ERROR_LOG, "a") as f:
            f.write(f"[{ts}] token-ledger.py: {msg}\n")
    except Exception:
        pass

def projects_root(tp):
    """The Claude Code projects directory this transcript lives under,
    derived from the transcript path itself rather than assumed to be
    ~/.claude/projects. A transcript path looks like
    <projects-root>/<project-slug>/<session-id>.jsonl, so walking up two
    levels finds the right root under any home directory, username, or
    CLAUDE_CONFIG_DIR -- nothing here is hardcoded to one machine."""
    return os.path.dirname(os.path.dirname(os.path.abspath(tp)))

def agent_transcripts(tp, sid_full):
    """Every subagent transcript belonging to this session, wherever it landed.

    A naive glob for subagent transcripts looks like:

        <dir(tp)>/<session-id>/subagents/*.jsonl

    one level deep, in the one project directory the main transcript lives
    in. That silently drops two real layouts:

      * Workflow-spawned agents nest one level deeper:
        <session-id>/subagents/workflows/wf_*/agent-*.jsonl
      * A session that `cd`s into a project directory partway through gets
        a SECOND project directory, keyed by that project's own slug:
        <projects-root>/<other-project-slug>/<session-id>/...

    Both cases under-report the session's real agent spend, and because
    subagents usually run on cheaper tiers than the main loop, dropping
    them skews any delegation ratio computed from the ledger PESSIMISTIC --
    it makes delegation look worse than it actually was. The fix is
    recursive, checks every project directory this session id appears
    under, and dedupes by realpath.
    """
    main_rp = os.path.realpath(tp)
    root = projects_root(tp)
    seen, out = set(), []
    for session_dir in glob.glob(os.path.join(root, "*", sid_full)):
        for p in glob.iglob(os.path.join(session_dir, "**", "*.jsonl"), recursive=True):
            # journal.jsonl is a workflow's own bookkeeping file -- no usage blocks.
            if os.path.basename(p) == "journal.jsonl":
                continue
            rp = os.path.realpath(p)
            if rp == main_rp or rp in seen:
                continue
            seen.add(rp)
            out.append(rp)
    return sorted(out)

def build_row(tp, sid_full):
    """Re-parse the FULL transcript (+ any subagent transcripts) from scratch.
    For a resumed session this naturally recomputes cumulative totals across
    the whole (now longer) history -- not just the new increment."""
    acc, models = {}, set()
    # Main transcript first, kept separately: whichever tier dominates HERE
    # is the main-loop model, which is what the delegation alarm measures.
    # Derived, never hardcoded -- the main-loop model can change across
    # sessions and the alarm must not care which one holds that role.
    main_acc = {}
    accumulate(tp, main_acc, models)
    for t, v in main_acc.items():
        acc[t] = list(v)
    for sub in agent_transcripts(tp, sid_full):
        accumulate(sub, acc, models)
    if not acc:
        return None
    tot_in = tot_out = tot_cw = tot_cr = cost = 0
    per_tier_cost = {}
    for t, (i, o, cw, cr) in acc.items():
        pi, po, pcw, pcr = PRICES[t]
        c = (i * pi + o * po + cw * pcw + cr * pcr) / 1_000_000
        per_tier_cost[t] = c
        cost += c
        tot_in += i; tot_out += o; tot_cw += cw; tot_cr += cr
    # cache hit rate = cache_read / (cache_read + cache_creation + input)
    denom = tot_cr + tot_cw + tot_in
    hit = (tot_cr / denom * 100) if denom else 0
    # use transcript mtime, not today -- correct for backfills; identical for live runs
    date = datetime.date.fromtimestamp(os.path.getmtime(tp)).isoformat()
    sess = sid_full[:8]
    mix = " ".join(
        f"{t}=${per_tier_cost[t]:.2f}" for t in sorted(per_tier_cost)
    )
    row = (
        f"| {date} | {sess} | {tot_in:,} | {tot_out:,} | {tot_cw:,} | "
        f"{tot_cr:,} | {hit:.0f}% | ${cost:.2f} | {mix} |\n"
    )
    # Costliest tier in the MAIN transcript = the main-loop model this
    # session ran on. Model-agnostic on purpose: it measures whichever tier
    # actually dominates the main transcript, not a hardcoded model name.
    main_cost = {
        t: sum(n * p for n, p in zip(v, PRICES[t])) / 1_000_000
        for t, v in main_acc.items()
    }
    main_tier = max(main_cost, key=main_cost.get) if main_cost else None
    return sess, row, cost, per_tier_cost, date, main_tier

def write_row(sess, row):
    """Update the row for `sess` in place if it already exists (same line,
    refreshed totals); otherwise append a new row. Written atomically via
    temp file + os.replace so a mid-write failure can't truncate the ledger."""
    if os.path.exists(LEDGER):
        with open(LEDGER) as f:
            lines = f.readlines()
        if not lines:
            lines = list(HEADER)
    else:
        os.makedirs(os.path.dirname(LEDGER), exist_ok=True)
        lines = list(HEADER)
    marker = f"| {sess} |"
    idx = next((i for i, l in enumerate(lines) if marker in l), None)
    if idx is not None:
        lines[idx] = row          # resumed/re-ended session -- refresh in place
    else:
        lines.append(row)         # brand-new session -- append
    tmp = f"{LEDGER}.tmp-{os.getpid()}"
    with open(tmp, "w") as f:
        f.writelines(lines)
    os.replace(tmp, LEDGER)        # atomic on POSIX -- never a truncated ledger

def parse_ledger():
    """Read back the rows this script itself wrote, for --report."""
    if not os.path.exists(LEDGER):
        return []
    rows = []
    with open(LEDGER) as f:
        for line in f:
            line = line.rstrip("\n")
            if not line.startswith("|") or line.startswith("| Date") or set(line.replace("|", "").strip()) <= {"-"}:
                continue
            cols = [c.strip() for c in line.strip("|").split("|")]
            if len(cols) != 9:
                continue
            date, sess, tin, tout, tcw, tcr, hit, cost, mix = cols
            try:
                cost_f = float(cost.lstrip("$").replace(",", ""))
            except ValueError:
                continue
            tiers = {}
            for part in mix.split():
                if "=$" in part:
                    k, v = part.split("=$", 1)
                    try:
                        tiers[k] = float(v)
                    except ValueError:
                        pass
            rows.append({"date": date, "session": sess, "cost": cost_f, "tiers": tiers, "hit": hit})
    return rows

def report():
    """--report: print a ledger overview to stdout instead of writing anything."""
    rows = parse_ledger()
    print(f"Ledger: {LEDGER}")
    if not rows:
        print("No sessions logged yet -- this populates as sessions end via the")
        print("SessionEnd hook, or when token-ledger.py is run manually against a")
        print("transcript file.")
        return
    total = sum(r["cost"] for r in rows)
    n = len(rows)
    avg = total / n if n else 0
    print(f"Sessions: {n}   Total: ${total:,.2f}   Avg/session: ${avg:,.2f}")

    tier_totals = {}
    for r in rows:
        for k, v in r["tiers"].items():
            tier_totals[k] = tier_totals.get(k, 0) + v
    if tier_totals:
        print()
        print("By tier:")
        for k in sorted(tier_totals, key=lambda k: -tier_totals[k]):
            share = tier_totals[k] / total * 100 if total else 0
            print(f"  {k:8s} ${tier_totals[k]:,.2f}  ({share:.0f}%)")

    top = sorted(rows, key=lambda r: r["cost"], reverse=True)[:3]
    print()
    print("Most expensive sessions:")
    for r in top:
        print(f"  {r['date']}  {r['session']}  ${r['cost']:.2f}")

    if os.path.exists(ALARM_LOG):
        with open(ALARM_LOG) as f:
            alarms = [l for l in f if l.strip()]
        if alarms:
            print()
            print(f"Delegation alarms logged: {len(alarms)} (see {ALARM_LOG})")

def main():
    if "--report" in sys.argv:
        report()
        return
    tp = read_transcript_path()
    if not tp or not os.path.isfile(tp):
        return
    try:
        sid_full = os.path.basename(tp).replace(".jsonl", "")
        built = build_row(tp, sid_full)
        if built is None:
            return
        sess, row, cost, per_tier, date, main_tier = built
        write_row(sess, row)
        # Delegation alarm: signal, not a block. A session that deliberately
        # runs deep, expensive work on its main-loop tier the whole time is
        # not a bug -- this just surfaces the ratio so a human can judge it.
        # Deduped by session id so a resumed session doesn't re-alarm.
        try:
            main_c = per_tier.get(main_tier, 0) if main_tier else 0
            # CLAUDE_COST_NO_ALARM=1 -> recompute rows only, skip the alarm.
            # Useful when backfilling historical transcripts, so a corrected
            # row can't inject a months-old "alarm" nobody will ever review.
            if os.environ.get("CLAUDE_COST_NO_ALARM"):
                return
            if cost > ALARM_USD and main_c / cost > ALARM_SHARE:
                os.makedirs(os.path.dirname(ALARM_LOG), exist_ok=True)
                existing = ""
                if os.path.exists(ALARM_LOG):
                    with open(ALARM_LOG) as f:
                        existing = f.read()
                if sess not in existing:
                    with open(ALARM_LOG, "a") as f:
                        f.write(
                            f"{date} {sess} main-loop={main_tier} ${main_c:.0f} = "
                            f"{main_c / cost * 100:.0f}% of ${cost:.0f} -- "
                            "intentional deep work, or should more of this delegate?\n"
                        )
        except Exception:
            log_error("delegation-alarm: " + traceback.format_exc(limit=2))
    except Exception:
        log_error(f"{tp}: {traceback.format_exc(limit=4)}")

if __name__ == "__main__":
    main()
