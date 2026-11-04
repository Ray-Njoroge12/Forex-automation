# Runbook: long jobs (replays, diagnostics)

Long jobs run detached through `validation.detached_job`, never in the foreground of an agent tool call. Agent tool timeouts kill foreground commands, and on 2026-11-03 killed replay parents left orphaned `multiprocessing` workers that overloaded the Mac.

Run every command below from `fx_ai_engine/` with the repo venv (`../.venv/bin/python`, or the absolute path `/Users/raynj/Desktop/Forex-automation/.venv/bin/python` from a worktree).

## Budget defaults

| Setting | Default | Flag |
| --- | --- | --- |
| Niceness of supervisor and command | 10 | `--nice` |
| Worker processes (`FX_MAX_WORKERS`, caps `--workers` in the replay) | 2 | `--max-workers` |
| Wall-clock timeout, then SIGTERM to the process group, SIGKILL after 10 s | 10,800 s (3 h) | `--timeout-seconds` |
| Job folder root | `artifacts/jobs/` (git-ignored) | `--jobs-dir` |

The replay itself defaults to 2 workers, runs workers at niceness ≥ 10, caches candidates in `artifacts/replay/cache/` (git-ignored; `--no-cache` to bypass), and turns SIGTERM into a clean exit that terminates its workers. A worker whose parent dies exits within about 2 s.

## Start

```bash
../.venv/bin/python -m validation.detached_job start --name replay-baseline -- \
  ../.venv/bin/python -m backtesting.run_replay --data-dir artifacts/datasets/mt5_2026-11-03 --variants baseline
```

It prints the job folder (`artifacts/jobs/<UTC stamp>_<name>/`) and returns at once. The command runs in its own process group, in the directory `start` was called from.

## Poll

```bash
../.venv/bin/python -m validation.detached_job status artifacts/jobs/<stamp>_replay-baseline
```

- `"state": "running"`: supervisor alive; `running.json` has `supervisor_pid`, `child_pid`, `pgid`.
- `"state": "done"`: `done.json` exists (the completion flag) with `status` (`completed`, `failed`, `timeout`, `stopped`), `exit_code`, `started_at`, `finished_at`, `duration_s`, `leftover_processes_killed`.
- `"state": "lost"`: the supervisor died without writing `done.json`. Run `stop` (it signals the recorded process group) and the stray-process check.

Output is in `log.txt` in the job folder; supervisor errors are in `supervisor.log`.

## Stop

```bash
../.venv/bin/python -m validation.detached_job stop artifacts/jobs/<stamp>_replay-baseline
```

This writes `stop.request` and sends SIGTERM to the supervisor, which terminates the command's process group and records `stopped`.

## Stray-process check (after every job)

```bash
ps -axo pid,ppid,ni,etime,command | grep -Ei 'Forex-automation/\.venv|multiprocessing|detached_job' | grep -v grep || echo "no stray processes"
```

Anything listed that belongs to this repository and is not a running job you started must be stopped (`kill -TERM <pid>`, or `kill -TERM -<pgid>` for a whole group).

## Rules

- Development waves: no multi-year replays; small samples only.
- At most 2 worker processes on the owner's Mac unless the owner says otherwise.
- Never start `main.py --mode demo` or launchd agents from a job unless the owner starts a test wave.
