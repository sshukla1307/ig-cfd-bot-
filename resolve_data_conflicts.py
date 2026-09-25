#!/usr/bin/env python3
"""
Auto-resolves the three known-safe classes of git rebase conflict in this
repo's data files, so a genuine two-run race (e.g. a manual workflow_dispatch
overlapping the every-2-minute "watch" cron) doesn't lose an entire tick's
audit-log commit to a conflict abort -- which is exactly the failure the
fetch+rebase+retry logic in cfd_trading.yml was built to prevent in the
first place, just not far enough: it correctly detects "this isn't just a
race, there's a real conflict" and aborts rather than corrupting state, but
every conflict data/ can ever produce is actually one of these three
semantically-safe kinds, never a real one.

*** INCIDENT (2026-09-25): data/trailing_peaks.json (added when the
continuous trailing-stop scheme shipped) wasn't in either of the original
two classes, so a real race on it correctly fell through to the "leave
conflict markers, abort" path -- exactly as designed for a genuinely
unhandled file, but it cost that specific run its entire tick's audit-log
commit (the ephemeral runner tore down with the commit never pushed).
Added a third class below for it. ***

Three conflict classes, resolved differently:

1. data/order_log.jsonl and data/equity_history.jsonl are pure APPEND-ONLY
   logs -- every real tick only ever adds new lines, never edits or removes
   existing ones. Git's default text-based conflict detection doesn't know
   this, and flags two independent appends to the same file as conflicting
   even though there's nothing to actually reconcile: the correct merge is
   simply "every line either side added, in the order each side wrote it."
   Resolved here by reading all three merge stages (:1: common ancestor,
   :2: ours -- during a REBASE this is the branch being rebased ONTO, i.e.
   origin/main, :3: theirs -- the commit being replayed, i.e. this run's own
   new tick) and concatenating: the common prefix, then whatever lines ours
   added past that prefix, then whatever lines theirs added past that
   prefix. This is real audit data, so nothing is ever discarded -- both
   sides' new lines survive, unlike the abort path which loses theirs
   entirely.

2. data/dashboard/*.json are NOT history -- they're a full-overwrite
   snapshot of "current state as of whichever run wrote them last",
   regenerated from scratch every tick (see dashboard_exporter.py). Any two
   ticks racing on these will conflict on nearly every line since the whole
   file differs, even though there's no real information to lose -- the
   losing snapshot is stale again within one more tick cycle regardless of
   which side wins. Resolved here by simply keeping "theirs" (this run's
   own, freshly-generated-this-tick version) -- correctness of the
   dashboard doesn't depend on which side wins, only on not aborting the
   entire audit-log commit over it.

3. data/trailing_peaks.json is a {deal_id: peak_favorable_pct} map, fully
   rewritten every sync call (see cfd_runner._save_trailing_peaks) -- so like
   the dashboard files, a race conflicts on nearly the whole file. UNLIKE the
   dashboard files, it isn't purely cosmetic: it's the one-way-ratchet memory
   for the trailing stop, and each value only ever increases for a given
   deal_id (a real peak, once reached, is never un-reached). Picking "theirs"
   outright, like the dashboard resolution, risks silently reverting a
   deal_id's peak to a lower value if "ours" had already recorded a higher
   one this cycle -- not catastrophic (the live stop_level on IG is a
   separately-persisted value the one-way ratchet in
   _sync_margin_based_exits still protects independently of this file, so a
   reverted peak can't loosen an already-tightened real stop), but it could
   delay how much a subsequent tick locks in. The actually-correct merge is
   a per-key MAX across both sides (and a union of keys) -- resolved here by
   parsing both JSON dicts and taking max(ours.get(k, 0), theirs.get(k, 0))
   for every key in either.

Exits 0 if every conflicted file was one of the three known-safe classes
above and got resolved (staged via `git add`, ready for `git rebase
--continue`). Exits 1 -- leaving conflict markers in place -- if ANY
conflicted file isn't one of these (or a resolution attempt itself looks
unsafe, e.g. one side rewrote history instead of purely appending), so the
caller in cfd_trading.yml still aborts the rebase rather than guessing at
an unanticipated conflict.
"""
import json
import subprocess
import sys

APPEND_ONLY_LOGS = {"data/order_log.jsonl", "data/equity_history.jsonl"}
REGENERATED_SNAPSHOTS = {
    "data/dashboard/equity.json",
    "data/dashboard/positions.json",
    "data/dashboard/trades.json",
    "data/dashboard/last_updated.json",
}
PEAK_STATE_FILES = {"data/trailing_peaks.json"}


def _run(args):
    return subprocess.run(args, capture_output=True, text=True)


def _conflicted_files():
    result = _run(["git", "diff", "--name-only", "--diff-filter=U"])
    return [f for f in result.stdout.splitlines() if f.strip()]


def _show_stage(stage, path):
    result = _run(["git", "show", f":{stage}:{path}"])
    if result.returncode != 0:
        return None
    return result.stdout


def _resolve_append_only(path: str) -> bool:
    base = _show_stage(1, path)
    ours = _show_stage(2, path)
    theirs = _show_stage(3, path)
    if ours is None or theirs is None:
        return False  # one side deleted the file -- unexpected, don't guess

    base_lines = base.splitlines() if base is not None else []
    ours_lines = ours.splitlines()
    theirs_lines = theirs.splitlines()

    # Confirm this really is a pure append past the common ancestor on BOTH
    # sides before trusting the concatenation below -- if either side edited
    # or removed an existing line, this isn't a safe auto-merge, bail out.
    if ours_lines[:len(base_lines)] != base_lines or theirs_lines[:len(base_lines)] != base_lines:
        return False

    ours_new = ours_lines[len(base_lines):]
    theirs_new = theirs_lines[len(base_lines):]
    merged_lines = base_lines + ours_new + theirs_new
    merged = "\n".join(merged_lines) + ("\n" if merged_lines else "")

    with open(path, "w") as f:
        f.write(merged)
    _run(["git", "add", path])
    return True


def _resolve_regenerated_snapshot(path: str) -> bool:
    theirs = _show_stage(3, path)
    if theirs is None:
        return False
    with open(path, "w") as f:
        f.write(theirs)
    _run(["git", "add", path])
    return True


def _resolve_peak_state(path: str) -> bool:
    ours = _show_stage(2, path)
    theirs = _show_stage(3, path)
    if ours is None or theirs is None:
        return False  # one side deleted the file -- unexpected, don't guess
    try:
        ours_dict = json.loads(ours) if ours.strip() else {}
        theirs_dict = json.loads(theirs) if theirs.strip() else {}
    except json.JSONDecodeError:
        return False  # not the {deal_id: float} shape we expect -- don't guess
    if not isinstance(ours_dict, dict) or not isinstance(theirs_dict, dict):
        return False

    merged = dict(ours_dict)
    for deal_id, theirs_peak in theirs_dict.items():
        merged[deal_id] = max(merged.get(deal_id, 0.0), theirs_peak)

    with open(path, "w") as f:
        json.dump(merged, f)
    _run(["git", "add", path])
    return True


def main() -> int:
    conflicted = _conflicted_files()
    if not conflicted:
        print("No conflicted files found -- nothing to resolve.")
        return 0

    unresolved = []
    for path in conflicted:
        if path in APPEND_ONLY_LOGS:
            ok = _resolve_append_only(path)
        elif path in REGENERATED_SNAPSHOTS:
            ok = _resolve_regenerated_snapshot(path)
        elif path in PEAK_STATE_FILES:
            ok = _resolve_peak_state(path)
        else:
            ok = False

        if ok:
            print(f"Auto-resolved (known-safe): {path}")
        else:
            unresolved.append(path)

    if unresolved:
        print(f"Could not auto-resolve -- leaving conflict markers in place: {unresolved}")
        return 1

    print("All conflicted files were known-safe data files -- auto-resolved.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
