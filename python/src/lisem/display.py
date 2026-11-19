"""Explicit bounded capture of native display publications."""
import base64
from contextlib import AbstractContextManager
import math
import struct
import zlib

from .errors import (HistoryLost, OperationTimeout, ProtocolError, RunEnded,
                     preserve_exception)


class DisplayFrame:
    """One immutable BGRA publication with its original clocks and sequence."""

    def __init__(self, bgra, metadata):
        self.bgra, self.metadata = bgra, metadata

    def screenshot(self):
        from .client import Screenshot
        width, height = self.metadata['width'], self.metadata['height']
        rgba = bytearray(self.bgra)
        rgba[0::4], rgba[2::4] = self.bgra[2::4], self.bgra[0::4]
        stride = width * 4
        rows = b''.join(b'\0' + rgba[y * stride:(y + 1) * stride] for y in range(height))

        def chunk(kind, data):
            return (struct.pack('>I', len(data)) + kind + data
                    + struct.pack('>I', zlib.crc32(kind + data)))

        png = (b'\x89PNG\r\n\x1a\n'
               + chunk(b'IHDR', struct.pack('>2I5B', width, height, 8, 6, 0, 0, 0))
               + chunk(b'IDAT', zlib.compress(rows)) + chunk(b'IEND', b''))
        return Screenshot(png, dict(self.metadata))

    def save(self, path):
        return self.screenshot().save(path)


class DisplayCapture(AbstractContextManager):
    """Capture subsequent publications; stop and read frames before close()."""

    def __init__(self, run, *, max_frames=128, max_seconds=5):
        if isinstance(max_frames, bool) or not isinstance(max_frames, int) or not 1 <= max_frames <= 4096:
            raise ValueError('max_frames must be an integer in 1..4096')
        if (isinstance(max_seconds, bool) or not isinstance(max_seconds, (int, float))
                or not 0.001 <= max_seconds <= 60 or not math.isfinite(max_seconds)):
            raise ValueError('max_seconds must be finite and in 0.001..60 host seconds')
        self.run, self.closed = run, False
        self.metadata = run._call('display_capture', dict(action='start', capacity=max_frames,
                                                         max_ms=math.ceil(max_seconds * 1000)))
        self.id = self.metadata.get('capture')
        if not isinstance(self.id, str) or self.metadata.get('run') != run.id:
            raise ProtocolError('Invalid display capture start identity')
        # Register even when the queue filled before the start reply arrived.
        run.tasks.append(self)
        self._validate(self.metadata)

    def _validate(self, value):
        if value.get('capture') != self.id or value.get('run') != self.run.id:
            raise ProtocolError('Display capture identity changed')
        if value.get('status') not in ('running', 'completed', 'overflow', 'expired',
                                     'interrupted', 'discarded'):
            raise ProtocolError('Invalid display capture status')
        self.metadata = {k: v for k, v in value.items() if k not in ('data', 'offset', 'next_offset')}
        return value

    def _call(self, action, **params):
        if self.closed:
            raise ProtocolError('Display capture is closed')
        return self._validate(self.run._call('display_capture',
            dict(action=action, capture=self.id, **params), mutation=action not in ('read', 'status')))

    @staticmethod
    def _check(value):
        status = value.get('status')
        if status == 'overflow':
            raise HistoryLost('Display capture capacity exhausted; retained frames are incomplete')
        if status == 'expired':
            raise OperationTimeout('Display capture host deadline expired')
        if status == 'interrupted':
            raise RunEnded('Display capture was interrupted')
        if status not in ('running', 'completed'):
            raise ProtocolError('Invalid display capture status')

    def status(self):
        """Return status without masking failure, for inspecting retained evidence."""
        return self._call('status')

    def stop(self):
        from .client import Delivery
        value = self._call('stop')
        self._check(value)
        if value['status'] != 'completed':
            raise ProtocolError('Display capture stop was not confirmed')
        return Delivery('completed', value)

    def frames(self, *, allow_incomplete=False):
        """Read retained frames after stop; opt in explicitly for failed evidence."""
        state = self.status()
        if state['status'] == 'running':
            raise ProtocolError('Stop display capture before reading frames')
        if not allow_incomplete:
            self._check(state)
        previous = state['start_sequence']
        for index in range(state['frames']):
            data, offset, identity = bytearray(), 0, None
            while True:
                value = self._call('read', index=index, offset=offset, limit=1024 * 1024)
                if not allow_incomplete:
                    self._check(value)
                if value.get('index') != index or value.get('offset') != offset:
                    raise ProtocolError('Display capture frame cursor changed')
                if identity is None:
                    identity = value['frame']
                    if identity['sequence'] != previous + 1:
                        raise HistoryLost('Display capture publication sequence is not contiguous')
                elif value['frame'] != identity:
                    raise ProtocolError('Display capture frame identity changed')
                raw = base64.b64decode(value['data'], validate=True)
                size = state['width'] * state['height'] * 4
                if (value.get('bytes') != size or not raw or len(raw) > 1024 * 1024
                        or value.get('next_offset') != offset + len(raw) or offset + len(raw) > size):
                    raise ProtocolError('Invalid display capture frame bytes')
                data.extend(raw)
                offset += len(raw)
                if offset == size:
                    break
            previous = identity['sequence']
            yield DisplayFrame(bytes(data), dict(capture=self.id, run=self.run.id,
                instance_id=self.run.instance.id, width=state['width'], height=state['height'],
                frame=identity, index=index, capture_status=state['status'], pixel_format='bgra8'))
        if state['status'] == 'completed' and previous != state['end_sequence']:
            raise HistoryLost('Display capture end sequence does not match retained frames')

    def close(self):
        """Release native storage; callers must stop/check before discarding evidence."""
        if not self.closed:
            metadata = self.metadata
            value = self._call('discard')
            if value['status'] != 'discarded':
                raise ProtocolError('Display capture release was not confirmed')
            self.closed = True
            self.metadata = dict(metadata, released=True)

    def cancel(self):
        if self.closed:
            return
        try:
            self.stop()
        except Exception as error:
            try:
                self.close()
            except Exception as cleanup:
                preserve_exception(error, cleanup)
            raise
        self.close()

    def __exit__(self, typ, exc, tb):
        if not self.closed:
            try:
                self.cancel()
            except Exception as cleanup:
                preserve_exception(exc, cleanup)
