import asyncio

import discord
import pytest
from pypdf import PdfWriter

from printbot import bot as botmod
from printbot.bot import PrintSetup, PrintSetupView, format_panel, format_progress
from printbot.config import load_config
from printbot.db import JobRecord
from printbot.ipp import JobStatus


def _setup(total=10, max_pages=30, copies=1, color=False):
    return PrintSetup(total=total, max_pages=max_pages, copies=copies, color=color)


def test_defaults_all_pages_mono():
    s = _setup()
    assert (s.selection.cups_ranges, s.selection.count, s.sheets, s.first_page) == (None, 10, 10, 1)
    assert s.printable
    assert "全ページ" in s.summary("a.pdf", "PDF", None)
    assert "モノクロ" in s.summary("a.pdf", "PDF", None)


def test_pages_and_copies_change_sheets():
    s = _setup()
    s.set_pages("3-5,8")
    s.copies = 2
    assert (s.selection.cups_ranges, s.sheets, s.first_page) == ("3-5,8", 8, 3)


def test_invalid_pages_keep_previous_selection():
    s = _setup()
    s.set_pages("2-3")
    s.set_pages("99")
    assert s.selection.cups_ranges == "2-3"
    assert s.page_error and "前の指定のまま" in s.summary("a.pdf", "PDF", None)
    s.set_pages("")  # back to all pages clears the error
    assert s.selection.cups_ranges is None and s.page_error is None


def test_too_many_pages_blocks_until_narrowed():
    s = _setup(total=40, max_pages=30)
    assert not s.printable
    assert "ページ指定" in s.summary("a.pdf", "PDF", None)
    s.set_pages("1-30")
    assert s.printable


def test_converted_kind_is_mentioned():
    assert "画像をPDFに変換しました" in _setup().summary("a.png", "画像", None)


def _rec(sheets):
    return JobRecord(cups_job_id="P-1", user_id=1, channel_id=None, file_name="f.pdf", sheets=sheets)


@pytest.mark.parametrize("status,sheets,expected", [
    (JobStatus(5, impressions=3), 10, "3 / 10枚"),
    (JobStatus(5, impressions=12), 10, "10 / 10枚"),  # never overshoot
    (JobStatus(5, impressions=0), 10, None),           # nothing reported yet
    (JobStatus(5, impressions=1), 1, None),            # single sheet: no bar
    (JobStatus(3, impressions=3), 10, None),           # not printing
    (None, 10, None),
])
def test_format_progress(status, sheets, expected):
    out = format_progress(_rec(sheets), status)
    assert (out is None) if expected is None else (expected in out)


def test_panel_shows_progress():
    assert "▓" in format_panel(_rec(4), JobStatus(5, impressions=2), None)


@pytest.fixture
def printbot(tmp_path, monkeypatch):
    cfg = tmp_path / "c.toml"
    cfg.write_text(f'guild_id = 1\n[printer]\nname = "P"\n[limits]\nmax_copies = 3\n'
                   f'daily_sheets_per_user = 12\n'
                   f'[paths]\ndb = "{tmp_path}/x.db"\nwork_dir = "{tmp_path}/tmp"\n')
    monkeypatch.setenv("DISCORD_TOKEN", "x")
    b = botmod.PrintBot(load_config(cfg))
    yield b
    b.db.close()


def test_view_buttons_follow_settings(printbot, tmp_path):
    pdf = tmp_path / "d.pdf"
    w = PdfWriter()
    for _ in range(5):
        w.add_blank_page(width=595, height=842)
    w.write(pdf)

    async def go():
        setup = _setup(total=5)
        view = PrintSetupView(printbot, 1, setup, pdf=pdf, preview_png=tmp_path / "p.png",
                              title="d.pdf", kind="PDF", printer_note=None, timeout=60)
        first = await view.render()
        assert [o.label for o in view.copies_select.options] == ["1部", "2部", "3部"]
        assert view.mono_btn.style is discord.ButtonStyle.primary
        assert view.color_btn.style is discord.ButtonStyle.secondary
        assert "全ページ" in view.pages_btn.label and not view.ok.disabled
        assert "attachments" in first  # first render sends the preview

        setup.color, setup.copies = True, 3
        setup.set_pages("2-5")  # 4 pages x 3 = 12 sheets = daily limit, still ok
        again = await view.render()
        assert view.color_btn.style is discord.ButtonStyle.primary
        assert "2-5" in view.pages_btn.label and not view.ok.disabled
        assert [o.default for o in view.copies_select.options] == [False, False, True]
        assert "attachments" in again  # first page changed 1 -> 2: new preview

        setup.set_pages("1-5")  # 15 sheets > 12: print button disabled
        await view.render()
        assert view.ok.disabled and "上限" in setup.summary("d.pdf", "PDF", None)

        setup.set_pages("1-4")  # same first page: keep the preview
        assert "attachments" not in await view.render()
    asyncio.run(go())


def test_help_text_mentions_buttons_and_limits(printbot):
    text = botmod.help_text(printbot.config)
    for word in ("/print", "モノクロ", "カラー", "ページ指定", "印刷する", "/queue", "/cancel", "/status",
                 "3部", "1日 12枚"):
        assert word in text
    assert len(text) <= 2000  # Discord message limit


class _User:
    def __init__(self, uid, bot=False):
        self.id, self.bot = uid, bot

    def __eq__(self, other):
        return isinstance(other, _User) and other.id == self.id

    def __hash__(self):
        return self.id


class _Guild:
    id = 1


class _Message:
    def __init__(self, author, mentions, guild=_Guild()):
        self.author, self.mentions, self.guild = author, mentions, guild
        self.replies = []

    async def reply(self, text, **kw):
        self.replies.append(text)


def test_mention_replies_with_help_once_per_cooldown(printbot, monkeypatch):
    me = _User(999, bot=True)
    monkeypatch.setattr(type(printbot), "user", property(lambda self: me))

    async def go():
        m = _Message(_User(5), [me])
        await printbot.on_message(m)
        assert len(m.replies) == 1 and "使い方" in m.replies[0]
        again = _Message(_User(5), [me])
        await printbot.on_message(again)
        assert again.replies == []  # cooldown
        for msg in (_Message(_User(6), []),               # not mentioned
                    _Message(_User(7, bot=True), [me]),    # from a bot
                    _Message(_User(8), [me], guild=None)):  # DM
            await printbot.on_message(msg)
            assert msg.replies == []
    asyncio.run(go())
