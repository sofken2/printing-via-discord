"""Thin async wrapper around the CUPS command line tools (lp, lpstat, cancel)."""
from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from pathlib import Path

_REQUEST_ID_RE = re.compile(r"request id is (\S+)")


class PrintError(Exception):
    pass


def job_number(job_id: str) -> int:
    """"TS6730-12" -> 12"""
    return int(job_id.rsplit("-", 1)[-1])


@dataclass(frozen=True)
class QueuedJob:
    job_id: str
    size: str
    submitted: str


async def _run(*args: str, timeout: float = 30) -> tuple[int, str, str]:
    # Force English output so the parsers below work regardless of the system locale.
    env = {**os.environ, "LC_ALL": "C", "LANG": "C"}
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env,
        )
    except FileNotFoundError:
        raise PrintError(f"{args[0]} が見つかりません（cups-client をインストールしてください）")
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise PrintError(f"{args[0]} がタイムアウトしました")
    return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")


def parse_request_id(output: str) -> str:
    m = _REQUEST_ID_RE.search(output)
    if not m:
        raise PrintError(f"ジョブIDを取得できませんでした: {output.strip()}")
    return m.group(1)


def parse_lpstat_jobs(output: str) -> list[QueuedJob]:
    # Example line: "TS6730-12   printbot   102400   Wed 22 Oct 2026 10:00:00 AM JST"
    jobs = []
    for line in output.splitlines():
        parts = line.split(None, 3)
        if len(parts) >= 3 and not line.startswith((" ", "\t")):
            jobs.append(QueuedJob(parts[0], parts[2], parts[3] if len(parts) > 3 else ""))
    return jobs


class Cups:
    def __init__(self, printer: str, extra_options: tuple[str, ...] = ()):
        self.printer = printer
        self.extra_options = extra_options

    def normalize_job_id(self, job_id: str) -> str:
        job_id = job_id.strip()
        return f"{self.printer}-{job_id}" if job_id.isdigit() else job_id

    async def submit(self, pdf: Path, *, copies: int, page_ranges: str | None,
                     options: tuple[str, ...], title: str) -> str:
        args = ["lp", "-d", self.printer, "-n", str(copies), "-t", title]
        if page_ranges:
            args += ["-P", page_ranges]
        for opt in (*self.extra_options, *options):
            args += ["-o", opt]
        args += ["--", str(pdf)]
        code, out, err = await _run(*args)
        if code != 0:
            raise PrintError(f"印刷ジョブの登録に失敗しました: {(err or out).strip()}")
        return parse_request_id(out)

    async def active_jobs(self) -> list[QueuedJob]:
        code, out, err = await _run("lpstat", "-o", self.printer)
        if code != 0:
            raise PrintError(f"キューを取得できませんでした: {err.strip()}")
        return parse_lpstat_jobs(out)

    async def is_active(self, job_id: str) -> bool:
        return any(j.job_id == job_id for j in await self.active_jobs())

    async def cancel(self, job_id: str) -> None:
        code, out, err = await _run("cancel", job_id)
        if code != 0:
            raise PrintError(f"取り消しに失敗しました: {(err or out).strip()}")
