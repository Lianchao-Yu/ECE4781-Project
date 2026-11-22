"""
BME three-channel (EMG / PPG / EDA) BLE live monitor.

Usage:
    python bme_plot.py [--auto-connect]

Flash the firmware to an ESP32-S3 first, power the board, then click Connect
(or start with --auto-connect). Dependencies are listed in requirements.txt.
"""

from __future__ import annotations

import asyncio
import collections
import csv
import gc
import math
import struct
import sys
import threading
import time
from dataclasses import dataclass, field

import numpy as np
from PyQt6.QtCore import QLineF, QPointF, QRectF, Qt, QTimer
from PyQt6.QtGui import QColor, QFont, QPainter, QPen, QPolygonF
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from bleak import BleakClient, BleakScanner

# ---------------------------------------------------------------- protocol

SERVICE_UUID = "5f9b1c00-3e4a-4d21-9a77-0b1c2d3e4f50"
TX_UUID = "5f9b1c01-3e4a-4d21-9a77-0b1c2d3e4f50"
RX_UUID = "5f9b1c02-3e4a-4d21-9a77-0b1c2d3e4f50"
DEVICE_NAME = "BME-S3"

MAGIC = b"\xA5\x5A"
FRAME = struct.Struct("<BBBBHB10h2h")
assert FRAME.size == 31

EMG_FS = 1000.0
PPG_FS = 100.0
EDA_FS = 100.0

# How much history the host keeps for the scrollbar (seconds).
HISTORY_S = 60.0

WINDOW_CHOICES = [1, 2, 5, 10, 30]
DETAIL_CHOICES = [("Full", 1), ("1/2", 2), ("1/4", 4), ("1/8", 8), ("1/16", 16), ("1/32", 32)]
ZOOM_CHOICES = [("1x", 1.0), ("2x", 2.0), ("5x", 5.0), ("10x", 10.0), ("20x", 20.0)]

# Nominal Y span at 1x, in millivolts. 1x covers the ADC full scale, so nothing
# is clipped by default; zoom in for detail.
BASE_SPAN_MV = 3200.0

# ADC: 12 bit over ~3.1 V with 12 dB attenuation; the firmware reports
# (raw - 2048) * 8, so one count is 3.1 / 4095 / 8 V.
MV_PER_COUNT = 3.1 / 4095.0 / 8.0 * 1000.0

CMD_START = b"\x01"
CMD_STOP = b"\x00"

BG_COLOR = QColor("#1b1b1f")
GRID_COLOR = QColor(255, 255, 255, 26)
AXIS_COLOR = QColor("#9a9aa8")
TITLE_COLOR = QColor("#d8d8de")

# ---------------------------------------------------------------- ring buffer


class Ring:
    """Fixed-size circular buffer of float32 samples."""

    def __init__(self, size: int):
        self.buf = np.zeros(size, np.float32)
        self.size = size
        self.head = 0  # next write position
        self.count = 0
        self.total = 0  # samples ever pushed, never wraps

    def clear(self) -> None:
        self.head = 0
        self.count = 0
        self.total = 0
        self.buf[:] = 0

    def push(self, value: float) -> None:
        self.buf[self.head] = value
        self.head = (self.head + 1) % self.size
        if self.count < self.size:
            self.count += 1
        self.total += 1

    def push_many(self, values: np.ndarray) -> None:
        n = len(values)
        if n == 0:
            return
        if n >= self.size:
            self.buf[:] = values[-self.size :]
            self.head = 0
            self.count = self.size
            return
        end = self.head + n
        if end <= self.size:
            self.buf[self.head : end] = values
        else:
            first = self.size - self.head
            self.buf[self.head :] = values[:first]
            self.buf[: end - self.size] = values[first:]
        self.head = end % self.size
        self.count = min(self.count + n, self.size)
        self.total += n

    def tail(self, n: int, offset: int = 0) -> np.ndarray:
        """Return the newest n samples that end `offset` samples before the head."""
        available = self.count - offset
        if available <= 0:
            return np.empty(0, np.float32)
        n = min(n, available)
        end = (self.head - offset) % self.size
        start = (end - n) % self.size
        if start < end:
            return self.buf[start:end]
        return np.concatenate((self.buf[start:], self.buf[:end]))


@dataclass
class Buffers:
    """Written by the BLE thread, read by the Qt thread; guarded by lock."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    emg: Ring = field(default_factory=lambda: Ring(int(EMG_FS * HISTORY_S)))
    ppg: Ring = field(default_factory=lambda: Ring(int(PPG_FS * HISTORY_S)))
    eda: Ring = field(default_factory=lambda: Ring(int(EDA_FS * HISTORY_S)))
    emg_rate: float = 0.0
    frames: int = 0
    lost: int = 0
    last_seq: int | None = None
    bench: bool = False
    saturated: bool = False

    def clear(self) -> None:
        with self.lock:
            self.emg.clear()
            self.ppg.clear()
            self.eda.clear()
            self.frames = 0
            self.lost = 0
            self.last_seq = None
            self.emg_rate = 0.0

    def oldest_seconds(self) -> float:
        with self.lock:
            return self.emg.count / EMG_FS


class Decoder:
    """Frame parser that tolerates split notifications and garbage bytes."""

    def __init__(self, bufs: Buffers):
        self.bufs = bufs
        self.buf = bytearray()
        self.rate_t0 = time.monotonic()
        self.rate_samples = 0

    def feed(self, data: bytes) -> None:
        self.buf += data
        while True:
            idx = self.buf.find(MAGIC)
            if idx < 0:
                # Keep the last byte: it may be the first half of a magic word.
                del self.buf[: max(0, len(self.buf) - 1)]
                return
            if idx > 0:
                del self.buf[:idx]
            if len(self.buf) < FRAME.size:
                return
            raw = bytes(self.buf[: FRAME.size])
            (m0, m1, ver, flags, seq, n_emg, *rest) = FRAME.unpack(raw)
            emg = np.array(rest[:10], np.float32)
            ppg = float(rest[10])
            eda = float(rest[11])

            with self.bufs.lock:
                if self.bufs.last_seq is not None:
                    gap = (seq - self.bufs.last_seq) & 0xFFFF
                    # A huge gap means the device rebooted and seq restarted,
                    # which is not packet loss.
                    if 1 < gap < 6000:
                        self.bufs.lost += gap - 1
                self.bufs.last_seq = seq
                self.bufs.frames += 1
                self.bufs.bench = bool(flags & 0x01)
                self.bufs.saturated = bool(flags & 0x02)
                self.bufs.emg.push_many(emg)
                self.bufs.ppg.push(ppg)
                self.bufs.eda.push(eda)

            self.rate_samples += n_emg
            now = time.monotonic()
            dt = now - self.rate_t0
            if dt >= 1.0:
                with self.bufs.lock:
                    self.bufs.emg_rate = self.rate_samples / dt
                self.rate_t0 = now
                self.rate_samples = 0

            del self.buf[: FRAME.size]

    def reset_sequence(self) -> None:
        """Drop the sequence baseline so a new session is not counted as loss."""
        with self.bufs.lock:
            self.bufs.last_seq = None

# ---------------------------------------------------------------- BLE thread


class BLERunner:
    """Runs an asyncio loop in a worker thread; the UI only submits requests.

    Disconnects trigger an automatic rescan, so unplugging the board or moving
    it to a standalone power supply needs no manual reconnect.
    """

    RECONNECT_DELAY = 1.5

    def __init__(self, decoder: Decoder, log) -> None:
        self.decoder = decoder
        self.log = log
        self.loop = asyncio.new_event_loop()
        self.client: BleakClient | None = None
        self._want_stop = True
        self._waiting_logged = False
        self.thread = threading.Thread(target=self._run, daemon=True, name="ble-loop")
        self.thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def submit(self, coro) -> None:
        asyncio.run_coroutine_threadsafe(coro, self.loop)

    def connect(self) -> None:
        self._want_stop = False
        self.submit(self._connect_loop())

    def disconnect(self) -> None:
        self._want_stop = True
        self.submit(self._disconnect())

    def shutdown(self) -> None:
        self._want_stop = True
        try:
            self.loop.call_soon_threadsafe(self.loop.stop)
        except RuntimeError:
            pass

    async def _connect_loop(self) -> None:
        while not self._want_stop:
            if await self._try_connect():
                return
            if self._want_stop:
                return
            await asyncio.sleep(self.RECONNECT_DELAY)

    async def _try_connect(self) -> bool:
        try:
            def is_target(dev, adv):
                name = adv.local_name or dev.name or ""
                if name.startswith(DEVICE_NAME):
                    return True
                return any(u.lower() == SERVICE_UUID for u in (adv.service_uuids or []))

            device = await BleakScanner.find_device_by_filter(is_target, timeout=8.0)
            if device is None:
                if not self._waiting_logged:
                    self.log(f"Waiting for {DEVICE_NAME} to advertise ... (auto reconnect)")
                    self._waiting_logged = True
                return False

            self.log(f"Device found, connecting to {device.name or device.address} ...")
            client = BleakClient(device, disconnected_callback=self._on_disconnect)
            await client.connect()
            self.client = client
            self.decoder.reset_sequence()

            await client.start_notify(TX_UUID, self._on_notify)
            try:
                await client.write_gatt_char(RX_UUID, CMD_START, response=False)
            except Exception as exc:  # the start command is optional
                self.log(f"Failed to send start command: {exc}")

            self._waiting_logged = False
            self.log(f"Connected, MTU={getattr(client, 'mtu_size', None)}")
            return True
        except Exception as exc:
            self.client = None
            self.log(f"Connect failed: {exc}, retrying in {self.RECONNECT_DELAY:.1f} s")
            return False

    async def _disconnect(self) -> None:
        if self.client is not None:
            try:
                await self.client.disconnect()
            except Exception:
                pass
            self.client = None
        self.log("Disconnected")

    def send_command(self, payload: bytes) -> None:
        """Write a command to the RX characteristic."""
        async def _send() -> None:
            if self.client is None:
                return
            try:
                await self.client.write_gatt_char(RX_UUID, payload, response=False)
            except Exception as exc:
                self.log(f"Command failed: {exc}")

        self.submit(_send())

    def _on_disconnect(self, _client) -> None:
        self.client = None
        if self._want_stop:
            return
        self.log("Link lost, waiting for the device to come back ...")
        self.loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self._connect_loop()))

    def _on_notify(self, _char, data: bytearray) -> None:
        self.decoder.feed(bytes(data))


# ---------------------------------------------------------------- plotting


class WavePane(QWidget):
    """A fixed-gain waveform pane painted directly with QPainter.

    The X axis is an absolute time axis: the window shows [t0, t1] seconds
    since the first sample, so the tick labels advance as data streams in.
    """

    LEFT, RIGHT, TOP, BOTTOM = 62, 12, 24, 38

    def __init__(self, title: str, color: str, base_span_mv: float):
        super().__init__()
        self.setMinimumHeight(140)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self._title = title
        self._color = QColor(color)
        self._base_span = base_span_mv
        self._window_s = 5.0
        self._t0 = 0.0
        self._t1 = 5.0
        self._start_s = 0.0
        self._center = 0.0
        self._span = base_span_mv
        self._values = np.empty(0, np.float32)
        self._fs = 1000.0
        self._font = QFont("Segoe UI", 8)
        self._hover_x: float | None = None
        self.setMouseTracking(True)

    # ---- configuration

    def set_window(self, window_s: float) -> None:
        if abs(window_s - self._window_s) > 1e-9:
            self._window_s = window_s
            self.update()

    def set_time_window(self, t0: float, t1: float) -> None:
        if abs(t0 - self._t0) > 1e-6 or abs(t1 - self._t1) > 1e-6:
            self._t0 = t0
            self._t1 = t1
            # Only the plot and the time labels depend on the window. A full
            # update here would repaint the Y labels and title every frame and
            # triples the paint cost.
            plot = self._plot_rect()
            bottom = QRectF(plot.left(), plot.bottom(), plot.width(), self.BOTTOM)
            self.update(plot.toAlignedRect().united(bottom.toAlignedRect()))

    def set_level(self, center_mv: float, span_mv: float) -> None:
        if abs(center_mv - self._center) > 0.01 or abs(span_mv - self._span) > 1e-6:
            self._center = center_mv
            self._span = span_mv
            self.update()

    def set_trace(self, values_mv: np.ndarray, fs_eff: float, start_s: float) -> None:
        self._values = values_mv
        self._fs = fs_eff
        self._start_s = start_s
        # The X grid and its labels move with time, so the plot plus the time
        # strip are refreshed; the Y labels only change when the level does.
        plot = self._plot_rect()
        bottom = QRectF(plot.left(), plot.bottom(), plot.width(), self.BOTTOM)
        self.update(plot.toAlignedRect().united(bottom.toAlignedRect()))

    def _plot_rect(self) -> QRectF:
        return QRectF(
            self.LEFT,
            self.TOP,
            max(1, self.width() - self.LEFT - self.RIGHT),
            max(1, self.height() - self.TOP - self.BOTTOM),
        )

    # ---- mouse readout

    def mouseMoveEvent(self, event) -> None:
        x = event.position().x()
        plot = self._plot_rect()
        if plot.left() <= x <= plot.right():
            self._hover_x = x
        else:
            self._hover_x = None
        self.update(plot.toAlignedRect())
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:
        self._hover_x = None
        self.update(self._plot_rect().toAlignedRect())
        super().leaveEvent(event)

    def _draw_cursor(self, p: QPainter, plot: QRectF, lo: float) -> None:
        """Vertical cursor with the exact time and value under the mouse."""
        if self._hover_x is None or self._values.size < 2:
            return
        frac = (self._hover_x - plot.left()) / plot.width()
        t = self._t0 + frac * (self._t1 - self._t0)

        # Nearest sample of the drawn trace.
        idx = int(round((t - self._start_s) * self._fs))
        idx = max(0, min(idx, self._values.size - 1))
        value = float(self._values[idx])

        p.setPen(QPen(QColor(255, 255, 255, 70), 1, Qt.PenStyle.DashLine))
        p.drawLine(QPointF(self._hover_x, plot.top()),
                   QPointF(self._hover_x, plot.bottom()))

        p.setFont(self._font)
        text = f"t = {t:.2f} s   {value:.0f} mV"
        metrics = p.fontMetrics()
        width = metrics.horizontalAdvance(text) + 12
        box = QRectF(plot.left() + 6, plot.top() + 6, width, 18)
        p.fillRect(box, QColor(0, 0, 0, 160))
        p.setPen(AXIS_COLOR)
        p.drawText(box, int(Qt.AlignmentFlag.AlignCenter), text)

    # ---- painting

    def _ticks(self, lo: float, hi: float, target: int) -> list[float]:
        span = hi - lo
        if span <= 0:
            return []
        raw = span / target
        mag = 10 ** math.floor(math.log10(raw))
        step = 10.0 * mag
        for m in (1.0, 2.0, 2.5, 5.0, 10.0):
            if raw <= m * mag:
                step = m * mag
                break
        start = math.ceil(lo / step) * step
        out = []
        value = start
        while value <= hi + 1e-9:
            out.append(value)
            value += step
        return out

    def _x_step(self) -> float:
        for candidate in (0.5, 1.0, 2.0, 5.0, 10.0, 15.0, 30.0):
            if self._window_s / candidate <= 8:
                return candidate
        return 30.0

    def _draw_x_labels(self, p: QPainter, plot: QRectF) -> None:
        """Time labels move with the stream, so they are redrawn per frame."""
        p.setFont(self._font)
        p.setPen(AXIS_COLOR)
        step = self._x_step()
        value = math.ceil(self._t0 / step) * step
        while value <= self._t1 + 1e-9:
            x = plot.left() + (value - self._t0) / (self._t1 - self._t0) * plot.width()
            text = f"{value:.1f}" if step < 1 else f"{value:.0f}"
            p.drawText(QRectF(x - 30, plot.bottom() + 2, 60, 14),
                       int(Qt.AlignmentFlag.AlignCenter), text)
            value += step
        p.drawText(QRectF(0, self.height() - 18, self.width(), 16),
                   int(Qt.AlignmentFlag.AlignCenter), "Time (s)")

    def _draw_chrome(self, p: QPainter, plot: QRectF, lo: float, hi: float) -> None:
        """Y labels, titles and border; only redrawn when those areas are dirty."""
        p.setFont(self._font)
        p.setPen(AXIS_COLOR)
        for value in self._ticks(lo, hi, 5):
            y = plot.bottom() - (value - lo) / (hi - lo) * plot.height()
            text = f"{value:.0f}" if abs(value) >= 10 or value == 0 else f"{value:.1f}"
            p.drawText(QRectF(0, y - 8, self.LEFT - 8, 16),
                       int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter), text)

        p.setPen(TITLE_COLOR)
        p.setFont(QFont("Segoe UI", 9))
        p.drawText(QRectF(plot.left(), 2, plot.width(), self.TOP - 4),
                   int(Qt.AlignmentFlag.AlignCenter), self._title)

        p.setPen(AXIS_COLOR)
        p.setFont(self._font)
        p.save()
        p.translate(14, plot.center().y())
        p.rotate(-90)
        p.drawText(QRectF(-plot.height() / 2, -12, plot.height(), 16),
                   int(Qt.AlignmentFlag.AlignCenter), "Amplitude (mV)")
        p.restore()

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        p.fillRect(event.rect(), BG_COLOR)

        dirty = QRectF(event.rect())
        plot = self._plot_rect()
        lo = self._center - self._span / 2.0
        hi = self._center + self._span / 2.0

        # Grid lines live inside the plot area; they are cheap to redraw.
        p.save()
        p.setClipRect(plot.intersected(dirty))
        p.setPen(QPen(GRID_COLOR, 1))
        for value in self._ticks(lo, hi, 5):
            y = plot.bottom() - (value - lo) / (hi - lo) * plot.height()
            p.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
        step = self._x_step()
        value = math.ceil(self._t0 / step) * step
        while value <= self._t1 + 1e-9:
            x = plot.left() + (value - self._t0) / (self._t1 - self._t0) * plot.width()
            p.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
            value += step
        p.restore()

        p.setPen(QPen(QColor(255, 255, 255, 40), 1))
        p.drawRect(plot)

        # Labels move with time, so the bottom strip is refreshed every frame;
        # the Y labels and title only when their areas are actually dirty.
        bottom_strip = QRectF(0, plot.bottom(), self.width(), self.BOTTOM)
        if dirty.intersects(bottom_strip):
            self._draw_x_labels(p, plot)
        left_strip = QRectF(0, 0, self.LEFT, self.height())
        top_strip = QRectF(0, 0, self.width(), self.TOP)
        if dirty.intersects(left_strip) or dirty.intersects(top_strip):
            self._draw_chrome(p, plot, lo, hi)

        values = self._values
        if values.size < 2:
            p.end()
            return

        p.save()
        p.setClipRect(plot.intersected(dirty))
        if values.size >= plot.width():
            self._draw_envelope(p, plot, lo)
        else:
            self._draw_polyline(p, plot, lo)
        p.restore()

        self._draw_cursor(p, plot, lo)
        p.end()

    def _draw_polyline(self, p: QPainter, plot: QRectF, lo: float) -> None:
        """Fewer points than pixels: a plain polyline is the clearest."""
        n = self._values.size
        scale_y = plot.height() / self._span
        scale_x = plot.width() / max(1e-9, self._t1 - self._t0)
        ts = self._start_s + np.arange(n) / self._fs
        xs = plot.left() + (ts - self._t0) * scale_x
        ys = plot.bottom() - (self._values - lo) * scale_y
        points = [QPointF(x, y) for x, y in zip(xs.tolist(), ys.tolist())]
        p.setPen(QPen(self._color, 1))
        p.drawPolyline(QPolygonF(points))

    def _draw_envelope(self, p: QPainter, plot: QRectF, lo: float) -> None:
        """One vertical segment per pixel column with that column's min/max.

        This keeps every spike visible (no sample is skipped) while drawing
        only ~1 line per pixel instead of thousands of QPointF objects, which
        is what made high-detail modes slow.
        """
        values = self._values
        n = values.size
        width = max(2, int(plot.width()))
        scale_x = plot.width() / max(1e-9, self._t1 - self._t0)
        scale_y = plot.height() / self._span

        ts = self._start_s + np.arange(n, dtype=np.float64) / self._fs
        ix = np.floor((ts - self._t0) * scale_x).astype(np.int32)
        iy = np.rint(plot.bottom() - (values - lo) * scale_y).astype(np.int32)

        keep = (ix >= 0) & (ix < width)
        if not keep.any():
            return
        ix = ix[keep]
        iy = iy[keep]

        # ix is non-decreasing because the samples are in time order.
        starts = np.concatenate(([0], np.flatnonzero(np.diff(ix)) + 1))
        cols = ix[starts]
        ymin = np.minimum.reduceat(iy, starts)
        ymax = np.maximum.reduceat(iy, starts)

        # Bridge neighbouring columns so steep edges do not show gaps.
        if cols.size > 1:
            prev_min = ymin[:-1].copy()
            prev_max = ymax[:-1].copy()
            ymin[1:] = np.minimum(ymin[1:], prev_max)
            ymax[1:] = np.maximum(ymax[1:], prev_min)

        left = plot.left()
        lines = [
            QLineF(left + cols[i] + 0.5, ymin[i], left + cols[i] + 0.5, ymax[i] + 1)
            for i in range(cols.size)
        ]
        p.setPen(QPen(self._color, 1))
        p.drawLines(lines)

# ---------------------------------------------------------------- main window


class MainWindow(QMainWindow):
    FRAME_MS = 25.0

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("BME 3-Channel Live Monitor (EMG / PPG / EDA)")
        geo = QApplication.primaryScreen().availableGeometry()
        self.resize(min(1200, geo.width() - 60), min(900, geo.height() - 80))

        # Live plotting allocates many short-lived arrays. Freezing the startup
        # objects and raising the thresholds keeps the collector from causing
        # the occasional ~100 ms hitch.
        gc.freeze()
        gc.set_threshold(20000, 50, 50)

        self.bufs = Buffers()
        self.decoder = Decoder(self.bufs)
        self.ble = BLERunner(self.decoder, self._log)

        self.records: list[tuple[float, ...]] = []
        self.recording = False
        self.t0 = time.monotonic()
        self._last_frames = 0

        self.window_s = 5.0
        self.detail = 16
        self.zoom = 1.0
        self._centers = {"emg": None, "ppg": None, "eda": None}
        # Effective span per pane: the selected zoom is an upper bound, each
        # channel is clamped so its own waveform always fits.
        self._spans = {key: BASE_SPAN_MV for key in ("emg", "ppg", "eda")}
        self._applied = {key: (None, None) for key in ("emg", "ppg", "eda")}
        self._status_t = 0.0
        self._paused = False
        self._paused_end_s = 0.0
        self._fitting = False
        # Playback clock: the head advances at the measured sample rate and
        # stays a little behind the newest sample, so bursty BLE delivery does
        # not turn into stop-and-jump motion on screen.
        self._play_last_t: float | None = None
        self._play_last_index = 0.0
        self._play_pos = 0.0
        self._rate_est = EMG_FS
        self._rate_hist = collections.deque()
        self.PLAY_MARGIN_S = 0.15

        central = QWidget()
        layout = QVBoxLayout(central)

        # ---- row 1: session buttons
        bar = QHBoxLayout()
        self.btn_connect = QPushButton("Connect")
        self.btn_disconnect = QPushButton("Disconnect")
        self.btn_stop = QPushButton("Stop sampling")
        self.btn_stop.setCheckable(True)
        self.btn_record = QPushButton("Start recording")
        self.btn_clear = QPushButton("Clear")
        self.btn_save = QPushButton("Save CSV")
        for b in (self.btn_connect, self.btn_disconnect, self.btn_stop,
                  self.btn_record, self.btn_clear, self.btn_save):
            bar.addWidget(b)
        self.status = QLabel("Not connected")
        # A fixed width keeps the toolbar from re-laying out whenever the
        # numbers change; per-frame relayout is surprisingly expensive.
        self.status.setMinimumWidth(430)
        bar.addStretch(1)
        bar.addWidget(self.status)
        layout.addLayout(bar)

        # ---- row 2: view controls
        controls = QHBoxLayout()

        controls.addWidget(QLabel("Window"))
        self.cmb_window = QComboBox()
        for w in WINDOW_CHOICES:
            self.cmb_window.addItem(f"{w} s", w)
        self.cmb_window.setCurrentIndex(WINDOW_CHOICES.index(5))
        controls.addWidget(self.cmb_window)

        controls.addSpacing(12)
        controls.addWidget(QLabel("Detail"))
        self.cmb_detail = QComboBox()
        for label, factor in DETAIL_CHOICES:
            self.cmb_detail.addItem(label, factor)
        self.cmb_detail.setCurrentIndex(4)  # 1/16
        controls.addWidget(self.cmb_detail)

        controls.addSpacing(12)
        controls.addWidget(QLabel("Zoom"))
        self.cmb_zoom = QComboBox()
        for label, factor in ZOOM_CHOICES:
            self.cmb_zoom.addItem(label, factor)
        self.cmb_zoom.setCurrentIndex(0)  # 1x
        controls.addWidget(self.cmb_zoom)

        controls.addSpacing(12)
        self.btn_center = QPushButton("Center")
        controls.addWidget(self.btn_center)

        controls.addStretch(1)
        self.delay_label = QLabel("Delay: 0.0 s")
        controls.addWidget(self.delay_label)
        layout.addLayout(controls)

        # ---- plots
        self.panes = {
            "emg": WavePane("EMG 1 kHz", "#4fc3f7", BASE_SPAN_MV),
            "ppg": WavePane("PPG 100 Hz", "#ef5350", BASE_SPAN_MV),
            "eda": WavePane("EDA 100 Hz (smoothed)", "#9ccc65", BASE_SPAN_MV),
        }
        for pane in self.panes.values():
            layout.addWidget(pane, 1)

        # ---- row 3: history scrubber
        scrub = QHBoxLayout()
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 1000)
        self.slider.setValue(1000)
        self.slider.setToolTip("Right end is live; drag left to review the last 60 s")
        self.btn_latest = QPushButton("Latest")
        scrub.addWidget(self.slider, 1)
        scrub.addWidget(self.btn_latest)
        layout.addLayout(scrub)

        self.setCentralWidget(central)

        # ---- signals
        self.btn_connect.clicked.connect(self.ble.connect)
        self.btn_disconnect.clicked.connect(self.ble.disconnect)
        self.btn_record.clicked.connect(self._toggle_record)
        self.btn_clear.clicked.connect(self._clear)
        self.btn_save.clicked.connect(self._save_csv)
        self.btn_latest.clicked.connect(lambda: self.slider.setValue(1000))
        self.slider.valueChanged.connect(self._on_slider)
        self.btn_center.clicked.connect(self._center_now)
        self.btn_stop.toggled.connect(self._toggle_sampling)
        self.cmb_window.currentIndexChanged.connect(self._on_window_changed)
        self.cmb_detail.currentIndexChanged.connect(self._on_detail_changed)
        self.cmb_zoom.currentIndexChanged.connect(self._on_zoom_changed)

        for pane in self.panes.values():
            pane.set_window(self.window_s)

        self.timer = QTimer(self)
        # Single-shot scheduling: the next frame is booked only after the
        # current one finishes, so a slow frame can never pile up repaints and
        # freeze the UI.
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._refresh)
        self.timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.timer.start(int(self.FRAME_MS))

        self._log("Ready, click Connect to start")

    # ---- control callbacks

    def _on_window_changed(self) -> None:
        self.window_s = float(self.cmb_window.currentData())
        for pane in self.panes.values():
            pane.set_window(self.window_s)
        self._autofit()

    def _on_detail_changed(self) -> None:
        self.detail = int(self.cmb_detail.currentData())
        self._autofit()

    def _on_zoom_changed(self) -> None:
        self.zoom = float(self.cmb_zoom.currentData())
        self._autofit()

    def _toggle_sampling(self, stopped: bool) -> None:
        """Stop/resume the device stream without dropping the BLE link."""
        self.ble.send_command(CMD_STOP if stopped else CMD_START)
        self.btn_stop.setText("Resume sampling" if stopped else "Stop sampling")
        self._log("Sampling stopped" if stopped else "Sampling resumed")

    def _on_slider(self, value: int) -> None:
        """Right end = live; any other position freezes the view there."""
        latest = self.bufs.emg.total / EMG_FS
        if value >= 999:
            self._paused = False
            self._paused_end_s = latest
            return
        # Only the buffered history can be reviewed; older data no longer exists.
        history = self.bufs.emg.count / EMG_FS
        max_delay = max(0.0, history - self.window_s)
        delay = max_delay * (1.0 - value / 1000.0)
        self._paused = True
        self._paused_end_s = max(0.0, latest - delay)

    @staticmethod
    def _range_mv(values: np.ndarray) -> tuple[float, float]:
        """Robust min/max in mV: ignores the extreme 0.5 % on both ends."""
        mv = values * MV_PER_COUNT
        return float(np.percentile(mv, 0.5)), float(np.percentile(mv, 99.5))

    @staticmethod
    def _extent_mv(values: np.ndarray) -> tuple[float, float]:
        """Full min/max in mV, used when fitting so nothing is clipped."""
        mv = values * MV_PER_COUNT
        return float(mv.min()), float(mv.max())

    def _center_now(self) -> None:
        end_s = self._paused_end_s if self._paused else self.bufs.emg.total / EMG_FS
        views = {}
        for key in self.panes:
            values, _ = self._channel_view(key, end_s)
            views[key] = values
            if values.size:
                lo, hi = self._range_mv(values)
                self._centers[key] = (lo + hi) / 2
        self._apply_levels(views, force=True)

    def _autofit(self) -> None:
        """Fit each pane to its own signal after a Detail/Zoom change.

        The zoom combo is an upper bound: a channel that would be clipped at
        that level keeps the finest span that still shows all of its data, so
        5x/10x/20x stay usable even though the three channels differ a lot in
        amplitude. Spans are only recomputed here, never continuously, so the
        trace does not breathe while it scrolls.
        """
        if self._fitting:
            return
        self._fitting = True
        try:
            end_s = self._paused_end_s if self._paused else self.bufs.emg.total / EMG_FS
            requested = BASE_SPAN_MV / self.zoom
            views = {}
            for key in self.panes:
                values, _ = self._channel_view(key, end_s)
                views[key] = values
                if not values.size:
                    continue
                lo, hi = self._extent_mv(values)
                self._centers[key] = (lo + hi) / 2
                needed = max(hi - lo, 1.0) * 1.10
                self._spans[key] = max(requested, needed)
            self._apply_levels(views, force=True)
        finally:
            self._fitting = False

    def _log(self, msg: str) -> None:
        self.status.setText(msg)
        print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

    def _toggle_record(self) -> None:
        if self.recording:
            self.recording = False
            self.btn_record.setText("Start recording")
            self._log(f"Recording stopped, {len(self.records)} samples")
        else:
            self.records.clear()
            self.t0 = time.monotonic()
            self.recording = True
            self.btn_record.setText("Stop recording")
            self._log("Recording started")

    def _playhead_s(self, latest_s: float) -> float:
        """Smoothly advancing playback position, in seconds."""
        latest_index = latest_s * EMG_FS
        now = time.monotonic()
        margin = self.PLAY_MARGIN_S * EMG_FS

        if self._play_last_t is None:
            self._play_last_t = now
            self._play_pos = max(0.0, latest_index - margin)
            self._rate_hist.append((now, latest_index))
            return self._play_pos / EMG_FS

        dt = now - self._play_last_t
        self._play_last_t = now
        self._rate_hist.append((now, latest_index))

        # Long-window rate estimate: bursty delivery makes per-frame deltas
        # useless, a couple of seconds averages them out.
        while len(self._rate_hist) > 2 and now - self._rate_hist[0][0] > 4.0:
            self._rate_hist.popleft()
        oldest_t, oldest_index = self._rate_hist[0]
        span = now - oldest_t
        if span > 1.5:
            rate = (latest_index - oldest_index) / span
            if 200.0 < rate < 5000.0:
                self._rate_est = rate

        self._play_pos += dt * self._rate_est

        # Slow pull towards newest - margin. Without it a 1 % rate error would
        # accumulate into a multi-second lag and periodic jumps. The gain is
        # small enough to stay invisible (±1 sample per burst).
        error = (latest_index - margin) - self._play_pos
        self._play_pos += error * 0.004

        # Hard bounds: never overtake the buffer, never fall more than 2 s back.
        self._play_pos = min(self._play_pos, latest_index - 0.05 * EMG_FS)
        if latest_index - self._play_pos > 2.0 * EMG_FS:
            self._play_pos = latest_index - margin
        self._play_pos = max(0.0, self._play_pos)
        return self._play_pos / EMG_FS

    def _clear(self) -> None:
        self.bufs.clear()
        self.records.clear()
        self._last_frames = 0
        self._centers = {"emg": None, "ppg": None, "eda": None}
        self._spans = {key: BASE_SPAN_MV for key in self.panes}
        self._applied = {key: (None, None) for key in self.panes}
        self._paused = False
        self._play_last_t = None
        self._play_pos = 0.0
        self._rate_est = EMG_FS
        self.slider.setValue(1000)

    def _save_csv(self) -> None:
        if not self.records:
            self._log("No data to save")
            return
        default = f"bme_{time.strftime('%Y%m%d_%H%M%S')}.csv"
        path, _ = QFileDialog.getSaveFileName(self, "Save CSV", default, "CSV (*.csv)")
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["time_s", "emg_counts", "ppg_counts", "eda_counts"])
            writer.writerows(self.records)
        self._log(f"Saved {len(self.records)} rows to {path}")

    # ---- data access

    def _channel_view(self, key: str, end_s: float,
                      margin_s: float = 0.25) -> tuple[np.ndarray, float]:
        """Samples ending at absolute time end_s, plus the time of the first one.

        A little extra history is included on the left so the polyline always
        extends past the clip edge; otherwise the leftmost vertex sits exactly
        on the boundary and pops out of view one sample at a time.
        """
        fs = {"emg": EMG_FS, "ppg": PPG_FS, "eda": EDA_FS}[key]
        ring = getattr(self.bufs, key)
        end_index = min(int(round(end_s * fs)), ring.total)
        start_index = max(0, end_index - int((self.window_s + margin_s) * fs))
        n = end_index - start_index
        with self.bufs.lock:
            values = ring.tail(n, ring.total - end_index)
        return values, (end_index - values.size) / fs

    def _prepare_trace(self, values: np.ndarray, start_s: float, fs: float,
                       key: str) -> tuple[np.ndarray, float, float]:
        """Smooth, decimate and align one channel for drawing."""
        if values.size < 2:
            return values, fs, start_s

        if key == "eda":
            # Causal 0.25 s average: the newest sample keeps the same value as
            # the previous one, so the right edge no longer jumps.
            k = 25
            padded = np.pad(values, (k - 1, 0), mode="edge")
            values = np.convolve(padded, np.ones(k, np.float32) / k, mode="valid")

        # Detail is defined for the 1 kHz EMG stream; scale it for slower
        # channels. Otherwise 1/16 would reduce 100 Hz data to 6.25 Hz and
        # destroy the PPG/EDA waveform.
        scale = max(1, int(round(EMG_FS / fs)))
        factor = max(1, self.detail // scale)
        if key != "emg":
            # 100 Hz channels carry no useful content above ~25 Hz; keeping
            # fewer points matters far more for painting cost than fidelity.
            factor = max(factor, int(math.ceil(fs / 50.0)))
        if factor <= 1 or values.size <= factor * 2:
            return values, fs, start_s

        # Snap the first sample to a multiple of `factor` in absolute sample
        # numbers. Skipping `abs_idx0 % factor` samples (the previous version)
        # moved the start *forward* by that amount, leaving the grid at
        # (idx0 + phase) % factor != 0, so the decimation pattern slid on every
        # frame and the stored waveform appeared to dance.
        abs_idx0 = int(round(start_s * fs))
        offset = (factor - (abs_idx0 % factor)) % factor
        if offset:
            values = values[offset:]
            start_s += offset / fs

        n_bins = values.size // factor
        if n_bins < 2:
            return values, fs, start_s

        trimmed = values[: n_bins * factor].reshape(n_bins, factor)
        # Mean of each bin. A min/max envelope would preserve single-sample
        # spikes, but on this broadband bench signal it paints a solid band
        # (each bin spans a large range) and triples the paint cost.
        return trimmed.mean(axis=1), fs / factor, start_s

    def _apply_levels(self, views: dict[str, np.ndarray], force: bool = False) -> None:
        """Apply the fixed Y level; the centre only moves when the user asks."""
        for key, pane in self.panes.items():
            values = views[key]
            if values.size and self._centers[key] is None:
                # Initial centring only; no automatic tracking afterwards.
                lo, hi = self._range_mv(values)
                self._centers[key] = (lo + hi) / 2

            center = self._centers[key] if self._centers[key] is not None else 0.0
            span = self._spans[key]

            last_center, last_span = self._applied[key]
            if (force or last_span != span or last_center is None
                    or abs(center - last_center) > 1e-6):
                pane.set_level(center, span)
                self._applied[key] = (center, span)

    # ---- periodic redraw

    def _refresh(self) -> None:
        frame_t0 = time.monotonic()
        try:
            self._refresh_frame()
        finally:
            # Adaptive pacing: keep ~40 FPS when frames are cheap, back off
            # automatically when one is expensive.
            elapsed_ms = (time.monotonic() - frame_t0) * 1000.0
            self.timer.start(int(max(5.0, self.FRAME_MS - elapsed_ms)))

    def _refresh_frame(self) -> None:
        latest_s = self.bufs.emg.total / EMG_FS

        # The slider is a timeline: far right is live, dragging left freezes
        # the view at that moment while acquisition keeps running.
        end_s = min(self._paused_end_s, latest_s) if self._paused else self._playhead_s(latest_s)
        # Never ask for a window that reaches further back than the buffer.
        history = self.bufs.emg.count / EMG_FS
        oldest_end = max(0.0, latest_s - max(0.0, history - self.window_s))
        end_s = max(end_s, oldest_end)
        max_delay = max(0.0, latest_s - oldest_end)
        if self._paused and max_delay <= 0.5:
            # Nothing older to review (window covers the whole buffer): go live.
            self._paused = False
            self.slider.blockSignals(True)
            self.slider.setValue(1000)
            self.slider.blockSignals(False)
        want_enabled = max_delay > 0.5
        if self.slider.isEnabled() != want_enabled:
            self.slider.setEnabled(want_enabled)

        delay = max(0.0, latest_s - end_s)
        if self._paused:
            text = f"Delay: {delay:5.1f} s  [PAUSED]"
        else:
            text = "Live"
        if self.delay_label.text() != text:
            self.delay_label.setText(text)

        if end_s <= self.window_s:
            t0, t1 = 0.0, float(self.window_s)
        else:
            t0, t1 = end_s - self.window_s, end_s

        views = {}
        starts = {}
        for key in self.panes:
            values, start_s = self._channel_view(key, end_s)
            views[key] = values
            starts[key] = start_s
            self.panes[key].set_time_window(t0, t1)

        self._apply_levels(views)

        for key, pane in self.panes.items():
            values = views[key]
            if values.size < 2:
                continue
            fs = {"emg": EMG_FS, "ppg": PPG_FS, "eda": EDA_FS}[key]
            values, fs_eff, start_s = self._prepare_trace(values, starts[key], fs, key)
            pane.set_trace(values * MV_PER_COUNT, fs_eff, start_s)

        # ---- recording (raw counts, 1 ms rows for EMG)
        with self.bufs.lock:
            frames = self.bufs.frames
            bench = self.bufs.bench
            saturated = self.bufs.saturated
            rate = self.bufs.emg_rate
            lost = self.bufs.lost
        new_frames = frames - self._last_frames
        self._last_frames = frames

        if self.recording and new_frames > 0:
            with self.bufs.lock:
                n_new = min(new_frames * 10, self.bufs.emg.count)
                emg_new = self.bufs.emg.tail(n_new).copy()
                ppg_last = float(self.bufs.ppg.tail(1)[0]) if self.bufs.ppg.count else 0.0
                eda_last = float(self.bufs.eda.tail(1)[0]) if self.bufs.eda.count else 0.0
            now = time.monotonic() - self.t0
            dt = 1.0 / EMG_FS
            base = now - len(emg_new) * dt
            self.records.extend(
                (round(base + i * dt, 4), float(v), ppg_last, eda_last)
                for i, v in enumerate(emg_new)
            )

        if frames:
            now = time.monotonic()
            if now - self._status_t < 0.5:
                return
            self._status_t = now
            loss_pct = 100.0 * lost / max(1, frames + lost)
            mode = "bench" if bench else "live"
            sat_text = " | saturated" if saturated else ""
            self.status.setText(
                f"{mode} | {rate:5.0f} Hz | frames {frames} | loss {loss_pct:4.1f}%{sat_text}"
            )

    def closeEvent(self, event) -> None:
        self.ble.shutdown()
        super().closeEvent(event)


def main() -> int:
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    # --auto-connect: scan and connect on startup (handy for demos/autostart).
    if "--auto-connect" in sys.argv or "-a" in sys.argv:
        QTimer.singleShot(300, win.ble.connect)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())




