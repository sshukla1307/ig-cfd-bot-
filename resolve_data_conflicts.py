#!/usr/bin/env python3
"""
Auto-resolves the two known-safe classes of git rebase conflict in this
repo's data files, so a genuine two-run race (e.g. a manual workflow_dispatch
overlapping the every-2-minute "watch" cron) doesn't lose an entire tick's
audit-log commit to a conflict abort -- which is exactly the failure the
fetch+rebase+retry logic in cfd_trading.yml was built to prevent in the
first place, just not far enough: it correctly detects "this isn't just a
race, there's a real conflict" and aborts rather than corrupting state, but
every conflict data/ can ever produce is actually one of these two
semantically-safe kinds, never a real one.

Two conflict classes, resolved differently:

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

Exits 0 if every conflicted file was one of the two known-safe classes
above and got resolved (staged via `git add`, ready for `git rebase
--continue`). Exits 1 -- leaving conflict markers in place -- if ANY
conflicted file isn't one of these (or a resolution attempt itself looks
unsafe, e.g. one side rewrote history instead of purely appending), so the
caller in cfd_trading.yml still aborts the rebase rather than guessing at
an unanticipated conflict.
"""
import subprocess
import sys

APPEND_ONLY_LOGS = {"data/order_log.jsonl", "data/equity_history.jsonl"}
REGENERATED_SNAPSHOTS = {
    "data/dashboard/equity.json",
    "data/dashboard/positions.json",
    "data/dashboard/trades.json",
    "data/dashboard/last_updated.json",
}


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
