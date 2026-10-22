import asyncio

import pytest

from printbot import bot as botmod
from printbot.config import load_config
from printbot.ipp import IppNotFound, JobStatus, PrinterStatus


@pytest.fixture
def printbot(tmp_path, monkeypatch):
    cfg_file = tmp_path / "c.toml"
    cfg_file.write_text(
        f'guild_id = 1\n[printer]\nname = "P"\n'
        f'[paths]\ndb = "{tmp_path}/x.db"\nwork_dir = "{tmp_path}/tmp"\n'
    )
    monkeypatch.setenv("DISCORD_TOKEN", "x")
    monkeypatch.setattr(botmod, "POLL_INTERVAL_SEC", 0)
    b = botmod.PrintBot(load_config(cfg_file))
    b.private, b.public = [], []

    async def notify(rec, private_text, public_text):
        b.private.append(private_text)
        b.public.append(public_text)
    b.notify = notify
    yield b
    b.db.close()


class FakeIpp:
    def __init__(self, states, printer=PrinterStatus(3)):
        self.states = list(states)
        self.printer = printer

    async def job_status(self, job_id):
        s = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        if s is None:
            raise IppNotFound("gone")
        return s

    async def printer_status(self):
        return self.printer


class FakePanel:
    def __init__(self):
        self.contents = []

    async def edit_original_response(self, content):
        self.contents.append(content)


def _add(b, job_id="P-1"):
    b.db.add_job(cups_job_id=job_id, user_id=10, user_name="u", channel_id=None,
                 file_name="secret.pdf", pages=2, copies=3, color=False)
    return b.db.get(job_id)


def test_success_updates_panel_and_notifies(printbot):
    printbot.ipp = FakeIpp([JobStatus(3), JobStatus(5, message="Sending data to printer."), JobStatus(9)])
    panel = FakePanel()
    asyncio.run(printbot._watch(_add(printbot), panel))
    assert [c.splitlines()[1] for c in panel.contents] == [
        "状態: **🕒 印刷待ち**", "状態: **📨 データ送信中・印刷中**", "状態: **✅ 印刷完了**"]
    assert "Sending data to printer." in panel.contents[1]
    assert len(printbot.private) == 1 and "完了" in printbot.private[0]
    assert printbot.db.queued() == [] and printbot.db.sheets_today(10) == 6


def test_failure_is_reported_with_reason(printbot):
    printbot.ipp = FakeIpp([JobStatus(5), JobStatus(8, message="Printer not responding")],
                           printer=PrinterStatus(5, reasons=("media-empty-error",)))
    asyncio.run(printbot._watch(_add(printbot)))
    assert "印刷失敗" in printbot.private[0]
    assert "用紙切れ" in printbot.private[0] and "Printer not responding" in printbot.private[0]
    assert printbot.db.sheets_today(10) == 0  # failed jobs don't use up the quota


def test_public_fallback_never_contains_file_name(printbot):
    printbot.ipp = FakeIpp([JobStatus(8)])
    asyncio.run(printbot._watch(_add(printbot)))
    printbot.ipp = FakeIpp([JobStatus(9)])
    asyncio.run(printbot._watch(_add(printbot, "P-2")))
    assert printbot.public and all("secret.pdf" not in t for t in printbot.public)


def test_owner_cancel_is_silent(printbot):
    printbot.ipp = FakeIpp([JobStatus(7)])
    rec = _add(printbot)
    printbot.db.set_status(rec.cups_job_id, "canceled")
    asyncio.run(printbot._watch(rec))
    assert printbot.private == []


def test_admin_cancel_notifies_owner(printbot):
    printbot.ipp = FakeIpp([JobStatus(7)])
    asyncio.run(printbot._watch(_add(printbot)))
    assert "取り消され" in printbot.private[0]


def test_purged_job_counts_as_done(printbot):
    printbot.ipp = FakeIpp([None])
    asyncio.run(printbot._watch(_add(printbot)))
    assert "完了" in printbot.private[0]


def test_stall_warning_mentions_printer_problem(printbot, monkeypatch):
    monkeypatch.setattr(botmod, "STALL_NOTICE_SEC", 0)
    printbot.ipp = FakeIpp([JobStatus(3), JobStatus(9)],
                           printer=PrinterStatus(3, reasons=("marker-supply-empty-error",)))
    asyncio.run(printbot._watch(_add(printbot)))
    assert "インク切れ" in printbot.private[0] and "完了" in printbot.private[1]
