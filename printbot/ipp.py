"""Minimal IPP client for querying job and printer state from the local cupsd.

`lpstat` cannot tell a completed job from an aborted one, so job/printer state
is read over IPP instead. Only the few operations the bot needs are implemented.
"""
from __future__ import annotations

import asyncio
import getpass
import http.client
import itertools
import os
import socket
import struct
from dataclasses import dataclass, field
from pathlib import Path

OP_GET_JOB_ATTRIBUTES = 0x0009
OP_GET_PRINTER_ATTRIBUTES = 0x000B

STATUS_NOT_FOUND = 0x0406

TAG_OPERATION = 0x01
TAG_END = 0x03
TAG_INTEGER = 0x21
TAG_BOOLEAN = 0x22
TAG_ENUM = 0x23
TAG_URI = 0x45
TAG_NAME = 0x42
TAG_KEYWORD = 0x44
TAG_CHARSET = 0x47
TAG_LANGUAGE = 0x48

DEFAULT_SOCKETS = ("/run/cups/cups.sock", "/var/run/cups/cups.sock")

_request_ids = itertools.count(1)


class IppError(Exception):
    pass


class IppNotFound(IppError):
    pass


# --- job / printer state ----------------------------------------------------

JOB_PENDING, JOB_HELD, JOB_PROCESSING, JOB_STOPPED = 3, 4, 5, 6
JOB_CANCELED, JOB_ABORTED, JOB_COMPLETED = 7, 8, 9
JOB_FINAL_STATES = {JOB_CANCELED, JOB_ABORTED, JOB_COMPLETED}

JOB_STATE_LABELS = {
    JOB_PENDING: "🕒 印刷待ち",
    JOB_HELD: "⏸️ 保留中",
    JOB_PROCESSING: "📨 データ送信中・印刷中",
    JOB_STOPPED: "⛔ 停止中（プリンターを確認してください）",
    JOB_CANCELED: "🗑️ 取り消し",
    JOB_ABORTED: "❌ 印刷失敗",
    JOB_COMPLETED: "✅ 印刷完了",
}

PRINTER_STATE_LABELS = {3: "🟢 待機中", 4: "🖨️ 印刷中", 5: "🔴 停止中"}

# printer-state-reasons keywords (without -error/-warning/-report suffix)
REASON_LABELS = {
    "media-empty": "用紙切れ",
    "media-needed": "用紙を補給してください",
    "media-jam": "紙詰まり",
    "marker-supply-empty": "インク切れ",
    "marker-supply-low": "インク残量わずか",
    "marker-waste-full": "廃インク吸収体が満杯",
    "door-open": "カバーが開いています",
    "cover-open": "カバーが開いています",
    "offline": "オフライン",
    "connecting-to-device": "プリンターに接続中",
    "paused": "一時停止中",
    "shutdown": "電源オフ",
    "timed-out": "通信タイムアウト",
    "toner-empty": "インク切れ",
    "toner-low": "インク残量わずか",
    "other": "その他のエラー",
}


@dataclass(frozen=True)
class JobStatus:
    state: int
    reasons: tuple[str, ...] = ()
    message: str = ""
    # pages CUPS has finished so far (job-impressions-completed), counting every copy
    impressions: int = 0

    @property
    def final(self) -> bool:
        return self.state in JOB_FINAL_STATES

    @property
    def succeeded(self) -> bool:
        return self.state == JOB_COMPLETED and not any(
            r.startswith(("job-completed-with-errors", "job-completed-with-warnings"))
            for r in self.reasons
        )

    @property
    def label(self) -> str:
        if self.state == JOB_COMPLETED and not self.succeeded:
            return "⚠️ 印刷完了（エラーあり）"
        return JOB_STATE_LABELS.get(self.state, f"不明な状態 ({self.state})")


@dataclass(frozen=True)
class Marker:
    name: str
    level: int  # 0-100, negative = unknown


@dataclass(frozen=True)
class PrinterStatus:
    state: int
    reasons: tuple[str, ...] = ()
    message: str = ""
    accepting: bool = True
    markers: tuple[Marker, ...] = field(default_factory=tuple)

    @property
    def label(self) -> str:
        return PRINTER_STATE_LABELS.get(self.state, f"不明 ({self.state})")

    @property
    def usable(self) -> bool:
        return self.state != 5 and self.accepting

    def problems(self) -> list[str]:
        return describe_reasons(self.reasons)


def describe_reasons(reasons: tuple[str, ...]) -> list[str]:
    out = []
    for r in reasons:
        if r == "none":
            continue
        base = r
        for suffix in ("-error", "-warning", "-report"):
            base = base.removesuffix(suffix)
        label = REASON_LABELS.get(base)
        if label and label not in out:
            out.append(label)
    return out


# --- wire format ------------------------------------------------------------

def _attr(tag: int, name: str, value: bytes) -> bytes:
    n = name.encode()
    return struct.pack(">BH", tag, len(n)) + n + struct.pack(">H", len(value)) + value


def encode_request(op: int, request_id: int, attrs: list[tuple[int, str, object]]) -> bytes:
    out = struct.pack(">BBHI", 2, 0, op, request_id) + bytes([TAG_OPERATION])
    for tag, name, value in attrs:
        values = value if isinstance(value, (list, tuple)) else [value]
        for i, v in enumerate(values):
            raw = struct.pack(">i", v) if isinstance(v, int) else str(v).encode()
            out += _attr(tag, name if i == 0 else "", raw)
    return out + bytes([TAG_END])


def decode_response(data: bytes) -> tuple[int, dict[str, list]]:
    """Return (status-code, attributes). Attributes from all groups are merged."""
    if len(data) < 8:
        raise IppError("IPPレスポンスが短すぎます")
    status = struct.unpack(">H", data[2:4])[0]
    attrs: dict[str, list] = {}
    pos, last = 8, None
    while pos < len(data):
        tag = data[pos]
        pos += 1
        if tag == TAG_END:
            break
        if tag < 0x10:  # begin-attribute-group
            continue
        name_len = struct.unpack(">H", data[pos:pos + 2])[0]
        pos += 2
        name = data[pos:pos + name_len].decode(errors="replace")
        pos += name_len
        value_len = struct.unpack(">H", data[pos:pos + 2])[0]
        pos += 2
        raw = data[pos:pos + value_len]
        pos += value_len
        if tag in (TAG_INTEGER, TAG_ENUM) and value_len == 4:
            value: object = struct.unpack(">i", raw)[0]
        elif tag == TAG_BOOLEAN and value_len == 1:
            value = raw != b"\x00"
        elif 0x41 <= tag <= 0x49:
            value = raw.decode(errors="replace")
        else:
            value = raw
        if name:
            last = name
            attrs.setdefault(name, []).append(value)
        elif last:
            attrs[last].append(value)
    return status, attrs


# --- transport --------------------------------------------------------------

class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._path)


class IppClient:
    def __init__(self, printer: str, server: str | None = None, timeout: float = 10):
        self.printer = printer
        self.server = server or os.environ.get("CUPS_SERVER") or next(
            (p for p in DEFAULT_SOCKETS if Path(p).exists()), "localhost:631"
        )
        self.timeout = timeout
        self.user = getpass.getuser()

    @property
    def printer_uri(self) -> str:
        return f"ipp://localhost/printers/{self.printer}"

    def _connect(self) -> http.client.HTTPConnection:
        if self.server.startswith("/"):
            return _UnixHTTPConnection(self.server, self.timeout)
        host, _, port = self.server.partition(":")
        return http.client.HTTPConnection(host, int(port or 631), timeout=self.timeout)

    def _call(self, op: int, path: str, attrs: list[tuple[int, str, object]]) -> dict[str, list]:
        body = encode_request(op, next(_request_ids), [
            (TAG_CHARSET, "attributes-charset", "utf-8"),
            (TAG_LANGUAGE, "attributes-natural-language", "en"),
            *attrs,
        ])
        conn = self._connect()
        try:
            conn.request("POST", path, body, {"Content-Type": "application/ipp"})
            resp = conn.getresponse()
            data = resp.read()
        except OSError as e:
            raise IppError(f"CUPSに接続できません: {e}") from e
        finally:
            conn.close()
        if resp.status != 200:
            raise IppError(f"CUPSがHTTP {resp.status}を返しました")
        status, result = decode_response(data)
        if status == STATUS_NOT_FOUND:
            raise IppNotFound("not found")
        if status >= 0x0100:
            raise IppError(f"IPPエラー 0x{status:04x}")
        return result

    def _job(self, job_id: int) -> JobStatus:
        a = self._call(OP_GET_JOB_ATTRIBUTES, "/jobs", [
            (TAG_URI, "printer-uri", self.printer_uri),
            (TAG_INTEGER, "job-id", job_id),
            (TAG_NAME, "requesting-user-name", self.user),
            (TAG_KEYWORD, "requested-attributes",
             ["job-state", "job-state-reasons", "job-printer-state-message",
              "job-impressions-completed"]),
        ])
        return JobStatus(
            state=a.get("job-state", [0])[0],
            reasons=tuple(a.get("job-state-reasons", [])),
            message=a.get("job-printer-state-message", [""])[0],
            impressions=a.get("job-impressions-completed", [0])[0],
        )

    def _printer(self) -> PrinterStatus:
        a = self._call(OP_GET_PRINTER_ATTRIBUTES, f"/printers/{self.printer}", [
            (TAG_URI, "printer-uri", self.printer_uri),
            (TAG_NAME, "requesting-user-name", self.user),
            (TAG_KEYWORD, "requested-attributes", [
                "printer-state", "printer-state-reasons", "printer-state-message",
                "printer-is-accepting-jobs", "marker-names", "marker-levels",
            ]),
        ])
        names, levels = a.get("marker-names", []), a.get("marker-levels", [])
        return PrinterStatus(
            state=a.get("printer-state", [0])[0],
            reasons=tuple(a.get("printer-state-reasons", [])),
            message=a.get("printer-state-message", [""])[0],
            accepting=a.get("printer-is-accepting-jobs", [True])[0],
            markers=tuple(Marker(n, lv) for n, lv in zip(names, levels)),
        )

    async def job_status(self, job_id: int) -> JobStatus:
        return await asyncio.to_thread(self._job, job_id)

    async def printer_status(self) -> PrinterStatus:
        return await asyncio.to_thread(self._printer)
