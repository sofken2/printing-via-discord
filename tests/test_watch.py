import asyncio

import pytest

from printbot import bot as botmod
from printbot.config import load_config


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
    b.sent = []

    async def notify(rec, text):
        b.sent.append(text)
    b.notify = notify
    yield b
    b.db.close()


class FakeCups:
    def __init__(self, active_polls):
        self.remaining = active_polls

    async def is_active(self, job_id):
        self.remaining -= 1
        return self.remaining >= 0

    async def status(self):
        return True, "idle"


def _add(b, job_id="P-1"):
    b.db.add_job(cups_job_id=job_id, user_id=10, user_name="u", channel_id=None,
                 file_name="a.pdf", pages=2, copies=3, color=False)
    return b.db.get(job_id)


def test_watch_notifies_on_completion(printbot):
    printbot.cups = FakeCups(active_polls=2)
    asyncio.run(printbot._watch(_add(printbot)))
    assert len(printbot.sent) == 1 and "完了" in printbot.sent[0]
    assert printbot.db.queued() == []
    assert printbot.db.sheets_today(10) == 6


def test_watch_is_silent_after_cancel(printbot):
    printbot.cups = FakeCups(active_polls=0)
    rec = _add(printbot)
    printbot.db.set_status(rec.cups_job_id, "canceled")
    asyncio.run(printbot._watch(rec))
    assert printbot.sent == []
    assert printbot.db.sheets_today(10) == 0


def test_watch_warns_when_stalled(printbot, monkeypatch):
    monkeypatch.setattr(botmod, "STALL_NOTICE_SEC", 0)
    printbot.cups = FakeCups(active_polls=3)
    asyncio.run(printbot._watch(_add(printbot)))
    assert len(printbot.sent) == 2 and "印刷待ち" in printbot.sent[0]
