"""Explicit, bounded DAC recording with a lossless timestamped sidecar."""
import base64
from contextlib import AbstractContextManager
import os
from pathlib import Path
import struct
import threading
import time

from .errors import (DeliveryUnknown, HistoryLost, OperationTimeout, ProtocolError,
                     RunEnded, preserve_exception)


class _Wave:
    def __init__(self, stream):
        self.stream = stream
        self.rate = self.samples = self.origin = 0
        self.last_ns = None
        self.header()

    def header(self):
        rate = self.rate or 16000
        size = self.samples * 2
        self.stream.seek(0)
        self.stream.write(struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", size + 36,
            b"WAVE", b"fmt ", 16, 1, 1, rate, rate * 2, 2, 16, b"data", size))
        self.stream.seek(0, 2)
        self.stream.flush()

    def silence_to(self, target):
        if target > (2**32 - 37) // 2:
            raise HistoryLost("WAV size limit reached")
        while self.samples < target:
            count = min(4096, target - self.samples)
            self.stream.write(b"\0" * (count * 2))
            self.samples += count

    def append(self, raw):
        for ns, rate, sample, _pa, _reserved in struct.iter_unpack("<QIhBB", raw):
            if rate not in (8000, 16000, 24000, 32000, 48000, 96000):
                raise ProtocolError("Invalid DAC sample rate")
            if self.rate and rate != self.rate:
                raise HistoryLost("DAC sample rate changed during WAV recording")
            if ns < self.origin or self.last_ns is not None and ns <= self.last_ns:
                raise HistoryLost("Audio timestamps are not strictly increasing")
            self.rate = rate
            target = max(0, ((ns - self.origin) * rate + 500_000_000) // 1_000_000_000 - 1)
            self.silence_to(target)
            if self.samples >= (2**32 - 37) // 2:
                raise HistoryLost("WAV size limit reached")
            self.stream.write(struct.pack("<h", sample))
            self.samples += 1
            self.last_ns = ns
        self.header()


class AudioRecording(AbstractContextManager):
    """Drain real DAC frames until stop(), keeping WAV, raw frames and metadata.

    The host deadline fails the recording after confirming its own stop. It
    never stops the device. Frames are little-endian ns:u64, rate:u32,
    sample:i16, PA:u8, reserved:u8; the raw stream is preserved byte for byte.
    """
    def __init__(self, run, path, *, max_seconds):
        if not isinstance(max_seconds, (int, float)) or not 0 < max_seconds <= 600:
            raise ValueError("max_seconds must be >0 and <=600 host seconds")
        self.run = run
        self.path = Path(path).resolve()
        if self.path.is_relative_to(run.instance.client.data_dir / "devices"):
            raise ValueError("Recording must be outside the instance storage tree")
        self.raw_path = self.path.with_suffix(self.path.suffix + ".frames")
        self.metadata_path = self.path.with_suffix(self.path.suffix + ".json")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        created, streams = [], []
        try:
            for target in (self.path, self.raw_path, self.metadata_path):
                stream = target.open("xb")
                created.append(target)
                streams.append(stream)
                os.chmod(target, 0o600)
        except Exception:
            for stream in streams:
                stream.close()
            for target in created:
                target.unlink()
            raise
        try:
            self._wave = _Wave(streams[0])
        except Exception:
            for stream in streams:
                stream.close()
            for target in created:
                target.unlink()
            raise
        self._raw = streams[1]
        streams[2].close()
        self._stop = threading.Event()
        self._done = threading.Event()
        self._error = None
        self._error_reported = self._released = False
        self._thread = None
        self.sequence = None
        self.frames = 0
        self.deadline = time.monotonic() + max_seconds
        self.metadata = {"status": "starting", "run": run.id, "device_id": run.instance.id,
                         "path": str(self.path), "raw_path": str(self.raw_path),
                         "frame_format": "little-endian ns:u64,rate:u32,sample:i16,pa:u8,reserved:u8",
                         "max_seconds": max_seconds}
        try:
            packet = self._call("start")
            self.sequence = packet.get("sequence")
            origin = packet.get("started_ns")
            if not isinstance(self.sequence, int) or self.sequence <= 0 or not isinstance(origin, int):
                raise ProtocolError("Missing recording sequence or start timestamp")
            self._wave.origin = origin
            self.metadata.update(sequence=self.sequence, started_ns=origin, status="recording")
            self._save()
            self._thread = threading.Thread(target=self._pump, args=(packet,), daemon=True,
                                            name="lisem-audio-recording")
            self._thread.start()
        except Exception as exc:
            self._error = exc
            if self.sequence is not None:
                self._discard()
            self._finish()
            raise

    def __enter__(self):
        return self

    def __exit__(self, typ, exc, tb):
        try:
            self.stop()
        except Exception as cleanup:
            preserve_exception(exc, cleanup)

    def _call(self, action):
        params = {"action": action}
        if self.sequence is not None:
            params["sequence"] = self.sequence
        # Reading consumes frames. Lost replies must fail rather than replay.
        # Payloads are streamed to explicit files, not retained in operation RAM.
        return self.run._call("audio_record", params, timeout=2, _record=False)

    def _accept(self, packet):
        if packet.get("sequence") != self.sequence or packet.get("offset") != self.frames:
            raise HistoryLost("Audio recording sequence or frame cursor changed")
        if packet.get("started_ns") != self._wave.origin:
            raise HistoryLost("Audio recording origin changed")
        try:
            raw = base64.b64decode(packet["data"], validate=True)
        except (KeyError, ValueError, TypeError) as exc:
            raise ProtocolError("Invalid audio recording payload") from exc
        if len(raw) % 16 or len(raw) > 65536:
            raise ProtocolError("Invalid audio recording frame packet")
        if self.frames * 16 + len(raw) > 1024**3:
            raise HistoryLost("Raw audio recording reached the 1 GiB limit")
        self._raw.write(raw)
        self._raw.flush()
        self.frames += len(raw) // 16
        self._wave.append(raw)
        if packet.get("error"):
            raise HistoryLost(f"Audio recording: {packet['error']}")
        if not isinstance(packet.get("remaining"), int) or packet["remaining"] < 0:
            raise ProtocolError("Invalid audio recording remaining byte count")

    def _pump(self, packet):
        stopping = expired = False
        drain_deadline = None
        try:
            while True:
                self._accept(packet)
                if stopping and packet["remaining"] == 0:
                    if packet.get("active") is not False:
                        raise DeliveryUnknown("Audio recording stop was not confirmed")
                    end = packet.get("virtual_ns")
                    if not isinstance(end, int) or end < self._wave.origin:
                        raise ProtocolError("Invalid audio recording end timestamp")
                    self._wave.silence_to((end - self._wave.origin) * (self._wave.rate or 16000) // 1_000_000_000)
                    self._wave.header()
                    self.metadata["ended_ns"] = end
                    self._released = True
                    if expired:
                        raise OperationTimeout("Audio recording deadline expired; recording stopped")
                    self.metadata["status"] = "completed"
                    break
                if not stopping and packet.get("active") is not True:
                    raise RunEnded("Audio recording ended unexpectedly")
                if not stopping and (self._stop.is_set() or time.monotonic() >= self.deadline):
                    expired = not self._stop.is_set()
                    stopping = True
                    drain_deadline = time.monotonic() + 8
                    packet = self._call("stop")
                else:
                    if stopping and time.monotonic() >= drain_deadline:
                        raise DeliveryUnknown("Stopped audio recording could not be drained in time")
                    if not stopping and packet["remaining"] == 0:
                        self._stop.wait(min(.02, max(0, self.deadline - time.monotonic())))
                    packet = self._call("read")
        except Exception as exc:
            self._error = exc
            self._discard()
        finally:
            self._finish()

    def _discard(self):
        try:
            self._call("discard")
            self._released = True
        except Exception as exc:
            self.metadata["discard_error"] = str(exc)

    def _save(self):
        from .client import _json
        self.metadata.update(frames=self.frames, samples=self._wave.samples, rate=self._wave.rate or 16000)
        _json(self.metadata_path, self.metadata)

    def _finish(self):
        try:
            if self._error is not None:
                self.metadata.update(status="failed", error=str(self._error), error_type=type(self._error).__name__)
            self._wave.header()
            self._save()
        except Exception as exc:
            if self._error is None:
                self._error = exc
        finally:
            self._wave.stream.close()
            self._raw.close()
            self._done.set()

    def stop(self, *, timeout=12):
        from .client import Delivery
        if not isinstance(timeout, (int, float)) or not 0 < timeout <= 60:
            raise ValueError("timeout must be >0 and <=60 seconds")
        self._stop.set()
        if not self._done.wait(timeout):
            raise DeliveryUnknown("Audio recording stop remains unconfirmed; do not start another recording")
        if self._thread is not None:
            self._thread.join(timeout=1)
        if self._error is not None:
            self._error_reported = True
            raise self._error
        return Delivery("completed", dict(self.metadata))

    def cancel(self):
        if self._done.is_set() and self._error_reported and self._released:
            from .client import Delivery
            return Delivery(self.metadata["status"], dict(self.metadata))
        return self.stop()
