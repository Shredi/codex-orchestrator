#!/usr/bin/env python3
"""UserPromptSubmit guard: don't silently re-cache a huge, cold context.

MEASURED PROBLEM (audit of 4,238 chair messages, Aug 2026+): 99 messages
— 2.3% of the total — carried 66.1% of every cache-write token the chair
spent, and the first message after a >60-minute idle gap carried 51.4%
of them. Those are not "expensive turns"; they are FULL RE-CACHES. The
session sat in a terminal tab past the 1-hour prompt-cache TTL, the next
message found nothing warm, and the entire 300-800k context was written
again — at the 1h cache-write rate that is roughly 0.4 output tokens of
limit per context token, i.e. a single "quick question" costing more
than an hour of real work.

Why this hook and not SessionStart: the session never restarts. The tab
stays open across sleep/hibernate and the next prompt arrives hours
later in the SAME session, so SessionStart never fires again.
UserPromptSubmit is the only event on that path.

WHAT IT CANNOT KNOW. There is no API that reports prompt-cache state, so
this is a TIME HEURISTIC over the documented 1-hour TTL: idle longer than
FABLE_ORCH_COLD_MIN (default 55 min) is assumed cold. It can be wrong in
both directions — a warm cache blocked (one re-send costs 3 minutes of
patience) or a cold one missed (nothing worse than today).

BANDS, once the gap says "cold":

    ctx >= FABLE_ORCH_COLD_BLOCK_TOKENS (150k)  -> block, with the price
    ctx >= FABLE_ORCH_COLD_WARN_TOKENS  (50k)   -> systemMessage + context
    below                                       -> silent pass

A blocked prompt is never lost to the user: re-sending the same text
within FABLE_ORCH_COLD_ACK_MIN (3 min) passes through. `decision:
"block"` erases the prompt from the model's context but the message
tells the user exactly how to proceed, and Claude Code leaves the typed
text recoverable in the terminal.

FAIL-OPEN EVERYWHERE. Any unreadable payload, marker, or transcript, any
exception at all, means the prompt goes through. A cost guard that can
wedge a session is worse than the cost it saves.

ALWAYS PASSES, no matter how cold or how large:
  * prompts starting with "/" — `/clear` is this hook's own advice, and
    blocking `/exit` or `/compact` would trap the user in the guard
  * empty prompts, teammate sessions, FABLE_ORCH_COLD_GUARD=0
"""
import json
import os
import subprocess
import sys
import tempfile
import time


# --- knobs -----------------------------------------------------------

COLD_MIN_DEFAULT = 55.0        # minutes idle before the 1h cache is "cold"
BLOCK_TOKENS_DEFAULT = 150000  # context at or above this blocks; 0 disables
WARN_TOKENS_DEFAULT = 50000    # context at or above this warns; 0 disables
ACK_MIN_DEFAULT = 3.0          # minutes a block stays acknowledgeable

# Only the TAIL of the transcript is ever read: these files reach tens of
# MB and this hook runs before every single prompt.
TAIL_BYTES = 2 * 1024 * 1024

# List prices per million tokens, used only to turn a token count into
# something a human reacts to. The 1h cache-write rate is what a cold
# resume actually pays (subscription sessions were measured at 100% 1h
# TTL); the output rate converts that into "how much of the limit".
CACHE_WRITE_USD_PER_MTOK = 20.0
OUTPUT_USD_PER_MTOK = 50.0


def _env_float(name, default, minimum=0.0):
    try:
        return max(minimum, float(os.environ[name]))
    except (KeyError, TypeError, ValueError):
        return default


def _env_int(name, default, minimum=0):
    try:
        return max(minimum, int(float(os.environ[name])))
    except (KeyError, TypeError, ValueError):
        return default


# --- the session marker (same file the injector/stop guard use) -------

def session_marker_path(session_id):
    if not session_id:
        return None
    safe = "".join(c for c in str(session_id) if c.isalnum() or c in "-_")
    return os.path.join(tempfile.gettempdir(), f"fable-orch-model-{safe}.json")


def _marker_dict(session_id):
    """The marker as a dict, or None when there is no marker FILE.

    None is load-bearing: this guard then does nothing at all. It must
    not create the file, because marker PRESENCE is what switches the
    write guard from fail-open to enforcing and the stop guard from
    legacy discovery to binding-only — inventing a marker here would
    silently change two other guards' decisions for anyone running a
    partial (manual) install."""
    path = session_marker_path(session_id)
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def stamp(session_id, drop=(), **keys):
    """Read-modify-write the marker atomically, carrying every other key
    forward (`started`, `model`, `profile`, `ledger` — dropping any of
    them would unbind the session or disown its ledgers). Re-reads right
    before writing so a concurrent hook's keys survive.

    Returns True only when the marker actually holds the keys now. The
    block path depends on that answer: the escape hatch it advertises
    ("send another prompt within 3 minutes") lives in this file, so a
    block recorded nowhere would repeat on every prompt until the user
    found `/clear` or the kill switch."""
    marker = _marker_dict(session_id)
    if marker is None:
        return False
    for key in drop:
        marker.pop(key, None)
    for key, value in keys.items():
        marker[key] = round(value, 3) if isinstance(value, float) else value
    path = session_marker_path(session_id)
    tmp = f"{path}.{os.getpid()}.tmp.json"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(marker, f)
        os.replace(tmp, path)
        return True
    except Exception:
        try:
            os.remove(tmp)  # never leave a half-written sibling behind
        except OSError:
            pass
        return False


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _metric(event, session_id=None, **extra):
    """Append one event line to ~/.claude/fable-orch/metrics.jsonl (best effort)."""
    if (os.environ.get("FABLE_ORCH_METRICS") or "").strip() == "0":
        return
    try:
        d = os.path.join(os.path.expanduser("~"), ".claude", "fable-orch")
        os.makedirs(d, exist_ok=True)
        rec = {"ts": round(time.time(), 3), "event": event}
        if session_id:
            rec["session"] = str(session_id)[:8]
        rec.update(extra)
        with open(os.path.join(d, "metrics.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass


# --- teammate detection (verbatim from the other guards) --------------

TEAMMATE_DETECT_BUDGET = 1.5  # seconds; the walk measures ~5ms in practice


def _budget(deadline, cap=5.0):
    """Seconds a subprocess may run without overshooting the deadline.
    Monotonic — a wall clock can step backwards and hand back a budget
    that never expires."""
    if deadline is None:
        return cap
    return max(0.2, min(cap, deadline - time.monotonic()))


def _is_teammate_session(max_hops=12):
    """True when this hook is running inside a named teammate.

    A teammate's context is its own and short-lived; the chair's cost
    story is not its business, and a blocked worker prompt would eat the
    report it was about to deliver. Same ancestor walk as the stop guard
    (`--agent-id` on the nearest claude ancestor), hard-budgeted, and on
    budget exhaustion answering False ("assume chair") — the guard then
    still runs and, at worst, warns a worker."""
    deadline = time.monotonic() + TEAMMATE_DETECT_BUDGET
    pid = os.getpid()
    for _ in range(max_hops):
        if time.monotonic() > deadline:
            return False
        try:
            out = subprocess.run(
                ["ps", "-o", "ppid=,command=", "-p", str(pid)],
                capture_output=True, text=True, timeout=_budget(deadline),
            ).stdout.strip()
            bits = (out.splitlines()[0] if out else "").split(None, 1)
            ppid = int(bits[0])
        except Exception:
            return False
        command = bits[1] if len(bits) > 1 else ""
        for tok in command.split():
            base = os.path.basename(tok.strip("\"'"))
            if base == "claude" or "claude-code" in tok or base.startswith("2."):
                return "--agent-id" in command
        if ppid <= 1:
            return False
        pid = ppid
    return False


# --- context size -----------------------------------------------------

def context_tokens(transcript_path):
    """Tokens the next request would have to re-cache, or None.

    That is the last assistant message's input_tokens +
    cache_creation_input_tokens + cache_read_input_tokens: everything the
    model was handed on the most recent request, cached or not.

    Only the last TAIL_BYTES are read — transcripts reach tens of MB and
    this runs before every prompt. Sidechain (subagent) records are
    skipped: older Claude Code versions interleave them into the chair's
    transcript, and a worker's 20k context is not this session's."""
    if not isinstance(transcript_path, str) or not transcript_path:
        return None
    try:
        size = os.path.getsize(transcript_path)
        with open(transcript_path, "rb") as f:
            if size > TAIL_BYTES:
                f.seek(size - TAIL_BYTES)
                f.readline()  # discard the partial line the seek landed in
            chunk = f.read(TAIL_BYTES + 1)
    except OSError:
        return None
    for line in reversed(chunk.decode("utf-8", "replace").splitlines()):
        line = line.strip()
        if not line or '"usage"' not in line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict) or rec.get("type") != "assistant":
            continue
        if rec.get("isSidechain"):
            continue
        message = rec.get("message")
        usage = message.get("usage") if isinstance(message, dict) else None
        if not isinstance(usage, dict):
            continue
        total = 0
        for key in ("input_tokens", "cache_creation_input_tokens",
                    "cache_read_input_tokens"):
            try:
                total += int(usage.get(key) or 0)
            except (TypeError, ValueError):
                pass
        return total if total > 0 else None
    return None


# --- formatting -------------------------------------------------------

def fmt_gap(seconds):
    minutes = int(max(0.0, seconds) // 60)
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60:02d} min"


def fmt_tokens(tokens):
    if tokens >= 1000000:
        return f"{tokens / 1000000.0:.1f}M"
    return f"{int(round(tokens / 1000.0))}k"


def cost(tokens):
    """(list-price dollars, output-token equivalents) for re-caching."""
    usd = tokens / 1000000.0 * CACHE_WRITE_USD_PER_MTOK
    equiv = tokens * (CACHE_WRITE_USD_PER_MTOK / OUTPUT_USD_PER_MTOK)
    return usd, equiv


def bound_ledger(marker):
    """The path this session's ledger binding names, if it still exists."""
    path = marker.get("ledger")
    if isinstance(path, str) and path and os.path.isfile(path):
        return path
    return None


def block_reason(tokens, gap_seconds, ack_min, ledger):
    usd, equiv = cost(tokens)
    where = ledger if ledger else 'none bound'
    return (
        f"COLD CACHE - this session holds ~{fmt_tokens(tokens)} tokens of "
        f"context and was idle {fmt_gap(gap_seconds)}; the 1-hour prompt "
        f"cache is cold, so this message would re-write the whole context "
        f"(~${usd:.2f} list, ~{fmt_tokens(equiv)} output-token equivalents "
        f"of Fable limit).\n"
        f"/clear and start fresh (live ledger: {where}) - the ledger on disk "
        f"carries the state, and /compact would re-write the context too.\n"
        f"Or send any prompt again within {ack_min:g} min to proceed anyway."
    )


def warn_message(tokens, gap_seconds):
    usd, equiv = cost(tokens)
    return (
        f"Cold-cache resume: ~{fmt_tokens(tokens)} tokens of context "
        f"re-written after {fmt_gap(gap_seconds)} idle "
        f"(~${usd:.2f} list, ~{fmt_tokens(equiv)} output-token equivalents). "
        f"Finish this step, then /clear."
    )


def warn_context(tokens, gap_seconds, ledger):
    if ledger:
        return (f"Cold-cache resume after {fmt_gap(gap_seconds)}: re-read the "
                f"live ledger {ledger} before acting; finish quickly or hand "
                f"the task to a fresh session.")
    return (f"Cold-cache resume after {fmt_gap(gap_seconds)}: this session's "
            f"~{fmt_tokens(tokens)} tokens of context were re-written from "
            f"cold; finish quickly or hand the task to a fresh session.")


# --- the guard --------------------------------------------------------

def run_guard(data):
    if (os.environ.get("FABLE_ORCH_COLD_GUARD") or "").strip() == "0":
        return

    session_id = data.get("session_id")
    marker = _marker_dict(session_id)
    if marker is None:
        return  # no marker to read a baseline from, and none to invent

    now = time.time()
    prompt = data.get("prompt")
    prompt = prompt.strip() if isinstance(prompt, str) else ""

    # Slash commands and empty submits pass unconditionally. They still
    # stamp `last_prompt`, so a `/clear` (this hook's own advice) leaves
    # the session warm-by-definition instead of being nagged again on the
    # first real prompt afterwards.
    if not prompt or prompt.startswith("/"):
        stamp(session_id, last_prompt=now)
        return

    # `last_prompt` is stamped only when a prompt actually REACHES the
    # model — a blocked prompt leaves the cache exactly as cold as it
    # found it, so the gap has to keep growing across a block.
    baseline = max(_num(marker.get("last_stop")), _num(marker.get("last_prompt")))
    if baseline <= 0:
        stamp(session_id, last_prompt=now)
        return  # first prompt this marker has seen: no gap to judge
    gap = max(0.0, now - baseline)  # a backwards clock step reads as "warm"

    if gap < _env_float("FABLE_ORCH_COLD_MIN", COLD_MIN_DEFAULT) * 60.0:
        stamp(session_id, last_prompt=now)
        return

    if _is_teammate_session():
        stamp(session_id, last_prompt=now)
        return

    # Opt-through: the user saw the block and sent the same thing again.
    ack_min = _env_float("FABLE_ORCH_COLD_ACK_MIN", ACK_MIN_DEFAULT)
    ack = _num(marker.get("cold_ack"))
    if ack > 0 and 0 <= now - ack <= ack_min * 60.0:
        tokens = int(_num(marker.get("cold_ctx")))
        usd, equiv = cost(tokens)
        _metric("cold_ack", session_id, ctx_tokens=tokens,
                gap_min=round(gap / 60.0, 1), est_usd=round(usd, 2),
                est_out_equiv=int(equiv))
        stamp(session_id, drop=("cold_ack", "cold_ctx"), last_prompt=now)
        return

    tokens = context_tokens(data.get("transcript_path"))
    if tokens is None:
        stamp(session_id, last_prompt=now)
        return  # no transcript, no usage: nothing to be confident about

    block_at = _env_int("FABLE_ORCH_COLD_BLOCK_TOKENS", BLOCK_TOKENS_DEFAULT)
    warn_at = _env_int("FABLE_ORCH_COLD_WARN_TOKENS", WARN_TOKENS_DEFAULT)
    usd, equiv = cost(tokens)
    ledger = bound_ledger(marker)

    if block_at > 0 and tokens >= block_at:
        # cold_ctx rides along so the ack path can report what the user
        # chose to pay without re-reading the transcript.
        if not stamp(session_id, cold_ack=now, cold_ctx=tokens):
            # No marker write, no ack window: blocking now would repeat
            # on every prompt with no way through except /clear. A cost
            # guard that cannot offer its own escape hatch must not use
            # the block.
            _metric("cold_stamp_failed", session_id, ctx_tokens=tokens,
                    gap_min=round(gap / 60.0, 1), est_usd=round(usd, 2),
                    est_out_equiv=int(equiv))
            return
        _metric("cold_block", session_id, ctx_tokens=tokens,
                gap_min=round(gap / 60.0, 1), est_usd=round(usd, 2),
                est_out_equiv=int(equiv))
        print(json.dumps({
            "decision": "block",
            "reason": block_reason(tokens, gap, ack_min, ledger),
        }))
        return

    if warn_at > 0 and tokens >= warn_at:
        stamp(session_id, last_prompt=now)
        _metric("cold_warn", session_id, ctx_tokens=tokens,
                gap_min=round(gap / 60.0, 1), est_usd=round(usd, 2),
                est_out_equiv=int(equiv))
        print(json.dumps({
            "systemMessage": warn_message(tokens, gap),
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": warn_context(tokens, gap, ledger),
            },
        }))
        return

    stamp(session_id, last_prompt=now)


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return
    if not isinstance(data, dict):
        return
    try:
        run_guard(data)
    except Exception:
        pass  # fail open: a cost guard must never wedge a prompt


if __name__ == "__main__":
    main()
