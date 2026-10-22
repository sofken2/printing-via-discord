import asyncio
import logging
import re
import uuid
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands

from .config import Config
from .db import Database, JobRecord
from .pdf_utils import PdfError, inspect_pdf, parse_page_ranges, render_preview
from .printing import Cups, PrintError

log = logging.getLogger(__name__)

POLL_INTERVAL_SEC = 5
STALL_NOTICE_SEC = 90
MAX_WATCH_SEC = 24 * 60 * 60

COLOR_CHOICES = [
    app_commands.Choice(name="モノクロ", value="mono"),
    app_commands.Choice(name="カラー", value="color"),
]


def _safe_title(name: str) -> str:
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip()
    return (name or "document.pdf")[:100]


class ConfirmView(discord.ui.View):
    def __init__(self, owner_id: int, timeout: float):
        super().__init__(timeout=timeout)
        self.owner_id = owner_id
        self.confirmed: Optional[bool] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.owner_id

    @discord.ui.button(label="印刷する", style=discord.ButtonStyle.success)
    async def ok(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.confirmed = True
        await interaction.response.edit_message(content="⏳ 送信中…", view=None, attachments=[])
        self.stop()

    @discord.ui.button(label="キャンセル", style=discord.ButtonStyle.secondary)
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
        super().__init__(intents=intents)
        self.config = config
        self.tree = GuardedTree(self)
        self.cups = Cups(config.printer, config.extra_options)
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

    # --- job watching -----------------------------------------------------

    def watch(self, rec: JobRecord) -> None:
        task = asyncio.create_task(self._watch(rec))
        self._watchers.add(task)
        task.add_done_callback(self._watchers.discard)

    async def _watch(self, rec: JobRecord) -> None:
        waited = 0
        stall_notified = False
        while waited < MAX_WATCH_SEC:
            await asyncio.sleep(POLL_INTERVAL_SEC)
            waited += POLL_INTERVAL_SEC
            try:
                active = await self.cups.is_active(rec.cups_job_id)
            except PrintError as e:
                log.warning("lpstat failed while watching %s: %s", rec.cups_job_id, e)
                continue
            if not active:
                # /cancel already set the status, in which case we stay quiet.
                if self.db.set_status(rec.cups_job_id, "done"):
                    await self.notify(rec, f"✅ 印刷が完了しました: `{rec.file_name}`（{rec.sheets}枚）")
                return
            if not stall_notified and waited >= STALL_NOTICE_SEC:
                stall_notified = True
                _, status = await self.cups.status()
                await self.notify(
                    rec,
                    f"⚠️ `{rec.file_name}` がまだ印刷待ちです。プリンターの電源・用紙・インクを確認してください。\n"
                    f"```\n{status[:1500]}\n```",
                )
        log.warning("gave up watching %s", rec.cups_job_id)

    async def notify(self, rec: JobRecord, text: str) -> None:
        mention = discord.AllowedMentions(users=[discord.Object(rec.user_id)])
        try:
            if rec.channel_id:
                channel = self.get_channel(rec.channel_id) or await self.fetch_channel(rec.channel_id)
                await channel.send(f"<@{rec.user_id}> {text}", allowed_mentions=mention)
                return
        except discord.HTTPException as e:
            log.warning("channel notify failed, falling back to DM: %s", e)
        try:
            user = self.get_user(rec.user_id) or await self.fetch_user(rec.user_id)
            await user.send(text)
        except discord.HTTPException as e:
            log.warning("could not notify user %s: %s", rec.user_id, e)


def _register_commands(bot: PrintBot) -> None:
    cfg = bot.config
    guild = discord.Object(cfg.guild_id)
    tree = bot.tree

    @tree.command(name="print", description="PDFをプリンターで印刷します", guild=guild)
    @app_commands.describe(
        file="印刷するPDFファイル",
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

        is_color = color is not None and color.value == "color"
        work = cfg.work_dir / uuid.uuid4().hex
        pdf_path, png_path = work.with_suffix(".pdf"), work.with_suffix(".png")
        try:
            await file.save(pdf_path)
            try:
                total = await asyncio.to_thread(inspect_pdf, pdf_path)
                selection = parse_page_ranges(pages, total)
            except PdfError as e:
                await interaction.followup.send(f"❌ {e}", ephemeral=True)
                return
            if selection.count > cfg.max_pages:
                await interaction.followup.send(
                    f"❌ ページ数が多すぎます（{selection.count}ページ / 上限 {cfg.max_pages}ページ）。"
                    "ページ範囲を指定してください。", ephemeral=True)
                return

            sheets = selection.count * copies
            limit_msg = _check_daily_limit(bot, interaction.user.id, sheets)
            if limit_msg:
                await interaction.followup.send(limit_msg, ephemeral=True)
                return

            first_page = int(re.match(r"\d+", selection.cups_ranges).group()) if selection.cups_ranges else 1
            preview = await render_preview(pdf_path, first_page, png_path)
            usable, _ = await bot.cups.status()

            lines = [
                f"**{_safe_title(file.filename)}** を印刷しますか？",
                f"・ページ: {selection.cups_ranges or '全ページ'}（{selection.count}ページ / 全{total}ページ）",
                f"・部数: {copies}　→ 合計 **{sheets}枚**（片面）",
                f"・{'カラー' if is_color else 'モノクロ'}",
            ]
            if not usable:
                lines.append("⚠️ プリンターが停止中です。印刷はプリンター復帰後になります。")
            view = ConfirmView(interaction.user.id, timeout=cfg.confirm_timeout_sec)
            attachments = [discord.File(preview, filename="preview.png")] if preview else []
            await interaction.edit_original_response(
                content="\n".join(lines), view=view, attachments=attachments)

            if await view.wait():  # timed out
                await interaction.edit_original_response(
                    content="⌛ 時間切れのためキャンセルしました。", view=None, attachments=[])
                return
            if not view.confirmed:
                return

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
                        title=_safe_title(file.filename),
                    )
                except PrintError as e:
                    log.error("submit failed: %s", e)
                    await interaction.edit_original_response(content=f"❌ {e}")
                    return
                bot.db.add_job(
                    cups_job_id=job_id, user_id=interaction.user.id,
                    user_name=str(interaction.user), channel_id=interaction.channel_id,
                    file_name=_safe_title(file.filename), pages=selection.count,
                    copies=copies, color=is_color,
                )
            log.info("job %s submitted by %s (%d sheets)", job_id, interaction.user, sheets)
            await interaction.edit_original_response(
                content=f"🖨️ 印刷キューに追加しました（ジョブID: `{job_id}`）。完了したらお知らせします。")
            rec = bot.db.get(job_id)
            if rec:
                bot.watch(rec)
        finally:
            # lp has already sent the data to cupsd, so the local copy can go.
            pdf_path.unlink(missing_ok=True)
            png_path.unlink(missing_ok=True)

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
            who = f"<@{rec.user_id}> `{rec.file_name}`（{rec.sheets}枚）" if rec else "（Bot外のジョブ）"
            lines.append(f"・`{j.job_id}` {who}")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

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
        bot.db.set_status(job_id, "canceled")
        await interaction.response.send_message(f"🗑️ `{job_id}` を取り消しました。", ephemeral=True)

    @tree.command(name="status", description="プリンターの状態を表示します", guild=guild)
    async def status_cmd(interaction: discord.Interaction):
        usable, text = await bot.cups.status()
        try:
            queued = len(await bot.cups.active_jobs())
        except PrintError:
            queued = "?"
        used = bot.db.sheets_today(interaction.user.id)
        limit = cfg.daily_sheets_per_user
        quota = f"{used} / {limit}枚" if limit > 0 else f"{used}枚（上限なし）"
        await interaction.response.send_message(
            f"{'🟢 利用可能' if usable else '🔴 停止中'}　印刷待ち: {queued}件　本日のあなたの利用: {quota}\n"
            f"```\n{text[:1500]}\n```",
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
