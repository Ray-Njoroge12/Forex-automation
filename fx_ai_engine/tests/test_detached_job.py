"""Detached long jobs: completion flag, resource budget, timeout and stop (STATUS issue 4)."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

from validation import detached_job

POLL_LIMIT_SECONDS = 15.0


def _wait_done(job_dir: Path) -> dict:
    deadline = time.monotonic() + POLL_LIMIT_SECONDS
    while time.monotonic() < deadline:
        status = detached_job.job_status(job_dir)
        if status["state"] == "done":
            return status
        time.sleep(0.1)
    pytest.fail(f"job did not finish: {detached_job.job_status(job_dir)}")


def _wait_gone(pid: int, pgid: int | None = None) -> None:
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        group_gone = pgid is None or not detached_job._group_alive(pgid)
        if not detached_job._pid_alive(pid) and group_gone:
            return
        time.sleep(0.1)
    pytest.fail(f"process {pid} (group {pgid}) still alive")


def _running(job_dir: Path) -> dict:
    deadline = time.monotonic() + POLL_LIMIT_SECONDS
    while time.monotonic() < deadline:
        running = detached_job._read_json(job_dir / "running.json")
        if running:
            return running
        time.sleep(0.1)
    pytest.fail("job never reported running")


def test_job_completes_with_budget_applied(tmp_path):
    code = "import os; print('nice', os.nice(0)); print('workers', os.environ['FX_MAX_WORKERS'])"
    job_dir = detached_job.start_job([sys.executable, "-c", code], name="budget check",
                                     jobs_dir=tmp_path, cwd=tmp_path)
    assert job_dir.parent == tmp_path.resolve()
    job = json.loads((job_dir / "job.json").read_text())
    assert job["budget"]["nice"] == 10 and job["budget"]["max_workers"] == 2
    done = _wait_done(job_dir)
    assert done["status"] == "completed" and done["exit_code"] == 0
    assert done["leftover_processes_killed"] == 0
    log = (job_dir / "log.txt").read_text().split()
    assert int(log[log.index("nice") + 1]) >= 10
    assert log[log.index("workers") + 1] == "2"
    _wait_gone(_running(job_dir)["supervisor_pid"])


def test_job_timeout_kills_the_process_group(tmp_path):
    job_dir = detached_job.start_job(["sleep", "60"], name="timeout", timeout_seconds=1, jobs_dir=tmp_path)
    running = _running(job_dir)
    done = _wait_done(job_dir)
    assert done["status"] == "timeout"
    _wait_gone(running["child_pid"], running["pgid"])
    _wait_gone(running["supervisor_pid"])


def test_stop_request_ends_job_as_stopped(tmp_path):
    job_dir = detached_job.start_job(["sleep", "60"], name="stop", jobs_dir=tmp_path)
    running = _running(job_dir)
    assert detached_job.job_status(job_dir)["state"] == "running"
    detached_job.stop_job(job_dir)
    done = _wait_done(job_dir)
    assert done["status"] == "stopped"
    _wait_gone(running["child_pid"], running["pgid"])
    _wait_gone(running["supervisor_pid"])


def test_failing_command_reports_failed_with_exit_code(tmp_path):
    job_dir = detached_job.start_job([sys.executable, "-c", "raise SystemExit(3)"], name="fail", jobs_dir=tmp_path)
    done = _wait_done(job_dir)
    assert done["status"] == "failed" and done["exit_code"] == 3
    _wait_gone(_running(job_dir)["supervisor_pid"])


def test_leftover_background_process_is_killed(tmp_path):
    # The command exits at once but leaves a sleep running in its process group.
    job_dir = detached_job.start_job(["/bin/sh", "-c", "sleep 60 & exit 0"], name="leftover", jobs_dir=tmp_path)
    running = _running(job_dir)
    done = _wait_done(job_dir)
    assert done["status"] == "completed"
    assert done["leftover_processes_killed"] >= 1
    _wait_gone(running["child_pid"], running["pgid"])


def test_cli_start_prints_job_dir_and_status_reports_done(tmp_path, capsys):
    assert detached_job.main(["start", "--name", "cli", "--jobs-dir", str(tmp_path), "--",
                              sys.executable, "-c", "print('ok')"]) == 0
    job_dir = Path(capsys.readouterr().out.strip())
    assert job_dir.is_dir()
    _wait_done(job_dir)
    assert detached_job.main(["status", str(job_dir)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "completed"


def test_status_reports_lost_when_supervisor_is_gone(tmp_path):
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    (job_dir / "job.json").write_text("{}")
    (job_dir / "running.json").write_text(json.dumps({"supervisor_pid": 2**22 + 12345, "pgid": 0}))
    assert detached_job.job_status(job_dir)["state"] == "lost"
    assert os.path.exists(job_dir / "running.json")
