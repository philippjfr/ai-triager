"""Background triage/review jobs.

A job is a detached process (``ai-triager job <id>``) whose spec and live status
live in ``jobs/<id>.json`` and whose log is ``jobs/<id>.log``. Per-issue agent
transcripts go to ``logs/<id>/``. The app and the CLI only spawn, read and
signal jobs, so a job survives restarts of the app.

Runners:

- ``builtin``: the pydantic-ai agent in ``agent.py``, one fresh session per issue.
- any entry of ``[agents.runners]``: an external agent CLI (e.g. kilo) invoked
  once per batch with a command template, as ``bin/triage-loop`` used to.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time

from pathlib import Path

from . import core, github
from .config import MODES, Config, write_atomic

ANSI = re.compile(r'\x1b\[[0-9;]*[A-Za-z]')
FINISHED = re.compile(r'#(\d+): finished as ([a-z-]+) \((low|medium|high)\)')
VERIFIED = re.compile(r'#(\d+): verified=(yes|disputed)')


def batch_outcomes(text: str) -> list[dict]:
    """Per-issue outcomes from the `./triage.py finish`/`verify` output an external agent printed."""
    text = ANSI.sub('', text)
    out, seen = [], set()
    for n, cat, conf in FINISHED.findall(text):
        if n not in seen:
            seen.add(n)
            out.append({'issue': int(n), 'outcome': 'finished', 'category': cat, 'confidence': conf, 'status': 'ok'})
    for n, verdict in VERIFIED.findall(text):
        out.append({'issue': int(n), 'outcome': verdict, 'status': 'ok'})
    return out


def new_job_id(mode: str) -> str:
    return f"{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}-{mode}"


def job_path(cfg: Config, job_id: str) -> Path:
    return cfg.jobs / f'{job_id}.json'


def read_job(cfg: Config, job_id: str) -> dict:
    job = json.loads(job_path(cfg, job_id).read_text())
    if job.get('status') == 'running' and not _alive(job.get('pid')):
        job['status'] = 'died'
    return job


def list_jobs(cfg: Config) -> list[dict]:
    jobs = []
    for path in sorted(cfg.jobs.glob('*.json'), reverse=True) if cfg.jobs.exists() else []:
        try:
            jobs.append(read_job(cfg, path.stem))
        except (OSError, ValueError):
            continue
    return jobs


def _alive(pid) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    try:
        # A zombie child of this process still answers kill(0).
        return os.waitpid(pid, os.WNOHANG) == (0, 0)
    except ChildProcessError:
        return True


def spawn(cfg: Config, spec: dict, python: str | None = None) -> dict:
    """Write the job spec and start the detached job process."""
    cfg.jobs.mkdir(parents=True, exist_ok=True)
    job_id = spec.get('id') or new_job_id(spec['mode'])
    job = {**spec, 'id': job_id, 'status': 'queued', 'processed': [], 'created': core.now().isoformat()}
    write_atomic(job_path(cfg, job_id), json.dumps(job, indent=1))
    python = python or cfg.agents.get('python') or sys.executable
    log = open(cfg.jobs / f'{job_id}.log', 'ab')
    proc = subprocess.Popen(
        [python, '-m', 'ai_triager', '--workspace', str(cfg.root), 'job', job_id],
        cwd=cfg.root, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    job['pid'] = proc.pid
    _update(cfg, job_id, pid=proc.pid)
    return job


def stop(cfg: Config, job_id: str):
    job = read_job(cfg, job_id)
    if _alive(job.get('pid')):
        os.kill(job['pid'], signal.SIGTERM)


def _update(cfg: Config, job_id: str, **fields) -> dict:
    path = job_path(cfg, job_id)
    job = json.loads(path.read_text())
    job.update(fields)
    write_atomic(path, json.dumps(job, indent=1, default=str))
    return job


class JobRunner:
    def __init__(self, cfg: Config, job_id: str):
        self.cfg = cfg
        self.job_id = job_id
        self.spec = json.loads(job_path(cfg, job_id).read_text())
        self.settings = cfg.agents
        self.mode = self.spec['mode']
        self.workers = max(1, int(self.spec.get('workers') or 1))
        self.issues = [int(i) for i in self.spec.get('issues') or []]
        self.count = len(self.issues) if self.issues else int(self.spec.get('count') or 5)
        self.started_issues = 0
        self.attempted: set[int] = set()
        self.processed: list[dict] = []
        self.stopping = False
        self.procs: list[subprocess.Popen] = []
        self.logs = cfg.logs / job_id

    def log(self, msg: str):
        print(f"[{dt.datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

    def agent_id(self, worker: int) -> str:
        return f"{self.spec['model']}@{self.job_id}.w{worker}"

    # -- builtin runner -----------------------------------------------------

    def _claim(self, agent_id: str) -> int | None:
        kwargs = dict(issues=self.issues or None, exclude=self.attempted)
        if self.mode == 'review':
            picked = core.claim_review(self.cfg, 1, agent_id, **kwargs)
            return picked[0]['issue'] if picked else None
        picked = core.claim_next(self.cfg, 1, agent_id, order=self.spec.get('order') or 'oldest',
                                 label=self.spec.get('label') or None, **kwargs)
        return picked[0]['number'] if picked else None

    def _outcome(self, issue: int) -> dict:
        try:
            meta, _ = core.parse_writeup(core.writeup_path(self.cfg, issue))
        except (OSError, ValueError) as e:
            return {'outcome': 'invalid', 'detail': str(e)}
        if self.mode == 'review':
            verdict = meta.get('verified')
            return {'outcome': verdict if verdict in ('yes', 'disputed') else 'unverified',
                    'category': meta.get('category')}
        if meta.get('category') in (None, '', 'pending'):
            return {'outcome': 'unfinished'}
        errors = core.validate_writeup(self.cfg, core.writeup_path(self.cfg, issue))
        return {'outcome': 'invalid' if errors else 'finished', 'category': meta.get('category'),
                'confidence': meta.get('confidence'), 'recommendation': meta.get('recommendation'),
                'errors': errors[:5]}

    async def _builtin_worker(self, w: int):
        from .agent import run_issue

        agent_id = self.agent_id(w)
        errors_in_row = 0
        while not self.stopping and self.started_issues < self.count:
            if errors_in_row >= 2:
                self.log(f'w{w}: stopping after {errors_in_row} failed sessions in a row')
                return
            self.started_issues += 1
            issue = self._claim(agent_id)
            if issue is None:
                self.log(f'w{w}: nothing left to {"review" if self.mode == "review" else "claim"}')
                return
            self.attempted.add(issue)
            self.log(f'w{w}: #{issue} started')
            self._save(current={**self._current(), str(w): issue})
            # The agent's shell has no network, so `./triage.py context` must find the issue in the cache.
            try:
                await asyncio.to_thread(github.fetch_issue, self.cfg, issue, True)
            except Exception as e:
                self.log(f'w{w}: #{issue} could not refresh issue context ({e}); using the cache')
            try:
                summary = await run_issue(
                    self.cfg, mode=self.mode, model=self.spec['model'], issue=issue, agent_id=agent_id,
                    settings={**self.settings, 'effort': self.spec.get('effort'), 'steps': self.spec.get('steps')},
                    transcript=self.logs / f'{issue}.json',
                )
            finally:
                core.release(self.cfg, issue, review=self.mode == 'review')
            errors_in_row = errors_in_row + 1 if summary['status'] == 'error' else 0
            summary.update(self._outcome(issue), worker=w)
            summary.pop('output', None)
            self.processed.append(summary)
            cost = f", ${summary['cost']:.3f}" if summary.get('cost') is not None else ''
            self.log(f"w{w}: #{issue} {summary['outcome']} {summary.get('category') or ''} "
                     f"({summary['status']}, {summary['duration']}s{cost})"
                     + (f": {summary['error']}" if summary.get('error') else ''))
            current = self._current()
            current.pop(str(w), None)
            self._save(current=current)

    def _current(self) -> dict:
        return json.loads(job_path(self.cfg, self.job_id).read_text()).get('current', {})

    # -- external CLI runner ------------------------------------------------

    def _external_command(self, runner: dict, w: int, b: int, per_batch: int) -> list[str]:
        mode = MODES[self.mode]
        next_args = [f'-n {per_batch}']
        if self.spec.get('order'):
            next_args.append(f"--order {self.spec['order']}")
        if self.spec.get('label'):
            next_args.append(f"--label {shlex.quote(self.spec['label'])}")
        if self.issues:
            next_args.append('--issue ' + ' '.join(map(str, self.issues)))
        system_path = self.cfg.prompts / f'{self.mode}.md'
        values = {
            'root': str(self.cfg.root), 'agent': mode['agent'], 'command': mode['command'],
            'model': self.spec['model'], 'title': f"{mode['command']} {self.job_id} w{w} b{b}",
            'args': ' '.join(next_args), 'steps': str(self.spec.get('steps') or mode['steps']),
            'system': system_path.read_text() if system_path.exists() else '',
            'prompt': f"Follow prompts/{self.mode}-batch.md with arguments: {' '.join(next_args)}",
        }
        cmd = []
        for part in runner['command']:
            if part == '{disallowed...}':
                cmd += [f'Bash({p})' for p in self.settings.get('deny', [])]
            else:
                cmd.append(part.format(**values))
        return cmd

    async def _external_worker(self, w: int):
        runner = self.settings['runners'][self.spec['runner']]
        per_batch = int(self.spec.get('per_batch') or self.settings.get('per_batch') or 5)
        agent_id = self.agent_id(w)
        b = 0
        while not self.stopping and self.started_issues < self.count:
            b += 1
            n = min(per_batch, self.count - self.started_issues)
            self.started_issues += n
            preview = (core.claim_review if self.mode == 'review' else core.claim_next)(
                self.cfg, 1, agent_id, dry_run=True, issues=self.issues or None)
            if not preview:
                self.log(f'w{w}: queue empty, stopping')
                return
            cmd = self._external_command(runner, w, b, n)
            log = self.logs / f'w{w}-b{b}.log'
            log.parent.mkdir(parents=True, exist_ok=True)
            self.log(f'w{w}: batch {b} ({n} issues) -> {log.relative_to(self.cfg.root)}')
            env = dict(os.environ, TRIAGE_AGENT=agent_id,
                       TRIAGE_WORKSPACE=str(self.cfg.root))
            start = time.monotonic()
            with log.open('w') as f:
                proc = subprocess.Popen(cmd, cwd=self.cfg.root, stdout=f, stderr=subprocess.STDOUT,
                                        stdin=subprocess.DEVNULL, env=env, start_new_session=True)
                self.procs.append(proc)
                while proc.poll() is None:
                    await asyncio.sleep(1)
                self.procs.remove(proc)
            core.release_agent(self.cfg, agent_id)
            rel = str(log.relative_to(self.cfg.root))
            outcomes = batch_outcomes(log.read_text(errors='replace'))
            self.processed += [{**o, 'worker': w, 'batch': b, 'log': rel} for o in outcomes]
            if not outcomes:
                self.processed.append({'batch': b, 'worker': w, 'exit_code': proc.returncode,
                                       'duration': round(time.monotonic() - start, 1), 'log': rel})
            self.log(f'w{w}: batch {b} exited with {proc.returncode}')
            self._save()

    # -- lifecycle ----------------------------------------------------------

    def _save(self, **extra):
        totals = {'cost': round(sum(p.get('cost') or 0 for p in self.processed if isinstance(p, dict)), 4),
                  'issues': sum(1 for p in self.processed if 'issue' in p)}
        _update(self.cfg, self.job_id, processed=self.processed, totals=totals, **extra)

    def _on_signal(self, tasks: list[asyncio.Task]):
        self.log('stop requested')
        self.stopping = True
        for proc in self.procs:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        for task in tasks:
            task.cancel()

    async def _labels(self):
        from . import labels

        issues = labels.select_issues(self.cfg, self.spec.get('scope') or 'unreviewed', self.issues or None,
                                      None if self.issues else self.count)
        self.log(f'reviewing labels on {len(issues)} issue(s)')

        def on_result(entry):
            self.processed.append(entry)
            changes = f"+{len(entry.get('add', []))} -{len(entry.get('remove', []))}"
            self.log(f"#{entry['issue']} {entry.get('status')} {changes if entry.get('status') == 'pending' else ''}"
                     + (f": {entry['error']}" if entry.get('error') else ''))
            self._save()

        totals = await labels.classify(self.cfg, issues, self.spec['model'], workers=self.workers,
                                       on_result=on_result)
        if issues and totals['failed'] == len(issues):
            raise RuntimeError(self.processed[0].get('error', 'every request failed'))

    async def run(self):
        _update(self.cfg, self.job_id, status='running', pid=os.getpid(), started=core.now().isoformat())
        if self.mode == 'labels':
            from . import credentials
            credentials.apply_to_environ(self.cfg)
            os.environ.setdefault('PYDANTIC_AI_NO_BANNER', '1')
            self.log(f"labels job {self.job_id}: model={self.spec['model']} count={self.count}")
            status, error = 'done', None
            task = asyncio.create_task(self._labels())
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(sig, self._on_signal, [task])
            try:
                await task
            except asyncio.CancelledError:
                status = 'stopped'
            except Exception as e:
                status, error = 'failed', f'{type(e).__name__}: {e}'
            self._save(status=status, error=error, finished=core.now().isoformat())
            self.log(f'job {status}' + (f': {error}' if error else ''))
            return
        builtin = self.spec.get('runner', 'builtin') == 'builtin'
        if builtin:
            from . import credentials
            credentials.apply_to_environ(self.cfg)
            os.environ.setdefault('PYDANTIC_AI_NO_BANNER', '1')
        self.log(f"{self.mode} job {self.job_id}: runner={self.spec.get('runner', 'builtin')} "
                 f"model={self.spec['model']} count={self.count} workers={self.workers}")
        worker = self._builtin_worker if builtin else self._external_worker
        tasks = []
        for w in range(1, self.workers + 1):
            tasks.append(asyncio.create_task(worker(w)))
            await asyncio.sleep(0.5)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self._on_signal, tasks)
        status, error = 'done', None
        try:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            errors = [r for r in results if isinstance(r, BaseException) and not isinstance(r, asyncio.CancelledError)]
            if errors:
                status, error = 'failed', f'{type(errors[0]).__name__}: {errors[0]}'
            elif self.processed and all(p.get('status') == 'error' for p in self.processed):
                status, error = 'failed', self.processed[0].get('error')
        finally:
            for w in range(1, self.workers + 1):
                core.release_agent(self.cfg, self.agent_id(w))
            if self.stopping:
                status = 'stopped'
            self._save(status=status, error=error, finished=core.now().isoformat(), current={})
            core.build_index(self.cfg)
            self.log(f'job {status}' + (f': {error}' if error else ''))


def run_job(cfg: Config, job_id: str):
    asyncio.run(JobRunner(cfg, job_id).run())
