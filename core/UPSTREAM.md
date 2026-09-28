# Upstream

This repo is a fork of [Rylaa/fable5-orchestrator](https://github.com/Rylaa/fable5-orchestrator), split at `828974b` (v0.15.0). The two have diverged: upstream v0.16 removed the Requirements Ledger and the mandatory verification phase, which remain core here. So upstream is **reviewed and ported by hand, never merged**. Because of that, `git log HEAD..upstream/main` lists ported commits as unmerged forever. This file records what was taken, what was skipped and why, so the next review starts where the last one ended.

## Last reviewed

`93a19c9` (upstream v0.16.2, 2026-09-22), reviewed 2026-09-25.

```bash
git remote add upstream https://github.com/Rylaa/fable5-orchestrator   # once
git fetch upstream
git log --oneline 93a19c9..upstream/main                             # what's new since
```

After a review, update the commit above and add rows below.

## Taken

| Upstream | What | Landed |
|---|---|---|
| `e02be85` (v0.15.1) | Profile-switch delta only on an authoritative signal (payload model or env pin, never the global settings default or the sticky marker model). A fire that delivers nothing keeps the recorded profile only on `resume`. `from_profile` + `fire` on gated `inject` metrics. | 0.19.1, #13 |
| `e02be85` (v0.15.1) | Restored core clauses: fork exemption from the gates, "dodging tracker tasks IS the violation", `Workflow` tool only on an explicit user ask. | 0.19.1, #13 |
| `e02be85` (v0.15.1) | conftest PATH-shim directory removed at exit. | 0.19.1, #13 |
| `90d5c97` | README names Opus 5.5 as the hard-slice tier. | 0.19.1, #13 |

## Skipped

| Upstream | What | Why not |
|---|---|---|
| `f9093b4` (v0.16.0) | Remove the Requirements Ledger, its three gates and mandatory fresh-eyes verification. | Different direction: the ledger system is core to this fork. |
| `f9093b4` (v0.16.0) | Solo guard: deny once at the Nth chair file edit in a session with no worker spawned. | Measured in our transcripts: 3 of 90 chair sessions edited 4+ files with no spawn. Too rare to justify the friction. |
| `f9093b4` (v0.16.0) | Per-prompt reminder hook (`remind_chair.py`). | Costs tokens on every prompt; not wanted. |
| `0023c6b` (v0.16.1) | Deny a long (1500+ char) spawn without a `name`, once per session. | Measured: 323 of 368 long briefs set no `name` (the chair names the worker inside the brief instead), so workers run as background subagents. That is accepted. The direction is specialized agents from the project roster instead. |
| `93a19c9` (v0.16.2) | Opus capped at `xhigh` effort. | Doesn't apply: our cores state effort is not selectable per spawn. |
| `e02be85` (v0.15.1) | OPUS profile: which tier reruns a declined task. | Already covered by the playbook Declines rule and "a second decline STOPS the work". |

## Revisit if

- **Solo guard:** the solo share (script 1) reaches roughly 10% of chair sessions.
- **Naming guard:** you want workers visible in tmux panes and steerable via SendMessage. Re-run script 2 with `MIN = 0`: as specialized agents shorten briefs, the 1500-char cut would hide unnamed spawns rather than show fewer of them.

<details>
<summary>Measurement scripts (read-only, run locally against Claude Code transcripts)</summary>

Script 1: chair sessions that edited 4+ files with no worker spawned.

```bash
python3 - <<'EOF'
import json, glob, os
chair = solo = 0
for f in glob.glob(os.path.expanduser("~/.claude/projects/*/*.jsonl")):
    txt = open(f, encoding="utf-8", errors="ignore").read()
    if "Rule 0 — threshold" not in txt:   # only chairs that received our profile
        continue
    files, spawns = set(), 0
    for line in txt.splitlines():
        try: rec = json.loads(line)
        except Exception: continue
        if rec.get("isSidechain"): continue
        for c in (rec.get("message") or {}).get("content") or []:
            if not isinstance(c, dict) or c.get("type") != "tool_use": continue
            n, i = c.get("name"), c.get("input") or {}
            if n in ("Agent", "Task", "Workflow") and i.get("subagent_type") != "fork": spawns += 1
            if n in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
                files.add(i.get("file_path") or i.get("notebook_path"))
    chair += 1
    if spawns == 0 and len(files) >= 4: solo += 1
print(f"chair sessions: {chair}; >=4 files edited with 0 spawns: {solo}")
EOF
```

Script 2: spawns whose brief is at least `MIN` chars and that set no `name`.

```bash
python3 - <<'EOF'
import json, glob, os
MIN = 1500
spawns = long_ = unnamed = 0
examples = []
for f in glob.glob(os.path.expanduser("~/.claude/projects/*/*.jsonl")):
    txt = open(f, encoding="utf-8", errors="ignore").read()
    if "Rule 0 — threshold" not in txt:
        continue
    for line in txt.splitlines():
        try: rec = json.loads(line)
        except Exception: continue
        if rec.get("isSidechain"): continue
        for c in (rec.get("message") or {}).get("content") or []:
            if not isinstance(c, dict) or c.get("type") != "tool_use": continue
            if c.get("name") not in ("Agent", "Task"): continue
            i = c.get("input") or {}
            if i.get("subagent_type") == "fork": continue
            spawns += 1
            p = str(i.get("prompt") or "")
            if len(p) >= MIN:
                long_ += 1
                if not str(i.get("name") or "").strip():
                    unnamed += 1
                    examples.append((len(p), os.path.basename(f), p[:80].replace("\n", " ")))
print(f"spawns: {spawns}; briefs >={MIN} chars: {long_}; of those unnamed: {unnamed}")
for e in sorted(examples, reverse=True)[:5]: print(*e, sep=" | ")
EOF
```

Results on 2026-09-25: script 1 found 90 chair sessions, 3 solo. Script 2 found 458 spawns, 368 briefs of 1500+ chars, 323 of those unnamed.

</details>
