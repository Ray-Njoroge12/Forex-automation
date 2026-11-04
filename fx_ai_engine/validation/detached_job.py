"""Run a long job detached from the calling shell, with a resource budget and a completion flag.

Agent tool timeouts kill foreground commands, and killed parents used to leave
replay workers behind (STATUS known issue 4). This runner starts a supervisor
in its own session that survives the caller, runs the command niced in its own
process group with ``FX_MAX_WORKERS`` set, enforces a wall-clock timeout, makes
sure no process of the group survives, and then writes ``done.json``.

Usage (from fx_ai_engine/):
    python -m validation.detached_job start --name replay -- python -m backtesting.run_replay --data-dir ...
    python -m validation.detached_job status artifacts/jobs/<stamp>_replay
    python -m validation.detached_job stop artifacts/jobs/<stamp>_replay

Job folder files: job.json (command and budget), running.json (pids while it
runs), log.txt (command output), supervisor.log, done.json (completion flag:
status completed/failed/timeout/stopped, exit_code, timings,
leftover_processes_killed) and stop.request (written by ``stop``).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_JOBS_DIR = PROJECT_ROOT / "artifacts" / "jobs"
DEFAULT_NICE = 10
DEFAULT_MAX_WORKERS = 2
DEFAULT_TIMEOUT_SECONDS = 3 * 3600
TERM_GRACE_SECONDS = 10.0
POLL_SECONDS = 0.2
START_WAIT_SECONDS = 10.0


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _pid_alive(pid: object) -> bool:
    if type(pid) is not int or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _signal_group(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _group_members(pgid: int) -> list[int]:
    try:
        out = subprocess.run(["ps", "-A", "-o", "pid=,pgid="], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    members = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == str(pgid):
            members.append(int(parts[0]))
    return members


def _raise_niceness(target: int) -> None:
    current = os.nice(0)
    if current < target:
        os.nice(target - current)


# ----------------------------------------------------------------------
# start
# ----------------------------------------------------------------------
def start_job(command: list[str], *, name: str, nice: int = DEFAULT_NICE, max_workers: int = DEFAULT_MAX_WORKERS,
              timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS, jobs_dir: Path = DEFAULT_JOBS_DIR,
              cwd: Path | None = None) -> Path:
    if not command:
        raise ValueError("no command given")
    if nice < 0 or max_workers < 1 or timeout_seconds <= 0:
        raise ValueError("budget must be nice >= 0, max-workers >= 1 and timeout-seconds > 0")
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", name).strip("-.") or "job"
    jobs_dir = Path(jobs_dir).resolve()
    jobs_dir.mkdir(parents=True, exist_ok=True)
    stamp = _utc_now().strftime("%Y%m%dT%H%M%SZ")
    job_dir = jobs_dir / f"{stamp}_{safe_name}"
    suffix = 1
    while job_dir.exists():
        suffix += 1
        job_dir = jobs_dir / f"{stamp}_{safe_name}_{suffix}"
    job_dir.mkdir()
    _write_json_atomic(job_dir / "job.json", {
        "name": safe_name,
        "cmd": list(command),
        "cwd": str(Path(cwd or os.getcwd()).resolve()),
        "budget": {"nice": int(nice), "max_workers": int(max_workers),
                   "timeout_seconds": float(timeout_seconds), "term_grace_seconds": TERM_GRACE_SECONDS},
        "created_at": _utc_now().isoformat(),
    })
    with (job_dir / "supervisor.log").open("ab") as log:
        launcher = subprocess.Popen(
            [sys.executable, "-m", "validation.detached_job", "_supervise", str(job_dir)],
            cwd=PROJECT_ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    # The launcher forks the supervisor and exits at once, so the supervisor is
    # adopted by launchd/init and never becomes a zombie of this process.
    launcher.wait(timeout=30)
    deadline = time.monotonic() + START_WAIT_SECONDS
    while time.monotonic() < deadline:
        if (job_dir / "running.json").exists() or (job_dir / "done.json").exists():
            break
        time.sleep(POLL_SECONDS)
    return job_dir


# ----------------------------------------------------------------------
# supervise (internal)
# ----------------------------------------------------------------------
def _finish(job_dir: Path, status: str, exit_code: int | None, started: datetime, leftovers: int) -> None:
    finished = _utc_now()
    _write_json_atomic(job_dir / "done.json", {
        "status": status,
        "exit_code": exit_code,
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "duration_s": round((finished - started).total_seconds(), 3),
        "leftover_processes_killed": leftovers,
    })


def supervise(job_dir: Path) -> int:
    job_dir = Path(job_dir).resolve()
    job = _read_json(job_dir / "job.json")
    if job is None:
        print(f"unreadable job.json in {job_dir}", file=sys.stderr)
        return 2
    budget = job["budget"]
    started = _utc_now()
    stop_flag = {"set": False}

    def _on_term(_signum, _frame) -> None:
        stop_flag["set"] = True

    signal.signal(signal.SIGTERM, _on_term)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    _raise_niceness(int(budget["nice"]))
    env = dict(os.environ)
    env["FX_MAX_WORKERS"] = str(int(budget["max_workers"]))
    try:
        with (job_dir / "log.txt").open("ab") as log:
            child = subprocess.Popen(job["cmd"], cwd=job["cwd"], env=env, stdin=subprocess.DEVNULL,
                                     stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    except OSError as exc:
        print(f"could not start command: {exc}", file=sys.stderr)
        _finish(job_dir, "failed", 127, started, 0)
        return 0
    pgid = child.pid  # start_new_session makes the child its own group leader
    _write_json_atomic(job_dir / "running.json", {
        "supervisor_pid": os.getpid(), "child_pid": child.pid, "pgid": pgid, "started_at": started.isoformat(),
    })

    deadline = time.monotonic() + float(budget["timeout_seconds"])
    status: str | None = None
    while child.poll() is None:
        if stop_flag["set"] or (job_dir / "stop.request").exists():
            status = "stopped"
        elif time.monotonic() >= deadline:
            status = "timeout"
        if status is not None:
            _signal_group(pgid, signal.SIGTERM)
            try:
                child.wait(timeout=float(budget.get("term_grace_seconds", TERM_GRACE_SECONDS)))
            except subprocess.TimeoutExpired:
                _signal_group(pgid, signal.SIGKILL)
                child.wait()
            break
        time.sleep(POLL_SECONDS)

    exit_code = child.returncode
    leftovers = 0
    if _group_alive(pgid):
        # The command exited but something it started is still in its group.
        leftovers = len([pid for pid in _group_members(pgid) if pid != os.getpid()]) or 1
        _signal_group(pgid, signal.SIGTERM)
        grace_end = time.monotonic() + 2.0
        while _group_alive(pgid) and time.monotonic() < grace_end:
            time.sleep(POLL_SECONDS)
        _signal_group(pgid, signal.SIGKILL)
        kill_end = time.monotonic() + 5.0
        while _group_alive(pgid) and time.monotonic() < kill_end:
            time.sleep(POLL_SECONDS)
    if status is None:
        status = "completed" if exit_code == 0 else "failed"
    _finish(job_dir, status, exit_code, started, leftovers)
    return 0


def _launch_supervisor(job_dir: Path) -> int:
    """Fork so the supervisor is not a child of the ``start`` caller."""
    if os.fork() > 0:
        os._exit(0)
    return supervise(job_dir)


# ----------------------------------------------------------------------
# status / stop
# ----------------------------------------------------------------------
def job_status(job_dir: Path) -> dict[str, Any]:
    job_dir = Path(job_dir)
    done = _read_json(job_dir / "done.json")
    if done is not None:
        return {"state": "done", **done}
    running = _read_json(job_dir / "running.json")
    if running is not None:
        if _pid_alive(running.get("supervisor_pid")):
            return {"state": "running", **running}
        return {"state": "lost", **running}
    job = _read_json(job_dir / "job.json")
    if job is None:
        return {"state": "unknown", "reason": "no job.json"}
    return {"state": "lost", "reason": "supervisor never reported running"}


def stop_job(job_dir: Path) -> dict[str, Any]:
    job_dir = Path(job_dir)
    current = job_status(job_dir)
    if current["state"] == "done":
        return current
    (job_dir / "stop.request").write_text(_utc_now().isoformat(), encoding="utf-8")
    pid = current.get("supervisor_pid")
    if current["state"] == "running" and _pid_alive(pid):
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    elif type(current.get("pgid")) is int and current["pgid"] > 0:
        # Supervisor is gone; terminate the orphaned command group directly.
        _signal_group(current["pgid"], signal.SIGTERM)
    return job_status(job_dir)


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Detached long jobs with a completion flag and resource budget")
    sub = parser.add_subparsers(dest="action", required=True)
    start = sub.add_parser("start", help="start a command detached; prints the job folder")
    start.add_argument("--name", required=True)
    start.add_argument("--nice", type=int, default=DEFAULT_NICE)
    start.add_argument("--max-workers", type=int, default=DEFAULT_MAX_WORKERS)
    start.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    start.add_argument("--jobs-dir", type=Path, default=DEFAULT_JOBS_DIR)
    start.add_argument("cmd", nargs=argparse.REMAINDER, help="-- command and arguments")
    for action in ("status", "stop", "_supervise"):
        sub.add_parser(action).add_argument("job_dir", type=Path)
    args = parser.parse_args(argv)

    if args.action == "start":
        command = list(args.cmd)
        if command and command[0] == "--":
            command = command[1:]
        if not command:
            parser.error("start needs a command after --")
        job_dir = start_job(command, name=args.name, nice=args.nice, max_workers=args.max_workers,
                            timeout_seconds=args.timeout_seconds, jobs_dir=args.jobs_dir)
        print(job_dir)
        return 0
    if args.action == "_supervise":
        return _launch_supervisor(args.job_dir)
    result = job_status(args.job_dir) if args.action == "status" else stop_job(args.job_dir)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
