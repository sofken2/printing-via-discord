import asyncio
import logging
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands

from .config import Config
from .convert import ConvertError, detect_kind, to_pdf
from .db import Database, JobRecord
from .ipp import (
    JOB_ABORTED, JOB_CANCELED, JOB_PENDING, JOB_PROCESSING, JOB_STOPPED,
    IppClient, IppError, IppNotFound, JobStatus, PrinterStatus,
)
from .pdf_utils import PdfError, inspect_pdf, parse_page_ranges, render_preview
from .printing import Cups, PrintError, job_number

log = logging.getLogger(__name__)

POLL_INTERVAL_SEC = 3
STALL_NOTICE_SEC = 90
MAX_WATCH_SEC = 24 * 60 * 60
# Interaction tokens expire after 15 minutes; stop editing the ephemeral panel a bit before.
PANEL_TTL_SEC = 14 * 60

COLOR_CHOICES = [
    app_commands.Choice(name="モノクロ", value="mono"),
    app_commands.Choice(name="カラー", value="color"),
]


HELP_COOLDOWN_SEC = 30


def help_text(cfg: Config) -> str:
    daily = f"{cfg.daily_sheets_per_user}枚" if cfg.daily_sheets_per_user > 0 else "なし"
    return "\n".join([
        "## 🖨️ 印刷Botの使い方",
        "**1. `/print` と入力して、ファイルを添付して送信**",
        "　PDF・画像（JPEG/PNG など）・Word/Excel/PowerPoint・テキストに対応しています。",
        "**2. 確認画面のボタンで設定**",
        "　・部数: 一番上のメニューで選択",
        "　・色: 「⚫ モノクロ」「🎨 カラー」ボタン（青いほうが選択中）",
        "　・ページ: 「📄 ページ指定」で `1-3,5` のように入力（空欄で全ページ）",
        "**3. プレビューと枚数を確認して「印刷する」**",
        "　印刷の進み具合は画面に表示され、終わったら DM でお知らせします。",
        "",
        "**その他のコマンド**",
        "`/queue` 印刷待ちの一覧　`/cancel` 自分のジョブを取り消し　`/status` プリンターの状態と本日の利用枚数",
        "",
        f"-# 上限: {cfg.max_file_bytes // 1024 // 1024}MB・{cfg.max_pages}ページ・{cfg.max_copies}部・"
        f"1日 {daily}／やり取りはあなたにだけ表示されます",
    ])


def _safe_title(name: str) -> str:
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip()
    return (name or "document.pdf")[:100]


class PrintSetup:
    """What the user picked on the confirmation screen, and whether it can be printed."""

    def __init__(self, *, total: int, max_pages: int, copies: int, color: bool):
        self.total = total
        self.max_pages = max_pages
        self.copies = copies
        self.color = color
        self.selection = parse_page_ranges(None, total)
        self.page_error: Optional[str] = None
        self.limit_error: Optional[str] = None

    def set_pages(self, spec: Optional[str]) -> None:
        try:
            self.selection = parse_page_ranges(spec, self.total)
            self.page_error = None
        except PdfError as e:
            self.page_error = f"{e}（前の指定のままです）"

    @property
    def sheets(self) -> int:
        return self.selection.count * self.copies

    @property
    def too_many_pages(self) -> bool:
        return self.selection.count > self.max_pages

    @property
    def first_page(self) -> int:
        ranges = self.selection.cups_ranges
        return int(re.match(r"\d+", ranges).group()) if ranges else 1

    @property
    def printable(self) -> bool:
        return not self.too_many_pages and not self.limit_error

    def summary(self, title: str, kind: str, printer_note: Optional[str]) -> str:
        sel = self.selection
        head = f"**{title}** を印刷しますか？"
        if kind != "PDF":
            head += f"（{kind}をPDFに変換しました）"
        lines = [
            head,
            f"・ページ: **{sel.cups_ranges or '全ページ'}**（{sel.count}ページ / 全{self.total}ページ）",
            f"・部数: **{self.copies}部**　→ 合計 **{self.sheets}枚**（片面）",
            f"・色: **{'🎨 カラー' if self.color else '⚫ モノクロ'}**",
        ]
        if self.page_error:
            lines.append(f"⚠️ {self.page_error}")
        if self.too_many_pages:
            lines.append(f"❌ ページ数が多すぎます（上限 {self.max_pages}ページ）。「ページ指定」で範囲を絞ってください。")
        if self.limit_error:
            lines.append(self.limit_error)
        if printer_note:
            lines.append(printer_note)
        lines.append("-# 下のボタンで部数・色・ページを変更して、「印刷する」を押してください。")
        return "\n".join(lines)


class PagesModal(discord.ui.Modal, title="ページ指定"):
    def __init__(self, view: "PrintSetupView"):
        super().__init__(timeout=view.timeout)
        self.setup_view = view
        self.spec = discord.ui.TextInput(
            label=f"印刷するページ（全{view.setup.total}ページ）",
            placeholder="例: 1-3,5　（空欄で全ページ）",
            default=view.setup.selection.cups_ranges or "",
            required=False, max_length=200,
        )
        self.add_item(self.spec)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        self.setup_view.setup.set_pages(self.spec.value)
        await self.setup_view.refresh(interaction)


class CopiesSelect(discord.ui.Select):
    def __init__(self, max_copies: int, current: int):
        super().__init__(row=0, options=[
            discord.SelectOption(label=f"{n}部", value=str(n), default=n == current)
            for n in range(1, min(max_copies, 25) + 1)
        ])

    async def callback(self, interaction: discord.Interaction) -> None:
        view: PrintSetupView = self.view  # type: ignore[assignment]
        view.setup.copies = int(self.values[0])
        await view.refresh(interaction)


class PrintSetupView(discord.ui.View):
    """Confirmation screen with buttons for copies, colour and page range.

    The timeout restarts on every interaction, so changing settings does not expire it.
    """

    def __init__(self, bot: "PrintBot", owner_id: int, setup: PrintSetup, *, pdf: Path,
                 preview_png: Path, title: str, kind: str, printer_note: Optional[str], timeout: float):
        super().__init__(timeout=timeout)
        self.bot, self.owner_id, self.setup = bot, owner_id, setup
        self.pdf, self.preview_png = pdf, preview_png
        self.title, self.kind, self.printer_note = title, kind, printer_note
        self.confirmed: Optional[bool] = None
        self._preview_page: Optional[int] = None
        self._has_preview = False
        self.copies_select = CopiesSelect(bot.config.max_copies, setup.copies)
        self.add_item(self.copies_select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.owner_id

    def _check_limit(self) -> None:
        self.setup.limit_error = _check_daily_limit(self.bot, self.owner_id, self.setup.sheets)

    def _sync_items(self) -> None:
        for opt in self.copies_select.options:
            opt.default = opt.value == str(self.setup.copies)
        self.mono_btn.style = discord.ButtonStyle.primary if not self.setup.color else discord.ButtonStyle.secondary
        self.color_btn.style = discord.ButtonStyle.primary if self.setup.color else discord.ButtonStyle.secondary
        sel = self.setup.selection
        self.pages_btn.label = f"📄 ページ指定（{sel.cups_ranges or '全ページ'}）"[:80]
        self.ok.disabled = not self.setup.printable

    async def _attachments(self) -> Optional[list[discord.File]]:
        """New preview if the first page changed, otherwise None (keep the current one)."""
        page = self.setup.first_page
        if page == self._preview_page:
            return None
        self._preview_page = page
        preview = await render_preview(self.pdf, page, self.preview_png)
        self._has_preview = preview is not None
        return [discord.File(preview, filename="preview.png")] if preview else []

    async def render(self) -> dict:
        self._check_limit()
        self._sync_items()
        kwargs: dict = {"content": self.setup.summary(self.title, self.kind, self.printer_note), "view": self}
        files = await self._attachments()
        if files is not None:
            kwargs["attachments"] = files
        return kwargs

    async def refresh(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()  # rendering a preview can take >3s on big files
        await interaction.edit_original_response(**await self.render())

    @discord.ui.button(label="⚫ モノクロ", row=1)
    async def mono_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.setup.color = False
        await self.refresh(interaction)

    @discord.ui.button(label="🎨 カラー", row=1)
    async def color_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.setup.color = True
        await self.refresh(interaction)

    @discord.ui.button(label="📄 ページ指定", style=discord.ButtonStyle.secondary, row=2)
    async def pages_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(PagesModal(self))

    @discord.ui.button(label="印刷する", style=discord.ButtonStyle.success, row=3)
    async def ok(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.confirmed = True
        await interaction.response.edit_message(content="⏳ 送信中…", view=None, attachments=[])
        self.stop()

    @discord.ui.button(label="キャンセル", style=discord.ButtonStyle.secondary, row=3)
    async def ng(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.confirmed = False
        await interaction.response.edit_message(content="キャンセルしました。", view=None, attachments=[])
        self.stop()


class GuardedTree(app_commands.CommandTree):
    """Rejects every command outside the configured guild or without the allowed role."""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        bot: PrintBot = interaction.client  # type: ignore[assignment]
        cfg = bot.config
        if interaction.guild_id != cfg.guild_id or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("このサーバーでは利用できません。", ephemeral=True)
            return False
        if cfg.allowed_role_ids and not bot.is_admin(interaction.user):
            if not any(r.id in cfg.allowed_role_ids for r in interaction.user.roles):
                await interaction.response.send_message("印刷する権限がありません。", ephemeral=True)
                return False
        return True

    async def on_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.CommandOnCooldown):
            msg = f"連続実行はできません。{error.retry_after:.0f}秒後に再度お試しください。"
        else:
            log.exception("command failed", exc_info=error)
            msg = "内部エラーが発生しました。管理者に連絡してください。"
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)


class PrintBot(discord.Client):
    def __init__(self, config: Config):
        intents = discord.Intents.none()
        intents.guilds = True
        # Lets the bot see messages that @mention it (for the help reply). This is not the
        # privileged Message Content intent: Discord still sends the content of mentions.
        intents.guild_messages = True
        super().__init__(intents=intents)
        self.config = config
        self._help_replied: dict[int, float] = {}
        self.tree = GuardedTree(self)
        self.cups = Cups(config.printer, config.extra_options)
        self.ipp = IppClient(config.printer, config.cups_server)
        self.db = Database(config.db_path)
        # Serializes the "check daily limit -> submit -> record" sequence.
        self.submit_lock = asyncio.Lock()
        self._watchers: set[asyncio.Task] = set()
        config.work_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        _register_commands(self)

    def is_admin(self, member: discord.Member) -> bool:
        return member.guild_permissions.manage_guild or any(
            r.id in self.config.admin_role_ids for r in member.roles
        )

    async def setup_hook(self) -> None:
        guild = discord.Object(self.config.guild_id)
        await self.tree.sync(guild=guild)
        for rec in self.db.queued():
            self.watch(rec)
        log.info("commands synced; resumed %d watcher(s)", len(self._watchers))

    async def on_ready(self) -> None:
        log.info("logged in as %s", self.user)
        for g in self.guilds:
            await self._leave_if_foreign(g)

    async def on_message(self, message: discord.Message) -> None:
        """Reply with the help text when someone @mentions the bot in the configured server."""
        if (message.author.bot or message.guild is None or message.guild.id != self.config.guild_id
                or self.user is None or self.user not in message.mentions):
            return
        now = time.monotonic()
        if now - self._help_replied.get(message.author.id, -HELP_COOLDOWN_SEC) < HELP_COOLDOWN_SEC:
            return  # do not let one person flood the channel
        self._help_replied[message.author.id] = now
        try:
            try:
                await message.reply(help_text(self.config), mention_author=False, silent=True)
            except discord.Forbidden:
                # replying needs "Read Message History"; fall back to a plain message
                await message.channel.send(help_text(self.config), silent=True)
        except discord.HTTPException as e:
            log.info("could not send help reply: %s", e)

    async def on_guild_join(self, guild: discord.Guild) -> None:
        await self._leave_if_foreign(guild)

    async def _leave_if_foreign(self, guild: discord.Guild) -> None:
        if guild.id != self.config.guild_id:
            log.warning("leaving unexpected guild %s (%s)", guild.name, guild.id)
            await guild.leave()

    async def close(self) -> None:
        for t in self._watchers:
            t.cancel()
        await super().close()
        self.db.close()

    async def printer_status(self) -> Optional[PrinterStatus]:
        try:
            return await self.ipp.printer_status()
        except IppError as e:
            log.warning("printer status unavailable: %s", e)
            return None

    # --- job watching -----------------------------------------------------

    def watch(self, rec: JobRecord, panel: Optional[discord.Interaction] = None) -> None:
        task = asyncio.create_task(self._watch(rec, panel))
        self._watchers.add(task)
        task.add_done_callback(self._watchers.discard)

    async def _watch(self, rec: JobRecord, panel: Optional[discord.Interaction] = None) -> None:
        """Poll the job over IPP, keep the requester's ephemeral panel updated, report the result."""
        started = time.monotonic()
        last_shown = None
        stall_notified = False
        while time.monotonic() - started < MAX_WATCH_SEC:
            elapsed = time.monotonic() - started
            try:
                status: Optional[JobStatus] = await self.ipp.job_status(job_number(rec.cups_job_id))
            except IppNotFound:
                status = None  # job history already purged by CUPS
            except IppError as e:
                log.warning("cannot read state of %s: %s", rec.cups_job_id, e)
                await asyncio.sleep(POLL_INTERVAL_SEC)
                continue

            printer = None
            if status is None or status.final or status.state in (JOB_PENDING, JOB_STOPPED):
                printer = await self.printer_status()

            shown = (status, printer.problems() if printer else None)
            if panel and shown != last_shown and elapsed < PANEL_TTL_SEC:
                last_shown = shown
                await self._update_panel(panel, rec, status, printer)

            if status is None or status.final:
                await self._finish(rec, status, printer)
                return

            if not stall_notified and elapsed >= STALL_NOTICE_SEC and (
                status.state in (JOB_PENDING, JOB_STOPPED) or (printer and printer.problems())
            ):
                stall_notified = True
                problems = "、".join(printer.problems()) if printer and printer.problems() else ""
                await self.notify(
                    rec,
                    f"⚠️ `{rec.file_name}` がまだ印刷されていません（{status.label}）。"
                    + (f"\nプリンター: **{problems}**" if problems else
                       "\nプリンターの電源・用紙・インクを確認してください。"),
                    f"⚠️ 印刷ジョブ `{rec.cups_job_id}` がまだ印刷されていません。プリンターを確認してください。",
                )
            await asyncio.sleep(POLL_INTERVAL_SEC)
        log.warning("gave up watching %s", rec.cups_job_id)

    async def _update_panel(self, panel: discord.Interaction, rec: JobRecord,
                            status: Optional[JobStatus], printer: Optional[PrinterStatus]) -> None:
        try:
            await panel.edit_original_response(content=format_panel(rec, status, printer))
        except discord.HTTPException as e:
            log.info("could not update status panel: %s", e)

    async def _finish(self, rec: JobRecord, status: Optional[JobStatus],
                      printer: Optional[PrinterStatus]) -> None:
        if status is None:
            self.db.set_status(rec.cups_job_id, "done")
            await self.notify(
                rec,
                f"✅ 印刷が完了しました: `{rec.file_name}`（{rec.sheets}枚）\n"
                "※ 詳細な結果はCUPSの履歴から取得できませんでした",
                f"✅ 印刷ジョブ `{rec.cups_job_id}` が完了しました。",
            )
        elif status.succeeded:
            self.db.set_status(rec.cups_job_id, "done")
            await self.notify(
                rec,
                f"✅ 印刷が完了しました: `{rec.file_name}`（{rec.sheets}枚）",
                f"✅ 印刷ジョブ `{rec.cups_job_id}` が完了しました。",
            )
        elif status.state == JOB_CANCELED:
            # /cancel by the owner already marked it; otherwise an admin or CUPS canceled it.
            if self.db.set_status(rec.cups_job_id, "canceled"):
                await self.notify(
                    rec,
                    f"🗑️ `{rec.file_name}` の印刷は取り消されました。",
                    f"🗑️ 印刷ジョブ `{rec.cups_job_id}` は取り消されました。",
                )
        else:  # aborted or completed with errors
            self.db.set_status(rec.cups_job_id, "failed" if status.state == JOB_ABORTED else "done")
            reason = failure_reason(status, printer)
            await self.notify(
                rec,
                f"{status.label}: `{rec.file_name}`" + (f"\n理由: {reason}" if reason else ""),
                f"{status.label}: 印刷ジョブ `{rec.cups_job_id}`（詳細はDMを有効にすると届きます）",
            )
        log.info("job %s finished: %s", rec.cups_job_id, status.label if status else "unknown")

    async def notify(self, rec: JobRecord, private_text: str, public_text: str) -> None:
        """DM the requester. If DMs are closed, post a mention without the file name instead."""
        try:
            user = self.get_user(rec.user_id) or await self.fetch_user(rec.user_id)
            await user.send(private_text)
            return
        except discord.HTTPException as e:
            log.info("DM to %s failed (%s); falling back to channel", rec.user_id, e)
        if not rec.channel_id:
            return
        try:
            channel = self.get_channel(rec.channel_id) or await self.fetch_channel(rec.channel_id)
            await channel.send(
                f"<@{rec.user_id}> {public_text}",
                allowed_mentions=discord.AllowedMentions(users=[discord.Object(rec.user_id)]),
            )
        except discord.HTTPException as e:
            log.warning("could not notify user %s: %s", rec.user_id, e)


def failure_reason(status: JobStatus, printer: Optional[PrinterStatus]) -> str:
    parts = []
    if printer and printer.problems():
        parts.append("、".join(printer.problems()))
    if status.message:
        parts.append(status.message)
    return " / ".join(parts)


def format_panel(rec: JobRecord, status: Optional[JobStatus], printer: Optional[PrinterStatus]) -> str:
    label = status.label if status else "✅ 印刷完了"
    lines = [
        f"🖨️ **{rec.file_name}**（ジョブ `{rec.cups_job_id}` / {rec.sheets}枚）",
        f"状態: **{label}**",
    ]
    progress = format_progress(rec, status)
    if progress:
        lines.append(progress)
    if status and status.message:
        lines.append(f"詳細: {status.message}")
    if printer and printer.problems():
        lines.append(f"プリンター: ⚠️ {'、'.join(printer.problems())}")
    if status is None or not status.final:
        lines.append(f"最終更新: <t:{int(time.time())}:T>（結果はDMでもお知らせします）")
    return "\n".join(lines)


def format_progress(rec: JobRecord, status: Optional[JobStatus]) -> Optional[str]:
    """"進捗: ▓▓▓░░░ 3 / 10枚" while the job is printing (only if CUPS reports a count)."""
    if status is None or status.state != JOB_PROCESSING or rec.sheets < 2 or status.impressions < 1:
        return None
    done = min(status.impressions, rec.sheets)
    filled = round(done / rec.sheets * 10)
    return f"進捗: `{'▓' * filled}{'░' * (10 - filled)}` **{done} / {rec.sheets}枚**"


def format_printer(printer: PrinterStatus) -> str:
    lines = [f"**{printer.label}**" + ("" if printer.accepting else "（ジョブ受付停止中）")]
    if printer.problems():
        lines.append("⚠️ " + "、".join(printer.problems()))
    if printer.message:
        lines.append(f"メッセージ: {printer.message}")
    return "\n".join(lines)


def _register_commands(bot: PrintBot) -> None:
    cfg = bot.config
    guild = discord.Object(cfg.guild_id)
    tree = bot.tree

    @tree.command(name="print", description="PDF・画像・Office文書などを印刷します", guild=guild)
    @app_commands.describe(
        file="印刷するファイル（PDF、画像、Word/Excel/PowerPoint、テキストなど）",
        copies=f"部数（1〜{cfg.max_copies}）",
        pages="ページ範囲（例: 1-3,5）。省略で全ページ",
        color="カラー/モノクロ（省略でモノクロ）",
    )
    @app_commands.choices(color=COLOR_CHOICES)
    @app_commands.checks.cooldown(1, cfg.cooldown_sec, key=lambda i: i.user.id)
    async def print_cmd(
        interaction: discord.Interaction,
        file: discord.Attachment,
        copies: app_commands.Range[int, 1, cfg.max_copies] = 1,
        pages: Optional[str] = None,
        color: Optional[app_commands.Choice[str]] = None,
    ):
        await interaction.response.defer(ephemeral=True, thinking=True)
        if file.size > cfg.max_file_bytes:
            await interaction.followup.send(
                f"ファイルが大きすぎます（上限 {cfg.max_file_bytes // 1024 // 1024}MB）。", ephemeral=True)
            return

        try:
            kind = detect_kind(file.filename)  # reject unsupported types before downloading
        except ConvertError as e:
            await interaction.followup.send(f"❌ {e}", ephemeral=True)
            return

        work = cfg.work_dir / uuid.uuid4().hex
        work.mkdir(mode=0o700)
        png_path = work / "preview.png"
        try:
            src = work / "upload"
            await file.save(src)
            try:
                if kind != "pdf":
                    await interaction.edit_original_response(content="🔄 PDFに変換しています…")
                converted = await to_pdf(src, file.filename, work, cfg.work_dir / ".libreoffice")
                pdf_path = converted.pdf
                total = await asyncio.to_thread(inspect_pdf, pdf_path)
            except PdfError as e:
                await interaction.edit_original_response(content=f"❌ {e}")
                return

            # The command options only set the starting values; everything can be changed with buttons.
            setup = PrintSetup(total=total, max_pages=cfg.max_pages, copies=copies,
                               color=color is not None and color.value == "color")
            if pages:
                setup.set_pages(pages)

            printer = await bot.printer_status()
            printer_note = None
            if printer is None:
                printer_note = "⚠️ プリンターの状態を取得できませんでした。"
            elif not printer.usable or printer.problems():
                problems = "、".join(printer.problems()) or "停止中"
                printer_note = f"⚠️ プリンター: {problems}。印刷はプリンター復帰後になります。"

            title = _safe_title(file.filename)
            view = PrintSetupView(bot, interaction.user.id, setup, pdf=pdf_path, preview_png=png_path,
                                  title=title, kind=converted.kind, printer_note=printer_note,
                                  timeout=cfg.confirm_timeout_sec)
            await interaction.edit_original_response(**await view.render())

            if await view.wait():  # timed out
                await interaction.edit_original_response(
                    content="⌛ 時間切れのためキャンセルしました。", view=None, attachments=[])
                return
            if not view.confirmed:
                return

            selection, copies, is_color, sheets = setup.selection, setup.copies, setup.color, setup.sheets
            async with bot.submit_lock:
                limit_msg = _check_daily_limit(bot, interaction.user.id, sheets)
                if limit_msg:
                    await interaction.edit_original_response(content=limit_msg)
                    return
                try:
                    job_id = await bot.cups.submit(
                        pdf_path,
                        copies=copies,
                        page_ranges=selection.cups_ranges,
                        options=cfg.color_options if is_color else cfg.mono_options,
                        title=title,
                    )
                except PrintError as e:
                    log.error("submit failed: %s", e)
                    await interaction.edit_original_response(content=f"❌ {e}")
                    return
                bot.db.add_job(
                    cups_job_id=job_id, user_id=interaction.user.id,
                    user_name=str(interaction.user), channel_id=interaction.channel_id,
                    file_name=title, pages=selection.count,
                    copies=copies, color=is_color,
                )
            log.info("job %s submitted by %s (%d sheets)", job_id, interaction.user, sheets)
            rec = bot.db.get(job_id)
            if rec:
                await interaction.edit_original_response(
                    content=format_panel(rec, JobStatus(JOB_PENDING), None))
                bot.watch(rec, panel=interaction)
        finally:
            # lp has already sent the data to cupsd, so the local copies can go.
            shutil.rmtree(work, ignore_errors=True)

    @tree.command(name="queue", description="印刷待ちのジョブ一覧を表示します", guild=guild)
    async def queue_cmd(interaction: discord.Interaction):
        try:
            jobs = await bot.cups.active_jobs()
        except PrintError as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return
        if not jobs:
            await interaction.response.send_message("印刷待ちのジョブはありません。", ephemeral=True)
            return
        lines = []
        for j in jobs:
            rec = bot.db.get(j.job_id)
            try:
                state = (await bot.ipp.job_status(job_number(j.job_id))).label
            except IppError:
                state = "状態不明"
            if rec and rec.user_id == interaction.user.id:
                who = f"あなた `{rec.file_name}`（{rec.sheets}枚）"
            elif rec:
                who = f"他のメンバー（{rec.sheets}枚）"  # file names stay private
            else:
                who = "Bot外のジョブ"
            lines.append(f"・`{j.job_id}` {state} — {who}")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @tree.command(name="help", description="使い方とコマンドの説明を表示します", guild=guild)
    async def help_cmd(interaction: discord.Interaction):
        await interaction.response.send_message(help_text(cfg), ephemeral=True)

    @tree.command(name="cancel", description="印刷ジョブを取り消します", guild=guild)
    @app_commands.describe(job_id="ジョブID（/queue で確認できます）")
    async def cancel_cmd(interaction: discord.Interaction, job_id: str):
        job_id = bot.cups.normalize_job_id(job_id)
        rec = bot.db.get(job_id)
        member = interaction.user
        assert isinstance(member, discord.Member)
        if not bot.is_admin(member) and (rec is None or rec.user_id != member.id):
            await interaction.response.send_message("自分のジョブ以外は取り消せません。", ephemeral=True)
            return
        try:
            if not await bot.cups.is_active(job_id):
                await interaction.response.send_message("そのジョブは印刷待ちにありません。", ephemeral=True)
                return
            await bot.cups.cancel(job_id)
        except PrintError as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return
        if rec and rec.user_id == member.id:
            bot.db.set_status(job_id, "canceled")  # owner already knows; keep the watcher quiet
        await interaction.response.send_message(f"🗑️ `{job_id}` を取り消しました。", ephemeral=True)

    @tree.command(name="status", description="プリンターの状態を表示します", guild=guild)
    async def status_cmd(interaction: discord.Interaction):
        printer = await bot.printer_status()
        try:
            queued = len(await bot.cups.active_jobs())
        except PrintError:
            queued = "?"
        used = bot.db.sheets_today(interaction.user.id)
        limit = cfg.daily_sheets_per_user
        quota = f"{used} / {limit}枚" if limit > 0 else f"{used}枚（上限なし）"
        body = format_printer(printer) if printer else "🔴 プリンターの状態を取得できませんでした（CUPSを確認してください）"
        await interaction.response.send_message(
            f"{body}\n印刷待ち: {queued}件　本日のあなたの利用: {quota}",
            ephemeral=True,
        )


def _check_daily_limit(bot: PrintBot, user_id: int, sheets: int) -> Optional[str]:
    limit = bot.config.daily_sheets_per_user
    if limit <= 0:
        return None
    used = bot.db.sheets_today(user_id)
    if used + sheets > limit:
        return f"❌ 本日の印刷上限を超えます（本日 {used}枚 + 今回 {sheets}枚 > 上限 {limit}枚）。"
    return None


def run(config: Config) -> None:
    PrintBot(config).run(config.token, log_handler=None)
