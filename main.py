#!/usr/bin/env python3
"""
Lumen — Low-vision classroom assistant
Camera feed with zoom, pan, image enhancement, freeze-frame, and OCR.
"""

import sys
import os
import time
import datetime
import subprocess
import numpy as np
import cv2
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QSlider, QPushButton, QComboBox, QGroupBox, QSizePolicy,
    QTextEdit, QStatusBar,
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt6.QtGui import QImage, QPixmap, QKeySequence, QShortcut

# OCR — prefer Apple Vision (handles handwriting), fall back to Tesseract
try:
    import Vision
    from Foundation import NSURL
    VISION_AVAILABLE = True
except ImportError:
    VISION_AVAILABLE = False

try:
    import pytesseract
    pytesseract.pytesseract.tesseract_cmd = '/opt/homebrew/bin/tesseract'
    TESSERACT_AVAILABLE = True
except ImportError:
    TESSERACT_AVAILABLE = False

from PIL import Image as PILImage, ImageDraw, ImageFont
import tempfile

# macOS system fonts for Smart Board overlay (tried in order)
_FONT_PATHS = [
    "/System/Library/Fonts/Helvetica.ttc",
    "/Library/Fonts/Arial.ttf",
    "/System/Library/Fonts/ArialHB.ttf",
    "/System/Library/Fonts/SFNSDisplay.ttf",
]

OCR_AVAILABLE = VISION_AVAILABLE or TESSERACT_AVAILABLE

SAVE_DIR = os.path.expanduser("~/Desktop/Lumen_Captures")
os.makedirs(SAVE_DIR, exist_ok=True)


# ---------------------------------------------------------------------------
# Camera capture thread
# ---------------------------------------------------------------------------

class CameraThread(QThread):
    frame_ready = pyqtSignal(object)   # emits np.ndarray (RGB)
    error = pyqtSignal(str)

    def __init__(self, camera_index: int = 0):
        super().__init__()
        self.camera_index = camera_index
        self._running = False

    def run(self):
        # Use AVFoundation backend on macOS for reliable camera access
        cap = cv2.VideoCapture(self.camera_index, cv2.CAP_AVFOUNDATION)
        if not cap.isOpened():
            self.error.emit(f"Cannot open camera {self.camera_index}")
            return

        # 1080p for smooth live feed — plenty of detail for digital zoom
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1920)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 1080)
        cap.set(cv2.CAP_PROP_FPS, 30)

        actual_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        actual_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        self.error.emit(f"Camera {self.camera_index} — {actual_w}×{actual_h} — warming up…")

        # Warm-up: discard the first few frames — cameras take a moment
        # to initialise exposure and white balance
        for _ in range(5):
            cap.read()
            time.sleep(0.05)

        self.error.emit(f"Camera {self.camera_index} — {actual_w}×{actual_h} — live")

        fail_count = 0
        self._running = True
        while self._running:
            ret, frame = cap.read()
            if ret:
                fail_count = 0
                self.frame_ready.emit(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            else:
                fail_count += 1
                if fail_count > 10:
                    self.error.emit("Camera stopped sending frames — check cable")
                    break
                time.sleep(0.05)
        cap.release()

    def stop(self):
        self._running = False
        self.wait()


# ---------------------------------------------------------------------------
# Image processing
# ---------------------------------------------------------------------------

class ImageProcessor:
    def __init__(self):
        self.zoom      = 1.0   # 1.0–10.0
        self.pan_x     = 0.5   # normalised 0–1 (centre = 0.5)
        self.pan_y     = 0.5
        self.brightness = 0    # additive, –100 … +100
        self.contrast  = 1.0   # multiplicative, 0.5–3.0
        self.saturation = 1.0  # 0 = greyscale, 2 = vivid
        self.sharpness  = 0    # 0–10 (unsharp mask strength)
        self.invert     = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def process_live(self, frame: np.ndarray) -> np.ndarray:
        """Crop + adjust for the live feed (speed-optimised)."""
        cropped = self._crop(frame)
        return self._adjust(cropped)

    def process_freeze(self, frame: np.ndarray, display_size: tuple) -> np.ndarray:
        """Crop + high-quality upscale + sharpen for a freeze frame."""
        cropped = self._crop(frame)
        dw, dh = display_size
        upscaled = cv2.resize(cropped, (dw, dh), interpolation=cv2.INTER_LANCZOS4)
        if self.zoom > 2.0:
            upscaled = self._unsharp_mask(upscaled, amount=min(2.0, self.zoom * 0.3))
        return self._adjust(upscaled)

    def zoom_in(self, step: float = 0.5):
        self.zoom = min(10.0, round(self.zoom + step, 1))

    def zoom_out(self, step: float = 0.5):
        self.zoom = max(1.0, round(self.zoom - step, 1))

    def pan(self, dx: float, dy: float):
        """dx/dy are fractions of the *zoomed* viewport."""
        step = 0.05 / self.zoom
        self.pan_x = float(np.clip(self.pan_x + dx * step, 0.0, 1.0))
        self.pan_y = float(np.clip(self.pan_y + dy * step, 0.0, 1.0))

    def pan_pixels(self, dpx: int, dpy: int, display_w: int, display_h: int):
        """Pan by a pixel delta on the display (for mouse drag)."""
        self.pan_x = float(np.clip(self.pan_x + dpx / display_w / self.zoom, 0.0, 1.0))
        self.pan_y = float(np.clip(self.pan_y + dpy / display_h / self.zoom, 0.0, 1.0))

    def reset_pan(self):
        self.pan_x = 0.5
        self.pan_y = 0.5

    def reset_all(self):
        self.zoom = 1.0
        self.pan_x = 0.5
        self.pan_y = 0.5
        self.brightness = 0
        self.contrast = 1.0
        self.saturation = 1.0
        self.sharpness = 0
        self.invert = False

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _crop(self, frame: np.ndarray) -> np.ndarray:
        h, w = frame.shape[:2]
        crop_w = max(1, int(w / self.zoom))
        crop_h = max(1, int(h / self.zoom))

        cx = int(self.pan_x * w)
        cy = int(self.pan_y * h)

        x1 = int(np.clip(cx - crop_w // 2, 0, w - crop_w))
        y1 = int(np.clip(cy - crop_h // 2, 0, h - crop_h))
        return frame[y1:y1 + crop_h, x1:x1 + crop_w]

    def _adjust(self, frame: np.ndarray) -> np.ndarray:
        out = frame.astype(np.float32)

        # Brightness + contrast  (f(x) = contrast*(x + brightness - 127) + 127)
        out = self.contrast * (out + self.brightness - 127.0) + 127.0
        out = np.clip(out, 0, 255).astype(np.uint8)

        # Saturation via HSV
        if abs(self.saturation - 1.0) > 0.01:
            hsv = cv2.cvtColor(out, cv2.COLOR_RGB2HSV).astype(np.float32)
            hsv[:, :, 1] = np.clip(hsv[:, :, 1] * self.saturation, 0, 255)
            out = cv2.cvtColor(hsv.astype(np.uint8), cv2.COLOR_HSV2RGB)

        # Sharpness (unsharp mask)
        if self.sharpness > 0:
            out = self._unsharp_mask(out, amount=self.sharpness * 0.4)

        # Invert
        if self.invert:
            out = 255 - out

        return out

    @staticmethod
    def _unsharp_mask(img: np.ndarray, kernel: int = 5,
                       sigma: float = 1.2, amount: float = 1.0) -> np.ndarray:
        blurred = cv2.GaussianBlur(img, (kernel | 1, kernel | 1), sigma)
        sharp = np.clip((1 + amount) * img.astype(np.float32)
                        - amount * blurred.astype(np.float32), 0, 255)
        return sharp.astype(np.uint8)


# ---------------------------------------------------------------------------
# Video display widget (handles mouse drag for pan)
# ---------------------------------------------------------------------------

class VideoDisplay(QLabel):
    pan_dragged = pyqtSignal(int, int)   # pixel delta dx, dy

    def __init__(self):
        super().__init__()
        self.setMinimumSize(640, 480)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet("background-color: #000000;")
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self._drag_pos = None

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.position().toPoint()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def mouseMoveEvent(self, event):
        if self._drag_pos is not None:
            cur = event.position().toPoint()
            self.pan_dragged.emit(cur.x() - self._drag_pos.x(),
                                   cur.y() - self._drag_pos.y())
            self._drag_pos = cur

    def mouseReleaseEvent(self, event):
        self._drag_pos = None
        self.setCursor(Qt.CursorShape.OpenHandCursor)


# ---------------------------------------------------------------------------
# Preset profiles
# ---------------------------------------------------------------------------

PRESETS = {
    "Default": dict(brightness=0,   contrast=1.0, saturation=1.0, sharpness=0,  invert=False),
    "Whiteboard": dict(brightness=10, contrast=1.6, saturation=0.3, sharpness=4,  invert=False),
    "High Contrast": dict(brightness=0, contrast=2.0, saturation=0.0, sharpness=5,  invert=False),
    "Inverted Board": dict(brightness=0, contrast=1.8, saturation=0.0, sharpness=4,  invert=True),
    "Low Glare": dict(brightness=-30, contrast=1.3, saturation=0.7, sharpness=2,  invert=False),
}


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Lumen")
        self.setMinimumSize(1100, 750)

        self.proc = ImageProcessor()
        self.camera_thread: CameraThread | None = None
        self.current_frame: np.ndarray | None = None
        self.is_frozen = False
        self.ocr_mode = False
        self._controls_visible = True

        # Smart Board state
        self.frozen_raw: np.ndarray | None = None      # raw frame at freeze moment
        self.smartboard_raw: np.ndarray | None = None  # frame with text overlay
        self.show_smartboard = False                   # toggle state

        self._build_ui()
        self._build_shortcuts()
        self._apply_theme()
        self._discover_cameras()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(6, 6, 6, 6)
        root_layout.setSpacing(4)

        root_layout.addWidget(self._build_topbar())

        # Video + OCR side panel
        mid = QHBoxLayout()
        self.video = VideoDisplay()
        self.video.pan_dragged.connect(self._on_drag_pan)
        mid.addWidget(self.video, stretch=3)

        self.ocr_panel = self._build_ocr_panel()
        self.ocr_panel.setVisible(False)
        mid.addWidget(self.ocr_panel, stretch=1)
        root_layout.addLayout(mid, stretch=1)

        self.controls_widget = self._build_controls()
        root_layout.addWidget(self.controls_widget)

        self.statusbar = QStatusBar()
        self.statusbar.setStyleSheet("color: #888; font-size: 11px;")
        self.setStatusBar(self.statusbar)
        self._set_status("Starting…")

    def _build_topbar(self):
        bar = QWidget()
        bar.setFixedHeight(40)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(0, 0, 0, 0)

        # Camera selector
        cam_lbl = QLabel("Camera:")
        cam_lbl.setStyleSheet("color:#ccc; font-size:12px;")
        self.cam_combo = QComboBox()
        self.cam_combo.setFixedHeight(28)
        self.cam_combo.setMinimumWidth(130)
        self.cam_combo.currentIndexChanged.connect(self._on_camera_changed)
        self._style_combo(self.cam_combo)

        # Preset selector
        pre_lbl = QLabel("Preset:")
        pre_lbl.setStyleSheet("color:#ccc; font-size:12px;")
        self.preset_combo = QComboBox()
        self.preset_combo.setFixedHeight(28)
        self.preset_combo.addItems(list(PRESETS.keys()))
        self.preset_combo.currentTextChanged.connect(self._apply_preset)
        self._style_combo(self.preset_combo)

        layout.addWidget(cam_lbl)
        layout.addWidget(self.cam_combo)
        layout.addSpacing(16)
        layout.addWidget(pre_lbl)
        layout.addWidget(self.preset_combo)
        layout.addStretch()

        # Mode toggles
        self.ocr_btn = self._toggle_button("OCR Mode [O]", self._toggle_ocr, "#1a3a72")

        # Smart Board toggle — only visible when OCR mode is on
        self.smartboard_btn = self._toggle_button("Smart Board [B]", self._toggle_smartboard, "#1a5a1a")
        self.smartboard_btn.setVisible(False)

        self.hide_ctrl_btn = self._toggle_button("Hide Controls [H]", self._toggle_controls, "#2a2a2a")
        fs_btn = QPushButton("Fullscreen [F]")
        fs_btn.setFixedHeight(28)
        fs_btn.clicked.connect(self._toggle_fullscreen)
        self._style_btn(fs_btn, "#2a2a2a")

        layout.addWidget(self.ocr_btn)
        layout.addWidget(self.smartboard_btn)
        layout.addWidget(self.hide_ctrl_btn)
        layout.addWidget(fs_btn)
        return bar

    def _build_ocr_panel(self):
        panel = QWidget()
        panel.setMinimumWidth(280)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(4, 0, 0, 0)
        hdr = QLabel("OCR — Detected Text")
        hdr.setStyleSheet("color:#aaa; font-size:11px; padding-bottom:2px;")
        self.ocr_text = QTextEdit()
        self.ocr_text.setReadOnly(True)
        self.ocr_text.setStyleSheet(
            "background:#111; color:#fff; font-size:22px; font-family:Arial;"
            "border:1px solid #333; border-radius:4px; padding:6px;"
        )
        layout.addWidget(hdr)
        layout.addWidget(self.ocr_text)
        return panel

    def _build_controls(self):
        container = QWidget()
        container.setFixedHeight(195)
        container.setStyleSheet("background:#181818; border-top:1px solid #2a2a2a;")
        layout = QHBoxLayout(container)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(12)

        layout.addWidget(self._build_zoom_group(), stretch=2)
        layout.addWidget(self._build_adjust_group(), stretch=3)
        layout.addWidget(self._build_action_group(), stretch=2)
        return container

    def _build_zoom_group(self):
        g = self._group("Zoom  &  Pan")
        v = QVBoxLayout(g)
        v.setSpacing(6)

        # Zoom row
        zrow = QHBoxLayout()
        self.zoom_slider = QSlider(Qt.Orientation.Horizontal)
        self.zoom_slider.setRange(10, 100)  # × 0.1 = 1.0–10.0
        self.zoom_slider.setValue(10)
        self._style_slider(self.zoom_slider)
        self.zoom_slider.valueChanged.connect(self._on_zoom_slider)

        self.zoom_lbl = QLabel("1.0×")
        self.zoom_lbl.setStyleSheet("color:#fff; min-width:38px; font-size:13px;")
        self.zoom_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)

        zm = self._sq_btn("−", self._zoom_out)
        zp = self._sq_btn("+", self._zoom_in)
        zrow.addWidget(zm)
        zrow.addWidget(self.zoom_slider)
        zrow.addWidget(zp)
        zrow.addWidget(self.zoom_lbl)
        v.addLayout(zrow)

        # D-pad for pan
        dpad = QHBoxLayout()
        dpad.addStretch()
        col = QVBoxLayout()
        col.setSpacing(2)
        col.addWidget(self._sq_btn("▲", lambda: self._pan_key(0, -1)), alignment=Qt.AlignmentFlag.AlignCenter)
        mid_row = QHBoxLayout()
        mid_row.setSpacing(2)
        mid_row.addWidget(self._sq_btn("◀", lambda: self._pan_key(-1, 0)))
        mid_row.addWidget(self._sq_btn("⊙", self.proc.reset_pan))
        mid_row.addWidget(self._sq_btn("▶", lambda: self._pan_key(1, 0)))
        col.addLayout(mid_row)
        col.addWidget(self._sq_btn("▼", lambda: self._pan_key(0, 1)), alignment=Qt.AlignmentFlag.AlignCenter)
        dpad.addLayout(col)
        dpad.addStretch()
        v.addLayout(dpad)

        hint = QLabel("Drag image to pan  •  Scroll to zoom")
        hint.setStyleSheet("color:#555; font-size:10px;")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(hint)
        return g

    def _build_adjust_group(self):
        g = self._group("Image Adjustments")
        v = QVBoxLayout(g)
        v.setSpacing(4)

        self.sl_brightness = self._labeled_slider(v, "Brightness", -100, 100, 0,  self._on_brightness)
        self.sl_contrast   = self._labeled_slider(v, "Contrast",     50, 300, 100, self._on_contrast)
        self.sl_saturation = self._labeled_slider(v, "Saturation",    0, 200, 100, self._on_saturation)
        self.sl_sharpness  = self._labeled_slider(v, "Sharpness",     0,  10,   0, self._on_sharpness)
        return g

    def _build_action_group(self):
        g = self._group("Actions")
        v = QVBoxLayout(g)
        v.setSpacing(6)

        self.freeze_btn = QPushButton("❚❚  Freeze  [Space]")
        self.freeze_btn.setFixedHeight(46)
        self.freeze_btn.setCheckable(True)
        self.freeze_btn.clicked.connect(self._toggle_freeze)
        self._style_btn(self.freeze_btn, "#145a22", font_size=14)

        self.capture_btn = QPushButton("📷  Capture  [⌘ S]")
        self.capture_btn.setFixedHeight(46)
        self.capture_btn.clicked.connect(self._capture)
        self._style_btn(self.capture_btn, "#12345e", font_size=14)

        row2 = QHBoxLayout()
        self.invert_btn = QPushButton("Invert [I]")
        self.invert_btn.setFixedHeight(34)
        self.invert_btn.setCheckable(True)
        self.invert_btn.clicked.connect(self._toggle_invert)
        self._style_btn(self.invert_btn, "#3a1a5e")

        reset_btn = QPushButton("Reset [R]")
        reset_btn.setFixedHeight(34)
        reset_btn.clicked.connect(self._reset_all)
        self._style_btn(reset_btn, "#2a2a2a")
        row2.addWidget(self.invert_btn)
        row2.addWidget(reset_btn)

        v.addWidget(self.freeze_btn)
        v.addWidget(self.capture_btn)
        v.addLayout(row2)
        return g

    # ------------------------------------------------------------------
    # Shortcuts
    # ------------------------------------------------------------------

    def _build_shortcuts(self):
        def sc(key, fn):
            QShortcut(QKeySequence(key), self).activated.connect(fn)

        sc(Qt.Key.Key_Plus,  self._zoom_in)
        sc(Qt.Key.Key_Equal, self._zoom_in)
        sc(Qt.Key.Key_Minus, self._zoom_out)
        sc(Qt.Key.Key_Space, self._toggle_freeze)
        sc(Qt.Key.Key_I,     self._toggle_invert)
        sc(Qt.Key.Key_R,     self._reset_all)
        sc(Qt.Key.Key_F,     self._toggle_fullscreen)
        sc(Qt.Key.Key_O,     self._toggle_ocr)
        sc(Qt.Key.Key_B,     self._toggle_smartboard)
        sc(Qt.Key.Key_H,     self._toggle_controls)
        sc(Qt.Key.Key_Up,    lambda: self._pan_key(0, -1))
        sc(Qt.Key.Key_Down,  lambda: self._pan_key(0,  1))
        sc(Qt.Key.Key_Left,  lambda: self._pan_key(-1, 0))
        sc(Qt.Key.Key_Right, lambda: self._pan_key( 1, 0))
        sc("Ctrl+S",         self._capture)

        # Scroll wheel zoom
        self.video.wheelEvent = self._on_wheel

    def _on_wheel(self, event):
        delta = event.angleDelta().y()
        if delta > 0:
            self._zoom_in()
        elif delta < 0:
            self._zoom_out()

    # ------------------------------------------------------------------
    # Camera management
    # ------------------------------------------------------------------

    def _get_camera_names(self) -> list[str]:
        """Ask macOS for the real names of connected cameras."""
        try:
            result = subprocess.run(
                ["system_profiler", "SPCameraDataType"],
                capture_output=True, text=True, timeout=5
            )
            names = []
            for line in result.stdout.splitlines():
                s = line.strip()
                # Lines like "4K Veoαcam:" are camera names
                if (s.endswith(":")
                        and s not in ("Camera:", "Cameras:")
                        and not s.startswith("Model")
                        and not s.startswith("Unique")):
                    names.append(s.rstrip(":"))
            return names
        except Exception:
            return []

    def _discover_cameras(self):
        self.cam_combo.blockSignals(True)
        self.cam_combo.clear()

        # Use macOS system_profiler — it knows exactly what cameras are
        # connected without us needing to open/probe each one with OpenCV
        names = self._get_camera_names()

        if not names:
            names = ["Camera 0"]   # fallback if system_profiler fails

        for i, name in enumerate(names):
            self.cam_combo.addItem(name, i)

        self.cam_combo.blockSignals(False)

        # Auto-select the 4K / external camera if present
        preferred = 0
        for j, name in enumerate(names):
            if any(k in name.lower() for k in ["4k", "veo", "waft", "external", "usb"]):
                preferred = j
                break

        self.cam_combo.setCurrentIndex(preferred)
        self._connect_camera(preferred)

    def _connect_camera(self, index: int):
        if self.camera_thread:
            self.camera_thread.stop()
        self.camera_thread = CameraThread(index)
        self.camera_thread.frame_ready.connect(self._on_frame)
        self.camera_thread.error.connect(self._set_status)
        self.camera_thread.start()

    def _on_camera_changed(self, idx):
        data = self.cam_combo.itemData(idx)
        if data is not None:
            self._connect_camera(data)

    # ------------------------------------------------------------------
    # Frame processing & display
    # ------------------------------------------------------------------

    def _on_frame(self, frame: np.ndarray):
        if self.is_frozen:
            return
        self.current_frame = frame
        self._display(self.proc.process_live(frame))

    def _display(self, frame: np.ndarray):
        if frame is None:
            return
        dw, dh = self.video.width(), self.video.height()
        fh, fw = frame.shape[:2]
        scale = min(dw / fw, dh / fh)
        nw, nh = max(1, int(fw * scale)), max(1, int(fh * scale))
        out = cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_LANCZOS4)
        img = QImage(out.data, nw, nh, nw * 3, QImage.Format.Format_RGB888)
        self.video.setPixmap(QPixmap.fromImage(img))

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _toggle_freeze(self):
        self.is_frozen = not self.is_frozen
        self.freeze_btn.setChecked(self.is_frozen)

        if self.is_frozen:
            self.freeze_btn.setText("▶  Resume  [Space]")
            self.freeze_btn.setStyleSheet(self.freeze_btn.styleSheet().replace("#145a22", "#7a1414"))
            if self.current_frame is not None:
                # Always store the raw full-res frame for Smart Board processing
                self.frozen_raw = self.current_frame.copy()
                self.smartboard_raw = None   # will be generated by OCR
                self._update_frozen_display()
                if self.ocr_mode and OCR_AVAILABLE:
                    self._set_status("FROZEN — reading text…")
                    self._run_ocr(self.frozen_raw)
            self._set_status("FROZEN — press Space to resume  |  Cmd+S to save")
        else:
            self.freeze_btn.setText("❚❚  Freeze  [Space]")
            self.freeze_btn.setStyleSheet(self.freeze_btn.styleSheet().replace("#7a1414", "#145a22"))
            self.frozen_raw = None
            self.smartboard_raw = None
            self._set_status("Live")

    def _update_frozen_display(self):
        """Refresh the video display from the current frozen frame (original or smart board)."""
        frame = (self.smartboard_raw
                 if (self.show_smartboard and self.smartboard_raw is not None)
                 else self.frozen_raw)
        if frame is None:
            return
        dw, dh = self.video.width(), self.video.height()
        enhanced = self.proc.process_freeze(frame, (dw, dh))
        self._display_raw(enhanced)

    def _display_raw(self, frame: np.ndarray):
        """Display a frame that is already sized for the display area."""
        if frame is None:
            return
        h, w = frame.shape[:2]
        img = QImage(frame.data, w, h, w * 3, QImage.Format.Format_RGB888)
        self.video.setPixmap(QPixmap.fromImage(img))

    def _capture(self):
        if self.current_frame is None:
            return
        frame = self.proc.process_live(self.current_frame)
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(SAVE_DIR, f"capture_{ts}.png")
        bgr = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
        cv2.imwrite(path, bgr)
        self._set_status(f"Saved → {path}")

    def _run_ocr(self, frame: np.ndarray):
        self.ocr_text.setPlainText("Reading text…")
        if VISION_AVAILABLE:
            self._run_ocr_vision(frame)
        elif TESSERACT_AVAILABLE:
            self._run_ocr_tesseract(frame)
        else:
            self.ocr_text.setPlainText("OCR not available")

    def _run_ocr_vision(self, frame: np.ndarray):
        """Apple Vision framework — excellent handwriting recognition, works offline.
        Also generates the Smart Board overlay image with bounding-box-positioned text."""
        try:
            tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
            tmp_path = tmp.name
            tmp.close()
            PILImage.fromarray(frame).save(tmp_path)

            url = NSURL.fileURLWithPath_(tmp_path)

            # Each entry: (text_string, norm_x, norm_y, norm_w, norm_h)
            # Vision coords: origin bottom-left, normalised 0–1
            observations = []

            def completion(request, error):
                if error:
                    return
                for obs in request.results():
                    candidates = obs.topCandidates_(1)
                    if not candidates:
                        continue
                    text = str(candidates[0].string())
                    bb = obs.boundingBox()   # CGRect with origin bottom-left
                    observations.append((
                        text,
                        float(bb.origin.x),
                        float(bb.origin.y),
                        float(bb.size.width),
                        float(bb.size.height),
                    ))

            req = Vision.VNRecognizeTextRequest.alloc().initWithCompletionHandler_(completion)
            req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
            req.setUsesLanguageCorrection_(True)

            handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(url, {})
            handler.performRequests_error_([req], None)

            os.unlink(tmp_path)

            # Update the side-panel text
            lines = [o[0] for o in observations]
            self.ocr_text.setPlainText("\n".join(lines) if lines else "(No text detected)")

            # Generate the Smart Board overlay on the RAW frame
            if observations:
                self.smartboard_raw = self._generate_smartboard(frame, observations)
            else:
                self.smartboard_raw = frame.copy()

            # If smart board view is already active, refresh the display
            if self.show_smartboard and self.is_frozen:
                self._update_frozen_display()

            self._set_status("FROZEN — press Space to resume  |  Cmd+S to save"
                             + ("  |  [B] Smart Board ready" if observations else ""))

        except Exception as exc:
            self.ocr_text.setPlainText(f"Vision OCR error: {exc}")

    def _generate_smartboard(self, frame: np.ndarray,
                              observations: list) -> np.ndarray:
        """Return a copy of frame with handwriting replaced by clean digital text
        at the exact same positions — preserving the spatial layout of the board."""
        img = PILImage.fromarray(frame.copy())
        h, w = frame.shape[:2]

        # Find a clean font
        font_path = next((fp for fp in _FONT_PATHS if os.path.exists(fp)), None)

        draw = ImageDraw.Draw(img)

        for text, nx, ny, nw, nh in observations:
            # Convert Vision normalised coords (bottom-left origin) → PIL pixels (top-left)
            px = int(nx * w)
            py = int((1.0 - ny - nh) * h)
            pw = int(nw * w)
            ph = int(nh * h)

            if pw < 4 or ph < 4:
                continue

            # Padding so the white box covers any surrounding ink
            pad_x = max(4, int(pw * 0.04))
            pad_y = max(4, int(ph * 0.15))
            bx0 = max(0, px - pad_x)
            by0 = max(0, py - pad_y)
            bx1 = min(w, px + pw + pad_x)
            by1 = min(h, py + ph + pad_y)

            # White background to cover the handwriting
            draw.rectangle([bx0, by0, bx1, by1], fill=(255, 255, 255))

            # Font sized to match the detected line height
            font_size = max(14, int(ph * 0.80))
            font = None
            if font_path:
                try:
                    font = ImageFont.truetype(font_path, font_size)
                except Exception:
                    font = None
            if font is None:
                font = ImageFont.load_default()

            # Shrink font if text is too wide for the box
            try:
                bbox = draw.textbbox((0, 0), text, font=font)
                txt_w = bbox[2] - bbox[0]
                if txt_w > pw and txt_w > 0:
                    ratio = pw / txt_w * 0.92
                    new_size = max(10, int(font_size * ratio))
                    if font_path:
                        try:
                            font = ImageFont.truetype(font_path, new_size)
                        except Exception:
                            pass
            except Exception:
                pass

            draw.text((bx0 + pad_x, by0 + pad_y), text,
                      fill=(15, 15, 80), font=font)   # dark-blue ink on white

        return np.array(img)

    def _run_ocr_tesseract(self, frame: np.ndarray):
        """Tesseract fallback — works for printed text."""
        try:
            text = pytesseract.image_to_string(PILImage.fromarray(frame)).strip()
            self.ocr_text.setPlainText(text if text else "(No text detected)")
        except Exception as exc:
            self.ocr_text.setPlainText(f"OCR error: {exc}")

    # ------------------------------------------------------------------
    # Zoom / pan slots
    # ------------------------------------------------------------------

    def _zoom_in(self):
        self.proc.zoom_in()
        self._sync_zoom_ui()

    def _zoom_out(self):
        self.proc.zoom_out()
        self._sync_zoom_ui()

    def _on_zoom_slider(self, value: int):
        self.proc.zoom = value / 10.0
        self.zoom_lbl.setText(f"{self.proc.zoom:.1f}×")

    def _sync_zoom_ui(self):
        self.zoom_slider.blockSignals(True)
        self.zoom_slider.setValue(int(self.proc.zoom * 10))
        self.zoom_lbl.setText(f"{self.proc.zoom:.1f}×")
        self.zoom_slider.blockSignals(False)

    def _pan_key(self, dx: int, dy: int):
        self.proc.pan(dx, dy)

    def _on_drag_pan(self, dpx: int, dpy: int):
        self.proc.pan_pixels(-dpx, -dpy, self.video.width(), self.video.height())

    # ------------------------------------------------------------------
    # Adjustment slots
    # ------------------------------------------------------------------

    def _on_brightness(self, v): self.proc.brightness = v
    def _on_contrast(self, v):   self.proc.contrast   = v / 100.0
    def _on_saturation(self, v): self.proc.saturation = v / 100.0
    def _on_sharpness(self, v):  self.proc.sharpness  = v

    def _toggle_invert(self):
        self.proc.invert = not self.proc.invert
        self.invert_btn.setChecked(self.proc.invert)

    def _toggle_smartboard(self):
        """Switch between Original and Smart Board view on a frozen frame."""
        self.show_smartboard = not self.show_smartboard
        self.smartboard_btn.setChecked(self.show_smartboard)
        self.smartboard_btn.setText(
            "Original [B]" if self.show_smartboard else "Smart Board [B]"
        )
        # Hide the side text panel when Smart Board fills the image
        self.ocr_panel.setVisible(not self.show_smartboard and self.ocr_mode)
        if self.is_frozen:
            if self.show_smartboard and self.smartboard_raw is None:
                self._set_status("Freeze a frame first to generate Smart Board")
            else:
                self._update_frozen_display()

    def _toggle_ocr(self):
        if not OCR_AVAILABLE:
            self._set_status("OCR unavailable — install dependencies")
            return
        self.ocr_mode = not self.ocr_mode
        self.ocr_btn.setChecked(self.ocr_mode)
        self.ocr_btn.setText(f"OCR {'ON' if self.ocr_mode else 'OFF'}  [O]")
        # Smart Board button only makes sense when OCR is on
        self.smartboard_btn.setVisible(self.ocr_mode)
        if self.ocr_mode:
            if not self.show_smartboard:
                self.ocr_panel.show()
        else:
            self.ocr_panel.hide()
            if self.show_smartboard:
                self.show_smartboard = False
                self.smartboard_btn.setChecked(False)
                self.smartboard_btn.setText("Smart Board [B]")
                if self.is_frozen:
                    self._update_frozen_display()

    def _toggle_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def _toggle_controls(self):
        self._controls_visible = not self._controls_visible
        self.controls_widget.setVisible(self._controls_visible)
        self.hide_ctrl_btn.setChecked(not self._controls_visible)
        self.hide_ctrl_btn.setText(
            ("Show" if not self._controls_visible else "Hide") + " Controls [H]"
        )

    def _apply_preset(self, name: str):
        if name not in PRESETS:
            return
        p = PRESETS[name]
        self.proc.brightness = p["brightness"]
        self.proc.contrast   = p["contrast"]
        self.proc.saturation = p["saturation"]
        self.proc.sharpness  = p["sharpness"]
        self.proc.invert     = p["invert"]
        # Sync sliders
        self.sl_brightness.setValue(p["brightness"])
        self.sl_contrast.setValue(int(p["contrast"] * 100))
        self.sl_saturation.setValue(int(p["saturation"] * 100))
        self.sl_sharpness.setValue(p["sharpness"])
        self.invert_btn.setChecked(p["invert"])
        self.proc.invert = p["invert"]

    def _reset_all(self):
        self.proc.reset_all()
        self.preset_combo.setCurrentText("Default")
        self._apply_preset("Default")
        self.zoom_slider.setValue(10)
        self._set_status("Reset to defaults")

    # ------------------------------------------------------------------
    # Helpers — widget factories
    # ------------------------------------------------------------------

    def _group(self, title: str) -> QGroupBox:
        g = QGroupBox(title)
        g.setStyleSheet("""
            QGroupBox {
                color:#999; border:1px solid #2a2a2a; border-radius:5px;
                margin-top:10px; padding-top:4px; font-size:11px;
            }
            QGroupBox::title { subcontrol-origin:margin; left:8px; }
        """)
        return g

    def _sq_btn(self, text: str, fn) -> QPushButton:
        b = QPushButton(text)
        b.setFixedSize(34, 34)
        b.clicked.connect(fn)
        b.setStyleSheet("""
            QPushButton { background:#2c2c2c; color:#fff; border:1px solid #444;
                          border-radius:5px; font-size:15px; }
            QPushButton:hover { background:#3a3a3a; }
            QPushButton:pressed { background:#1a1a1a; }
        """)
        return b

    def _style_btn(self, btn: QPushButton, bg: str = "#2a2a2a", font_size: int = 12):
        btn.setStyleSheet(f"""
            QPushButton {{
                background:{bg}; color:#fff; border:1px solid #444;
                border-radius:5px; font-size:{font_size}px; padding:4px;
            }}
            QPushButton:hover {{ filter:brightness(1.2); background:{bg}dd; }}
            QPushButton:checked {{ border:2px solid #fff; }}
            QPushButton:pressed {{ background:#111; }}
        """)

    def _toggle_button(self, label: str, fn, bg: str = "#2a2a2a") -> QPushButton:
        btn = QPushButton(label)
        btn.setFixedHeight(28)
        btn.setCheckable(True)
        btn.clicked.connect(fn)
        self._style_btn(btn, bg)
        return btn

    def _style_combo(self, combo: QComboBox):
        combo.setStyleSheet("""
            QComboBox { background:#222; color:#eee; border:1px solid #444;
                        border-radius:4px; padding:3px 8px; font-size:12px; }
            QComboBox::drop-down { border:none; }
            QComboBox QAbstractItemView { background:#222; color:#eee; }
        """)

    def _style_slider(self, sl: QSlider):
        sl.setStyleSheet("""
            QSlider::groove:horizontal { height:4px; background:#2a2a2a; border-radius:2px; }
            QSlider::sub-page:horizontal { background:#4a6fa5; border-radius:2px; }
            QSlider::handle:horizontal {
                width:16px; height:16px; margin:-6px 0;
                background:#888; border-radius:8px;
            }
            QSlider::handle:horizontal:hover { background:#aaa; }
        """)

    def _labeled_slider(self, layout: QVBoxLayout, label: str,
                         min_v: int, max_v: int, default: int, fn) -> QSlider:
        row = QHBoxLayout()
        lbl = QLabel(f"{label}:")
        lbl.setStyleSheet("color:#888; font-size:11px;")
        lbl.setFixedWidth(70)

        sl = QSlider(Qt.Orientation.Horizontal)
        sl.setRange(min_v, max_v)
        sl.setValue(default)
        self._style_slider(sl)

        val_lbl = QLabel(str(default))
        val_lbl.setStyleSheet("color:#ccc; font-size:11px;")
        val_lbl.setFixedWidth(28)
        val_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        def on_change(v):
            val_lbl.setText(str(v))
            fn(v)

        sl.valueChanged.connect(on_change)
        row.addWidget(lbl)
        row.addWidget(sl)
        row.addWidget(val_lbl)
        layout.addLayout(row)
        return sl

    # ------------------------------------------------------------------
    # Theme & misc
    # ------------------------------------------------------------------

    def _apply_theme(self):
        self.setStyleSheet("""
            QMainWindow, QWidget { background:#111; color:#eee; }
        """)

    def _set_status(self, msg: str):
        self.statusbar.showMessage(msg)

    def closeEvent(self, event):
        if self.camera_thread:
            self.camera_thread.stop()
        event.accept()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Lumen")

    window = MainWindow()

    # Centre the window on screen
    screen = app.primaryScreen().geometry()
    w, h = 1100, 750
    window.setGeometry(
        (screen.width() - w) // 2,
        (screen.height() - h) // 2,
        w, h
    )

    window.show()
    window.raise_()
    window.activateWindow()

    # macOS: bring Python window to front after event loop starts
    from PyQt6.QtCore import QTimer
    QTimer.singleShot(300, lambda: (
        window.raise_(),
        window.activateWindow(),
        os.system("osascript -e 'tell application \"Python\" to activate' &")
    ))

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
