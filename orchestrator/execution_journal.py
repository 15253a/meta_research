"""Durable, per-target execution output journal."""

from __future__ import annotations

import codecs
import json
import math
import os
import stat
import struct
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Mapping, Optional, Tuple


DEFAULT_LIMIT = 200
MAX_LIMIT = 1000
_INDEX_ENTRY = struct.Struct(">QQ")
_MAX_APPEND_BYTES = 64 * 1024


@dataclass(frozen=True)
class JournalEvent:
    """One globally ordered stdout/stderr line or unterminated fragment."""

    seq: int
    stream: str
    text: str
    fragment: bool


@dataclass(frozen=True)
class JournalView:
    """A bounded read plus the compact target state observed with it."""

    events: Tuple[JournalEvent, ...]
    cursor: int
    latest_seq: int
    status: Dict[str, object]
    status_revision: int
    terminal: bool
    reason: str


class ExecutionJournal:
    """Thread-safe append-only output owned by one build target.

    One live instance owns a target journal.  Callers append stdout/stderr in
    observation order and share the returned ``seq`` as their only log cursor.
    Raw text lives only in the JSONL event file; memory retains compact offsets
    and decoder/status state.
    """

    def __init__(self, root: Path, *, target_id: int) -> None:
        if isinstance(target_id, bool) or not isinstance(target_id, int) or target_id <= 0:
            raise ValueError("target_id must be a positive integer")
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._path = self._root / f"target-{target_id}.events.jsonl"
        self._index_path = self._root / f"target-{target_id}.events.idx"
        self._state_path = self._root / f"target-{target_id}.state.json"
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._event_identity = None  # type: Optional[Tuple[int, int]]
        self._index_identity = None  # type: Optional[Tuple[int, int]]
        self._event_count = 0
        self._event_file_bytes = 0
        self._index_file_bytes = 0
        state = self._read_decoder_state()
        checkpoint = self._validate_checkpoint_metadata(state)
        self._load_committed_events(checkpoint=checkpoint)
        decoder_type = codecs.getincrementaldecoder("utf-8")
        self._decoders = {
            "stdout": decoder_type(errors="strict"),
            "stderr": decoder_type(errors="strict"),
        }
        self._stream_closed = {"stdout": False, "stderr": False}
        self._stream_bytes = {"stdout": 0, "stderr": 0}
        self._capture_id = None  # type: Optional[str]
        self._capture_base_stream_bytes = {
            "stdout": 0, "stderr": 0}
        self._capture_frame_offset = 0
        self._status = {}  # type: Dict[str, object]
        self._status_revision = 0
        self._terminal = False
        self._load_decoder_state(state)
        self._closed = False
        if state is None:
            if self._event_count:
                raise ValueError(
                    "execution journal events lack an initial atomic state")
            # Establish commit boundary zero before the first event append.
            # A crash after the event fsync but before the next state replace
            # can then be repaired by truncating back to this checkpoint.
            self._persist_decoder_state()

    def _read_decoder_state(self) -> Optional[Dict[str, object]]:
        if not self._state_path.exists():
            return None
        state = json.loads(self._state_path.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            raise ValueError("execution journal contains invalid decoder state")
        return state

    @staticmethod
    def _validate_checkpoint_metadata(
        state: Optional[Mapping[str, object]],
    ) -> Optional[Tuple[int, int, int]]:
        if state is None:
            return None
        fields = (
            "event_file_bytes", "event_count", "index_file_bytes",
            "stream_bytes")
        present = [field in state for field in fields]
        if not any(present):
            return None
        stream_bytes = state.get("stream_bytes")
        if (
            not all(present)
            or isinstance(state.get("event_file_bytes"), bool)
            or not isinstance(state.get("event_file_bytes"), int)
            or int(state["event_file_bytes"]) < 0
            or isinstance(state.get("event_count"), bool)
            or not isinstance(state.get("event_count"), int)
            or int(state["event_count"]) < 0
            or isinstance(state.get("index_file_bytes"), bool)
            or not isinstance(state.get("index_file_bytes"), int)
            or int(state["index_file_bytes"]) < 0
            or int(state["index_file_bytes"])
                != int(state["event_count"]) * _INDEX_ENTRY.size
            or not isinstance(stream_bytes, dict)
            or set(stream_bytes) != {"stdout", "stderr"}
            or any(
                isinstance(stream_bytes.get(stream), bool)
                or not isinstance(stream_bytes.get(stream), int)
                or int(stream_bytes[stream]) < 0
                for stream in ("stdout", "stderr")
            )
        ):
            raise ValueError("execution journal contains invalid capture checkpoint")
        return (
            int(state["event_file_bytes"]),
            int(state["event_count"]),
            int(state["index_file_bytes"]),
        )

    def _load_committed_events(
        self, *, checkpoint: Optional[Tuple[int, int, int]],
    ) -> None:
        committed_bytes, committed_count, committed_index_bytes = (
            (None, None, None) if checkpoint is None else checkpoint)
        event_exists = self._path.exists()
        index_exists = self._index_path.exists()
        if checkpoint is None:
            if event_exists or index_exists:
                raise ValueError(
                    "execution journal files lack an atomic checkpoint")
            return
        assert committed_bytes is not None
        assert committed_count is not None
        assert committed_index_bytes is not None
        if (
            (committed_bytes > 0 and not event_exists)
            or (committed_index_bytes > 0 and not index_exists)
        ):
            raise ValueError(
                "execution journal state exceeds missing event/index file")
        event_fd = index_fd = -1
        try:
            if event_exists:
                event_fd = os.open(
                    self._path,
                    os.O_RDWR
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_NONBLOCK", 0),
                )
                self._event_identity = self._verify_event_file(event_fd)
                size = os.fstat(event_fd).st_size
                if committed_bytes > size:
                    raise ValueError(
                        "execution journal state exceeds event file")
                if size > committed_bytes:
                    os.ftruncate(event_fd, committed_bytes)
                    os.fsync(event_fd)
            if index_exists:
                index_fd = os.open(
                    self._index_path,
                    os.O_RDWR
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_NONBLOCK", 0),
                )
                self._index_identity = self._verify_event_file(index_fd)
                size = os.fstat(index_fd).st_size
                if committed_index_bytes > size:
                    raise ValueError(
                        "execution journal state exceeds index file")
                if size > committed_index_bytes:
                    os.ftruncate(index_fd, committed_index_bytes)
                    os.fsync(index_fd)
            if committed_count:
                if event_fd < 0 or index_fd < 0:
                    raise ValueError(
                        "execution journal checkpoint files are incomplete")
                first = os.pread(index_fd, _INDEX_ENTRY.size, 0)
                last = os.pread(
                    index_fd, _INDEX_ENTRY.size,
                    (committed_count - 1) * _INDEX_ENTRY.size)
                if (
                    len(first) != _INDEX_ENTRY.size
                    or len(last) != _INDEX_ENTRY.size
                ):
                    raise ValueError(
                        "execution journal index checkpoint is torn")
                first_offset, first_length = _INDEX_ENTRY.unpack(first)
                last_offset, last_length = _INDEX_ENTRY.unpack(last)
                if (
                    first_offset != 0
                    or first_length <= 0
                    or last_length <= 0
                    or last_offset + last_length != committed_bytes
                ):
                    raise ValueError(
                        "execution journal index boundary is invalid")
            elif committed_bytes != 0:
                raise ValueError(
                    "execution journal empty index has event bytes")
            self._event_count = committed_count
            self._event_file_bytes = committed_bytes
            self._index_file_bytes = committed_index_bytes
        finally:
            if event_fd >= 0:
                os.close(event_fd)
            if index_fd >= 0:
                os.close(index_fd)

    def _read_events(self, start_index: int, limit: int) -> Tuple[JournalEvent, ...]:
        selected_count = min(limit, max(0, self._event_count - start_index))
        if selected_count <= 0:
            return ()
        event_fd = os.open(
            self._path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
        )
        index_fd = os.open(
            self._index_path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
        )
        try:
            identity = self._verify_event_file(event_fd)
            index_identity = self._verify_event_file(index_fd)
            if (
                identity != self._event_identity
                or index_identity != self._index_identity
            ):
                raise OSError("execution journal file identity changed")
            raw_index = os.pread(
                index_fd, selected_count * _INDEX_ENTRY.size,
                start_index * _INDEX_ENTRY.size)
            if len(raw_index) != selected_count * _INDEX_ENTRY.size:
                raise OSError("execution journal index changed during read")
            events = []
            prior_end = None
            for relative in range(selected_count):
                index = start_index + relative + 1
                offset, length = _INDEX_ENTRY.unpack_from(
                    raw_index, relative * _INDEX_ENTRY.size)
                if (
                    length <= 0
                    or offset + length > self._event_file_bytes
                    or (prior_end is not None and offset != prior_end)
                ):
                    raise OSError(
                        "execution journal index entry is invalid")
                raw_line = os.pread(event_fd, length, offset)
                if len(raw_line) != length or not raw_line.endswith(b"\n"):
                    raise OSError("execution journal changed during read")
                events.append(self._decode_event(raw_line[:-1], index))
                prior_end = offset + length
            return tuple(events)
        finally:
            os.close(event_fd)
            os.close(index_fd)

    @staticmethod
    def _decode_event(raw_record: bytes, expected_seq: int) -> JournalEvent:
        record = json.loads(raw_record.decode("utf-8"))
        seq = record.get("seq") if isinstance(record, dict) else None
        if (
            not isinstance(record, dict)
            or isinstance(seq, bool)
            or seq != expected_seq
            or record.get("stream") not in {"stdout", "stderr"}
            or not isinstance(record.get("text"), str)
            or not isinstance(record.get("fragment"), bool)
        ):
            raise ValueError("execution journal contains an invalid event")
        return JournalEvent(
            seq=expected_seq,
            stream=record["stream"],
            text=record["text"],
            fragment=record["fragment"],
        )

    @staticmethod
    def _verify_event_file(fd: int) -> Tuple[int, int]:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.geteuid()
        ):
            raise OSError(
                "execution journal must be an owner-controlled regular file"
            )
        return (info.st_dev, info.st_ino)

    def _load_decoder_state(
        self, state: Optional[Mapping[str, object]],
    ) -> None:
        if state is None:
            return
        pending = state.get("pending") if isinstance(state, dict) else None
        closed = state.get("closed", {"stdout": False, "stderr": False})
        status = state.get("status", {})
        status_revision = state.get("status_revision", 0)
        terminal = state.get("terminal", False)
        capture = state.get("capture")
        if (
            not isinstance(pending, dict)
            or set(pending) != {"stdout", "stderr"}
            or not all(isinstance(pending.get(stream), str) for stream in pending)
            or not isinstance(closed, dict)
            or set(closed) != {"stdout", "stderr"}
            or not all(isinstance(closed.get(stream), bool) for stream in closed)
            or not isinstance(status, dict)
            or isinstance(status_revision, bool)
            or not isinstance(status_revision, int)
            or status_revision < 0
            or not isinstance(terminal, bool)
            or (
                capture is not None
                and (
                    not isinstance(capture, dict)
                    or set(capture) != {
                        "capture_id", "base_stream_bytes", "frame_offset"}
                    or not self._valid_capture_id(
                        capture.get("capture_id"))
                    or not isinstance(
                        capture.get("base_stream_bytes"), dict)
                    or set(capture["base_stream_bytes"]) != {
                        "stdout", "stderr"}
                    or any(
                        isinstance(
                            capture["base_stream_bytes"].get(stream), bool)
                        or not isinstance(
                            capture["base_stream_bytes"].get(stream), int)
                        or int(
                            capture["base_stream_bytes"][stream]) < 0
                        for stream in ("stdout", "stderr")
                    )
                    or isinstance(capture.get("frame_offset"), bool)
                    or not isinstance(capture.get("frame_offset"), int)
                    or int(capture["frame_offset"]) < 0
                )
            )
        ):
            raise ValueError("execution journal contains invalid decoder state")
        try:
            for stream in ("stdout", "stderr"):
                self._decoders[stream].setstate(
                    (bytes.fromhex(pending[stream]), 0)
                )
                self._stream_closed[stream] = closed[stream]
            self._status = self._normalize_status(status)
            self._status_revision = status_revision
            self._terminal = terminal
            checkpoint = self._validate_checkpoint_metadata(state)
            if checkpoint is None:
                # Older state did not durably bind raw stream offsets to the
                # event file.  An empty event file is recoverable because only
                # the decoder's pending bytes have been consumed; otherwise
                # guessing an offset could duplicate or skip guardian bytes.
                if self._event_count:
                    raise ValueError(
                        "legacy execution journal with events lacks "
                        "capture checkpoint")
                self._stream_bytes = {
                    stream: len(self._decoders[stream].getstate()[0])
                    for stream in ("stdout", "stderr")
                }
            else:
                if (
                    int(state["event_count"]) != self._event_count
                    or int(state["event_file_bytes"])
                        != self._event_file_bytes
                    or int(state["index_file_bytes"])
                        != self._index_file_bytes
                ):
                    raise ValueError(
                        "execution journal capture checkpoint conflicts "
                        "with events")
                stream_bytes = state["stream_bytes"]
                assert isinstance(stream_bytes, dict)
                self._stream_bytes = {
                    stream: int(stream_bytes[stream])
                    for stream in ("stdout", "stderr")
                }
            if capture is not None:
                base = capture["base_stream_bytes"]
                assert isinstance(base, dict)
                if any(
                    int(base[stream]) > self._stream_bytes[stream]
                    for stream in ("stdout", "stderr")
                ):
                    raise ValueError(
                        "execution journal capture base exceeds stream bytes")
                self._capture_id = str(capture["capture_id"])
                self._capture_base_stream_bytes = {
                    stream: int(base[stream])
                    for stream in ("stdout", "stderr")
                }
                self._capture_frame_offset = int(
                    capture["frame_offset"])
        except ValueError as exc:
            raise ValueError(
                "execution journal contains invalid decoder state"
            ) from exc

    def _persist_decoder_state(self) -> None:
        pending = {}
        for stream in ("stdout", "stderr"):
            buffered, flag = self._decoders[stream].getstate()
            if flag != 0:
                raise RuntimeError("unexpected UTF-8 decoder state")
            pending[stream] = buffered.hex()
        payload = (
            json.dumps(
                {
                    "closed": self._stream_closed,
                    "capture": (
                        None if self._capture_id is None else {
                            "capture_id": self._capture_id,
                            "base_stream_bytes":
                                self._capture_base_stream_bytes,
                            "frame_offset":
                                self._capture_frame_offset,
                        }),
                    "event_count": self._event_count,
                    "event_file_bytes": self._event_file_bytes,
                    "index_file_bytes": self._index_file_bytes,
                    "pending": pending,
                    "status": self._status,
                    "status_revision": self._status_revision,
                    "stream_bytes": self._stream_bytes,
                    "terminal": self._terminal,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{self._state_path.name}.",
            dir=str(self._root),
        )
        try:
            self._write_all(fd, payload)
            os.fsync(fd)
            os.close(fd)
            fd = -1
            os.replace(temp_name, self._state_path)
            self._fsync_root()
        finally:
            if fd >= 0:
                os.close(fd)
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass

    @staticmethod
    def _write_all(fd: int, payload: bytes) -> None:
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("execution journal write made no progress")
            view = view[written:]

    def _fsync_root(self) -> None:
        directory_fd = os.open(
            self._root,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            if not stat.S_ISDIR(os.fstat(directory_fd).st_mode):
                raise OSError("execution journal root is not a directory")
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def __enter__(self) -> "ExecutionJournal":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            self._closed = True

    @property
    def event_path(self) -> Path:
        """Return the durable log reference; callers must treat it as read-only."""

        return self._path

    @property
    def stream_offsets(self) -> Dict[str, int]:
        """Return receipt-capture bytes durably committed per stream."""

        with self._lock:
            return dict(self._stream_bytes)

    def open_capture(self, capture_id: str) -> Dict[str, object]:
        """Open or recover one exact execution capture for this target.

        Guardian capture files restart their offsets at zero for every
        smoke/train/eval operation, while this journal spans the target's
        complete lifetime.  The persisted base translates those two scopes
        without replaying or guessing from prior operations.
        """
        if not self._valid_capture_id(capture_id):
            raise ValueError("capture_id must be a bounded non-empty string")
        with self._lock:
            if self._closed:
                raise RuntimeError("journal is closed")
            if self._capture_id != capture_id:
                if any(
                    self._decoders[stream].getstate()[0]
                    for stream in ("stdout", "stderr")
                ):
                    raise ValueError(
                        "prior execution capture ended with incomplete UTF-8")
                decoder_type = codecs.getincrementaldecoder("utf-8")
                self._decoders = {
                    "stdout": decoder_type(errors="strict"),
                    "stderr": decoder_type(errors="strict"),
                }
                self._stream_closed = {
                    "stdout": False, "stderr": False}
                self._capture_id = capture_id
                self._capture_base_stream_bytes = dict(
                    self._stream_bytes)
                self._capture_frame_offset = 0
                self._persist_decoder_state()
            relative = {
                stream: (
                    self._stream_bytes[stream]
                    - self._capture_base_stream_bytes[stream])
                for stream in ("stdout", "stderr")
            }
            if any(value < 0 for value in relative.values()):
                raise RuntimeError(
                    "execution journal capture offsets regressed")
            return {
                "capture_id": self._capture_id,
                "frame_offset": self._capture_frame_offset,
                "stream_offsets": relative,
            }

    def append_capture(
        self,
        capture_id: str,
        stream: str,
        data: bytes,
        frame_end_offset: int,
    ) -> Tuple[JournalEvent, ...]:
        """Atomically commit one ordered guardian frame and its log events."""
        if not self._valid_capture_id(capture_id):
            raise ValueError("capture_id must be a bounded non-empty string")
        if (
            isinstance(frame_end_offset, bool)
            or not isinstance(frame_end_offset, int)
            or frame_end_offset <= 0
        ):
            raise ValueError("frame_end_offset must be a positive integer")
        return self._append(
            stream, data, final=False,
            capture_id=capture_id,
            frame_end_offset=frame_end_offset)

    def append(
        self,
        stream: str,
        data: bytes,
        *,
        final: bool = False,
    ) -> Tuple[JournalEvent, ...]:
        """Append one observed byte chunk and return its committed text events.

        UTF-8 code points may span calls.  Newline-terminated pieces become
        complete events and a trailing piece becomes a fragment.  ``final``
        validates and permanently closes that stream.
        """

        return self._append(stream, data, final=final)

    def _append(
        self,
        stream: str,
        data: bytes,
        *,
        final: bool,
        capture_id: Optional[str] = None,
        frame_end_offset: Optional[int] = None,
    ) -> Tuple[JournalEvent, ...]:
        if stream not in {"stdout", "stderr"}:
            raise ValueError("stream must be 'stdout' or 'stderr'")
        if not isinstance(data, bytes):
            raise TypeError("data must be bytes")
        if len(data) > _MAX_APPEND_BYTES:
            raise ValueError(
                "journal append chunk exceeds 65536 bytes")
        if not isinstance(final, bool):
            raise TypeError("final must be a boolean")
        with self._lock:
            if self._closed:
                raise RuntimeError("journal is closed")
            if self._stream_closed[stream]:
                raise RuntimeError(f"{stream} stream is already finalized")
            if capture_id is not None:
                if self._capture_id != capture_id:
                    raise RuntimeError(
                        "guardian frame belongs to a different capture")
                assert frame_end_offset is not None
                if frame_end_offset <= self._capture_frame_offset:
                    raise ValueError(
                        "guardian frame offset must advance monotonically")
            prior_state = self._decoders[stream].getstate()
            try:
                text = self._decoders[stream].decode(data, final=final)
            except UnicodeDecodeError:
                self._decoders[stream].setstate(prior_state)
                raise
            if final:
                self._stream_closed[stream] = True
            if not text:
                self._stream_bytes[stream] += len(data)
                if capture_id is not None:
                    self._capture_frame_offset = frame_end_offset
                self._persist_decoder_state()
                return ()
            events = tuple(
                JournalEvent(
                    seq=self._event_count + offset,
                    stream=stream,
                    text=fragment,
                    fragment=not fragment.endswith("\n"),
                )
                for offset, fragment in enumerate(
                    self._split_text(text),
                    start=1,
                )
            )
            records = tuple(
                json.dumps(
                    {
                        "fragment": event.fragment,
                        "seq": event.seq,
                        "stream": event.stream,
                        "text": event.text,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                + b"\n"
                for event in events
            )
            payload = b"".join(records)
            event_file_was_missing = self._event_identity is None
            index_file_was_missing = self._index_identity is None
            fd = os.open(
                self._path,
                os.O_APPEND
                | os.O_CREAT
                | os.O_WRONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0),
                0o600,
            )
            try:
                identity = self._verify_event_file(fd)
                if (
                    self._event_identity is not None
                    and identity != self._event_identity
                ):
                    raise OSError("execution journal file identity changed")
                offset = os.lseek(fd, 0, os.SEEK_END)
                expected_offset = self._event_file_bytes
                if offset != expected_offset:
                    raise OSError(
                        "execution journal changed outside its owner"
                    )
                self._write_all(fd, payload)
                os.fsync(fd)
            finally:
                os.close(fd)
            index_records = []
            next_offset = offset
            for record in records:
                index_records.append(
                    _INDEX_ENTRY.pack(next_offset, len(record)))
                next_offset += len(record)
            index_payload = b"".join(index_records)
            index_fd = os.open(
                self._index_path,
                os.O_APPEND
                | os.O_CREAT
                | os.O_WRONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0),
                0o600,
            )
            try:
                index_identity = self._verify_event_file(index_fd)
                if (
                    self._index_identity is not None
                    and index_identity != self._index_identity
                ):
                    raise OSError(
                        "execution journal index identity changed")
                index_offset = os.lseek(index_fd, 0, os.SEEK_END)
                if index_offset != self._index_file_bytes:
                    raise OSError(
                        "execution journal index changed outside its owner")
                self._write_all(index_fd, index_payload)
                os.fsync(index_fd)
            finally:
                os.close(index_fd)
            self._event_identity = identity
            self._index_identity = index_identity
            self._event_count += len(records)
            self._event_file_bytes += len(payload)
            self._index_file_bytes += len(index_payload)
            if event_file_was_missing or index_file_was_missing:
                self._fsync_root()
            self._stream_bytes[stream] += len(data)
            if capture_id is not None:
                self._capture_frame_offset = frame_end_offset
            self._persist_decoder_state()
            self._condition.notify_all()
            return events

    @staticmethod
    def _valid_capture_id(value: object) -> bool:
        return bool(
            isinstance(value, str)
            and "\x00" not in value
            and 0 < len(value.encode("utf-8")) <= 256
        )

    def publish_status(
        self,
        status: Mapping[str, object],
        *,
        terminal: bool = False,
    ) -> int:
        """Publish bounded JSON status and return its monotonic revision."""

        if not isinstance(terminal, bool):
            raise TypeError("terminal must be a boolean")
        normalized = self._normalize_status(status)
        with self._lock:
            if self._closed:
                raise RuntimeError("journal is closed")
            next_terminal = self._terminal or terminal
            if normalized == self._status and next_terminal == self._terminal:
                return self._status_revision
            self._status = normalized
            self._terminal = next_terminal
            self._status_revision += 1
            self._persist_decoder_state()
            self._condition.notify_all()
            return self._status_revision

    def snapshot(self, limit: int = DEFAULT_LIMIT) -> JournalView:
        """Return the most recent bounded events in ascending sequence order."""

        self._validate_limit(limit)
        with self._lock:
            latest_seq = self._event_count
            start_index = max(0, latest_seq - limit)
            selected = self._read_events(start_index, limit)
            cursor = selected[-1].seq if selected else 0
            return JournalView(
                events=selected,
                cursor=cursor,
                latest_seq=latest_seq,
                status=self._copy_status(),
                status_revision=self._status_revision,
                terminal=self._terminal,
                reason="snapshot",
            )

    def incremental(
        self,
        *,
        after_seq: int,
        limit: int = DEFAULT_LIMIT,
    ) -> JournalView:
        """Return events strictly newer than ``after_seq`` without replay."""

        self._validate_limit(limit)
        self._validate_cursor("after_seq", after_seq)
        with self._lock:
            self._validate_cursor_is_current(after_seq)
            selected = self._read_events(after_seq, limit)
            latest_seq = self._event_count
            cursor = selected[-1].seq if selected else after_seq
            return JournalView(
                events=selected,
                cursor=cursor,
                latest_seq=latest_seq,
                status=self._copy_status(),
                status_revision=self._status_revision,
                terminal=self._terminal,
                reason="incremental",
            )

    def wait(
        self,
        *,
        after_seq: int,
        after_status_revision: int,
        timeout_s: float,
        limit: int = DEFAULT_LIMIT,
    ) -> JournalView:
        """Wait until events, status, terminal state, or the deadline is seen."""

        self._validate_limit(limit)
        self._validate_cursor("after_seq", after_seq)
        self._validate_cursor("after_status_revision", after_status_revision)
        if (
            isinstance(timeout_s, bool)
            or not isinstance(timeout_s, (int, float))
            or not math.isfinite(timeout_s)
            or timeout_s < 0
            or timeout_s > 1800
        ):
            raise ValueError("timeout_s must be between 0 and 1800 seconds")
        deadline = time.monotonic() + timeout_s
        with self._condition:
            self._validate_cursor_is_current(after_seq)
            while True:
                selected = self._read_events(after_seq, limit)
                if selected:
                    reason = "events"
                elif self._terminal:
                    reason = "terminal"
                elif self._status_revision > after_status_revision:
                    reason = "status"
                else:
                    remaining = deadline - time.monotonic()
                    if remaining > 0:
                        self._condition.wait(timeout=remaining)
                        continue
                    reason = "timeout"
                cursor = selected[-1].seq if selected else after_seq
                return JournalView(
                    events=selected,
                    cursor=cursor,
                    latest_seq=self._event_count,
                    status=self._copy_status(),
                    status_revision=self._status_revision,
                    terminal=self._terminal,
                    reason=reason,
                )

    @staticmethod
    def _validate_limit(limit: int) -> None:
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or limit < 1
            or limit > MAX_LIMIT
        ):
            raise ValueError("limit must be between 1 and 1000")

    @staticmethod
    def _validate_cursor(name: str, value: int) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{name} must be a non-negative integer")

    def _validate_cursor_is_current(self, after_seq: int) -> None:
        if after_seq > self._event_count:
            raise ValueError(
                "after_seq is ahead of the journal; use snapshot to recover"
            )

    @staticmethod
    def _normalize_status(status: Mapping[str, object]) -> Dict[str, object]:
        if not isinstance(status, Mapping):
            raise TypeError("status must be a mapping")
        try:
            payload = json.dumps(
                dict(status),
                allow_nan=False,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValueError("status must contain only JSON values") from exc
        if len(payload) > 16_384:
            raise ValueError("status must be no larger than 16384 bytes")
        normalized = json.loads(payload.decode("utf-8"))
        if not isinstance(normalized, dict):
            raise TypeError("status must be a mapping")
        return normalized

    def _copy_status(self) -> Dict[str, object]:
        return json.loads(json.dumps(self._status))

    @staticmethod
    def _split_text(text: str) -> Tuple[str, ...]:
        fragments = []
        start = 0
        while True:
            newline = text.find("\n", start)
            if newline < 0:
                break
            fragments.append(text[start : newline + 1])
            start = newline + 1
        if start < len(text):
            fragments.append(text[start:])
        return tuple(fragments)
