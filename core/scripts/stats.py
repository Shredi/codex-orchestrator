#!/usr/bin/env python3
"""Summarize the fable-orchestrator metrics log.

Usage:
    python3 scripts/stats.py [path]

Default path: ~/.claude/fable-orch/metrics.jsonl (written by the hooks;
disable collection with FABLE_ORCH_METRICS=0).
"""
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone


def records(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except ValueError:
                continue


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.expanduser("~"), ".claude", "fable-orch", "metrics.jsonl")
    if not os.path.isfile(path):
        print(f"no metrics yet: {path}")
        return

    per_day = defaultdict(Counter)
    events = Counter()
    profiles = Counter()
    ledgers = Counter()
    swarm_reaped = 0
    panes_reaped = 0
    # Cold-cache guard: tokens and list-price cost per band. A block whose
    # ack never came is context the session did NOT re-write; an ack is
    # the user deciding to pay it anyway.
    #
    # Counted per DISTINCT context, not per event: one session that walks
    # away, comes back cold, gets blocked, walks away again and is blocked
    # again logs two cold_block events for the same unwritten context —
    # summing those would claim twice the re-cache was avoided.
    cold_tokens = Counter()
    cold_usd = defaultdict(float)
    cold_distinct = Counter()
    cold_seen = set()

    for rec in records(path):
        event = rec.get("event") or "?"
        events[event] += 1
        try:
            day = datetime.fromtimestamp(
                float(rec.get("ts") or 0), tz=timezone.utc).strftime("%Y-%m-%d")
        except (ValueError, OSError, OverflowError):
            day = "?"
        per_day[day][event] += 1
        if event == "inject":
            profiles[rec.get("profile") or rec.get("model") or "?"] += 1
        if event == "stop_block":
            ledgers[rec.get("ledger") or "?"] += 1
        if event == "cleanup":
            try:
                swarm_reaped += int(rec.get("swarm_own") or 0)
                swarm_reaped += int(rec.get("swarm_stale") or 0)
            except (TypeError, ValueError):
                pass
        if event in ("cold_block", "cold_warn", "cold_ack"):
            try:
                ctx = int(rec.get("ctx_tokens") or 0)
                key = (event, rec.get("session"), ctx)
                if key not in cold_seen:
                    cold_seen.add(key)
                    cold_distinct[event] += 1
                    cold_tokens[event] += ctx
                    cold_usd[event] += float(rec.get("est_usd") or 0)
            except (TypeError, ValueError):
                pass
        if event == "teammate_reap":
            try:
                panes_reaped += int(rec.get("killed") or 0)
            except (TypeError, ValueError):
                pass

    print(f"metrics: {path}\n")
    print("== events per day ==")
    for day in sorted(per_day):
        parts = ", ".join(f"{k}={v}" for k, v in sorted(per_day[day].items()))
        print(f"{day}  {parts}")

    print("\n== totals ==")
    for name, count in sorted(events.items()):
        print(f"{name:26} {count}")

    if profiles:
        print("\n== sessions by profile/model (inject events) ==")
        for name, count in profiles.most_common():
            print(f"{name:8} {count}")

    denies = events.get("spawn_deny", 0)
    passes = events.get("spawn_pass_over_threshold", 0)
    if denies or passes:
        print(f"\nover-threshold spawns: {passes} passed with a ledger, {denies} denied")

    tdenies = events.get("tasks_deny", 0)
    tsupp = events.get("tasks_suppressed", 0)
    if tdenies or tsupp:
        print(f"\nsolo multi-phase nudges: {tdenies} denied, "
              f"{tsupp} further ledgerless tasks after the reminder")

    switches = events.get("inject_switch", 0)
    if switches:
        # Deliberately NOT folded into the profile counter above: that
        # one counts sessions, and a switch is the same session moving
        # tiers mid-flight.
        print(f"\nmid-session profile switches: {switches} "
              f"(short delta injected, not the full core)")

    blocks = cold_distinct.get("cold_block", 0)
    warns = cold_distinct.get("cold_warn", 0)
    acks = cold_distinct.get("cold_ack", 0)
    if blocks or warns or acks:
        print("\n== cold cache (idle resumes over the 1h cache TTL) ==")
        for name in ("cold_block", "cold_warn", "cold_ack"):
            if cold_distinct.get(name):
                n = cold_distinct[name]
                repeats = events.get(name, 0) - n
                extra = f" (+{repeats} repeat)" if repeats else ""
                label = f"{n} context{'' if n == 1 else 's'}{extra}"
                print(f"{name:11} {label:24}"
                      f"{cold_tokens[name] / 1000.0:8.0f}k ctx tokens  "
                      f"~${cold_usd[name]:.2f} list")
        # Each ack answers one earlier block, so the avoided figure is the
        # blocked cost minus what was then acked through. Never negative:
        # an ack can outlive its block's retention in this log.
        avoided = max(0.0, cold_usd["cold_block"] - cold_usd["cold_ack"])
        print(f"re-caches avoided: {max(0, blocks - acks)} of {blocks} blocked "
              f"contexts never acked, ~${avoided:.2f} list not re-written "
              f"(~${cold_usd['cold_ack']:.2f} acked through)")
        stamp_failed = events.get("cold_stamp_failed", 0)
        if stamp_failed:
            # The guard passed instead of blocking: without a writable
            # marker it cannot offer the re-send escape hatch.
            print(f"blocks suppressed (marker unwritable): {stamp_failed}")

    if swarm_reaped:
        print(f"\ntmux teammate servers reaped: {swarm_reaped}")
    if panes_reaped:
        print(f"idle teammate panes reaped: {panes_reaped}")

    if ledgers:
        print("\n== stop blocks by ledger ==")
        for name, count in ledgers.most_common(5):
            print(f"{count:5}  {name}")


if __name__ == "__main__":
    main()
