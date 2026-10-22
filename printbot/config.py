from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    token: str
    guild_id: int
    printer: str
    color_options: tuple[str, ...]
    mono_options: tuple[str, ...]
    extra_options: tuple[str, ...]
    max_file_bytes: int
    max_pages: int
    max_copies: int
    daily_sheets_per_user: int
    confirm_timeout_sec: int
    cooldown_sec: int
    allowed_role_ids: frozenset[int]
    admin_role_ids: frozenset[int]
    db_path: Path
    work_dir: Path


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader so the bot can run without systemd's EnvironmentFile."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def load_config(path: str | os.PathLike | None = None) -> Config:
    _load_dotenv(Path(".env"))
    path = Path(path or os.environ.get("PRINTBOT_CONFIG", "config.toml"))
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"{path} が見つかりません（config.example.toml をコピーしてください）")
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path} の書式が不正です: {e}")

    token = os.environ.get("DISCORD_TOKEN", "").strip()
    if not token:
        raise ConfigError("環境変数 DISCORD_TOKEN が設定されていません")

    try:
        printer = raw.get("printer", {})
        limits = raw.get("limits", {})
        access = raw.get("access", {})
        paths = raw.get("paths", {})
        config = Config(
            token=token,
            guild_id=int(raw["guild_id"]),
            printer=str(printer["name"]),
            color_options=tuple(printer.get("color_options", ["print-color-mode=color"])),
            mono_options=tuple(printer.get("mono_options", ["print-color-mode=monochrome"])),
            extra_options=tuple(printer.get("extra_options", [])),
            max_file_bytes=int(limits.get("max_file_mb", 10)) * 1024 * 1024,
            max_pages=int(limits.get("max_pages", 30)),
            max_copies=int(limits.get("max_copies", 5)),
            daily_sheets_per_user=int(limits.get("daily_sheets_per_user", 50)),
            confirm_timeout_sec=int(limits.get("confirm_timeout_sec", 60)),
            cooldown_sec=int(limits.get("cooldown_sec", 10)),
            allowed_role_ids=frozenset(int(r) for r in access.get("allowed_role_ids", [])),
            admin_role_ids=frozenset(int(r) for r in access.get("admin_role_ids", [])),
            db_path=Path(paths.get("db", "printbot.db")),
            work_dir=Path(paths.get("work_dir", "tmp")),
        )
    except KeyError as e:
        raise ConfigError(f"必須項目 {e} がありません")
    except (TypeError, ValueError) as e:
        raise ConfigError(f"設定値が不正です: {e}")

    if config.max_copies < 1 or config.max_pages < 1:
        raise ConfigError("max_copies と max_pages は1以上にしてください")
    return config
