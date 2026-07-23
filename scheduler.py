"""
scheduler.py — Engram's cron/self-scheduler. Lets Engram fire its own tasks on a
delay or a repeat, self-injecting them through the same `orchestrate_task` entry a
Telegram message uses. Closes the long-standing "can't self-inject a task" gap.

Grammar (via the `schedule` command): `schedule <when> | <task>`
  when := "in 2h" | "in 30m" | "every 45m" | "every 1d" | "daily 09:00" | "at 17:30"
State lives in config/schedules.json (git-ignored). Pure functions here; engram.py
supplies the dispatch callback so this module never imports the daemon (no cycle).
"""

import json
import logging
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger("engram")

SCHEDULES_FILE = Path(__file__).resolve().parent / "config" / "schedules.json"
_UNIT = {"s": 1, "m": 60, "h": 3600, "d": 86400}
TICK_SECONDS = 30


def parse_when(spec: str, now: datetime | None = None) -> tuple[str, datetime, int | None]:
    """Parse a when-spec → (kind, next_run, interval_s). kind is 'once' or 'repeat'.
    Raises ValueError with a helpful message on anything it can't parse."""
    now = now or datetime.now()
    s = spec.strip().lower()
    m = re.fullmatch(r"in\s+(\d+)\s*([smhd])", s)
    if m:
        return "once", now + timedelta(seconds=int(m[1]) * _UNIT[m[2]]), None
    m = re.fullmatch(r"every\s+(\d+)\s*([smhd])", s)
    if m:
        secs = int(m[1]) * _UNIT[m[2]]
        return "repeat", now + timedelta(seconds=secs), secs
    m = re.fullmatch(r"(?:daily\s+|at\s+)(\d{1,2}):(\d{2})", s)
    if m:
        hh, mm = int(m[1]), int(m[2])
        if not (0 <= hh < 24 and 0 <= mm < 60):
            raise ValueError(f"'{spec}' — hour must be 0-23, minute 0-59")
        nxt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if nxt <= now:
            nxt += timedelta(days=1)
        return "repeat", nxt, _UNIT["d"]
    raise ValueError(f"can't read schedule '{spec}' — try `in 2h`, `every 30m`, or `daily 09:00`")


def load_schedules() -> list:
    try:
        return json.loads(SCHEDULES_FILE.read_text())
    except (OSError, ValueError):
        return []


def save_schedules(scheds: list) -> None:
    SCHEDULES_FILE.parent.mkdir(parents=True, exist_ok=True)
    SCHEDULES_FILE.write_text(json.dumps(scheds, indent=2))


def add_schedule(rest: str, now: datetime | None = None) -> str:
    """`rest` = '<when> | <task>'. Returns a user-facing confirmation, or an error
    string on a bad spec / missing task (never raises — this feeds a Telegram reply)."""
    spec, sep, task = rest.partition("|")
    spec, task = spec.strip(), task.strip()
    if not sep or not task:
        return "Usage: `schedule <when> | <task>` — e.g. `schedule daily 09:00 | on alphabot, run the morning check`"
    try:
        kind, next_run, interval_s = parse_when(spec, now)
    except ValueError as exc:
        return f"❓ {exc}"
    scheds = load_schedules()
    sid = f"s{int((now or datetime.now()).timestamp())}{len(scheds)}"
    scheds.append(
        {
            "id": sid,
            "when": spec,
            "kind": kind,
            "next_run": next_run.isoformat(),
            "interval_s": interval_s,
            "task": task,
        }
    )
    save_schedules(scheds)
    tag = f"repeats {spec}" if kind == "repeat" else "once"
    return f"⏰ Scheduled ({tag}) — next run {next_run.strftime('%Y-%m-%d %H:%M')}:\n{task[:100]}"


def list_schedules(now: datetime | None = None) -> str:
    scheds = load_schedules()
    if not scheds:
        return "⏰ No schedules. Add one: `schedule <when> | <task>`"
    lines = ["Schedules — `unschedule <n>` to remove:"]
    for i, s in enumerate(scheds, 1):
        nxt = datetime.fromisoformat(s["next_run"]).strftime("%m-%d %H:%M")
        kind = f"every {s['when'].replace('every ', '')}" if s["kind"] == "repeat" else "once"
        lines.append(f"{i}. [{kind}] next {nxt} — {s['task'][:50]}  ({s['id']})")
    return "\n".join(lines)


def remove_schedule(sel: str) -> str:
    scheds = load_schedules()
    if not scheds:
        return "⏰ No schedules to remove."
    sel = sel.strip()
    idx = None
    if sel.isdigit() and 1 <= int(sel) <= len(scheds):
        idx = int(sel) - 1
    else:
        idx = next((i for i, s in enumerate(scheds) if s["id"] == sel), None)
    if idx is None:
        return f"❓ No schedule matches {sel!r}. Send `schedules` to list them."
    removed = scheds.pop(idx)
    save_schedules(scheds)
    return f"🗑 Removed schedule: {removed['task'][:60]}"


def due_schedules(scheds: list, now: datetime) -> tuple[list, list]:
    """Split into (fired, remaining-after-advance). Repeats get their next_run advanced
    past `now`; one-shots are dropped once fired. Pure — no I/O, easy to test."""
    fired, remaining = [], []
    for s in scheds:
        if datetime.fromisoformat(s["next_run"]) <= now:
            fired.append(s)
            if s["kind"] == "repeat" and s.get("interval_s"):
                nxt = datetime.fromisoformat(s["next_run"])
                while nxt <= now:
                    nxt += timedelta(seconds=s["interval_s"])
                s = {**s, "next_run": nxt.isoformat()}
                remaining.append(s)
            # once → not re-added
        else:
            remaining.append(s)
    return fired, remaining


class Scheduler(threading.Thread):
    """Ticks every TICK_SECONDS; fires due schedules via `dispatch(task_text)`."""

    def __init__(self, dispatch, tick_s: int = TICK_SECONDS):
        super().__init__(daemon=True, name="engram-scheduler")
        self.dispatch = dispatch
        self.tick_s = tick_s

    def run(self):
        log.info("[scheduler] started")
        while True:
            try:
                self.tick()
            except Exception as exc:
                log.warning(f"[scheduler] tick failed: {exc}")
            time.sleep(self.tick_s)

    def tick(self, now: datetime | None = None) -> int:
        now = now or datetime.now()
        scheds = load_schedules()
        if not scheds:
            return 0
        fired, remaining = due_schedules(scheds, now)
        for s in fired:
            log.info(f"[scheduler] firing {s['id']}: {s['task'][:60]}")
            self.dispatch(s["task"])
        if fired:
            save_schedules(remaining)
        return len(fired)
