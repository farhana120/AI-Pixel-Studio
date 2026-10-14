import sys
from pathlib import Path

import cv2
import numpy as np
import matplotlib.pyplot as plt
from PIL import Image, ImageEnhance, ImageFilter

from PySide6.QtCore import Qt, QRect, QSize
from PySide6.QtGui import QPixmap, QTransform, QImage, QPainter, QColor, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QLabel, QPushButton,
    QFileDialog, QVBoxLayout, QHBoxLayout, QSlider, QMessageBox,
    QInputDialog, QScrollArea, QFrame, QRubberBand, QComboBox
)


APP_TITLE = "AI Pixel Studio"


class ImageCanvas(QLabel):
    """Preview label with mouse-drag crop selection."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.owner = parent
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(180, 220)
        self.setMouseTracking(True)
        self.crop_mode = False
        self.rubber_band = QRubberBand(QRubberBand.Shape.Rectangle, self)
        self.drag_start = None
        self.crop_rect = None
        self.setStyleSheet("background:#0d1728; border:1px solid #263449; border-radius:8px; color:#8293ad; font-size:14px;")

    def set_crop_mode(self, enabled):
        self.crop_mode = enabled
        self.crop_rect = None
        self.rubber_band.hide()
        self.setCursor(
            Qt.CursorShape.CrossCursor if enabled else Qt.CursorShape.ArrowCursor
        )

    def mousePressEvent(self, event):
        if self.crop_mode and event.button() == Qt.MouseButton.LeftButton:
            self.drag_start = event.position().toPoint()
            self.rubber_band.setGeometry(QRect(self.drag_start, QSize()))
            self.rubber_band.show()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.crop_mode and self.drag_start is not None:
            rect = QRect(self.drag_start, event.position().toPoint()).normalized()
            self.rubber_band.setGeometry(rect)
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self.crop_mode and event.button() == Qt.MouseButton.LeftButton and self.drag_start is not None:
            rect = QRect(self.drag_start, event.position().toPoint()).normalized()
            self.drag_start = None
            self.rubber_band.hide()
            if rect.width() >= 8 and rect.height() >= 8:
                self.crop_rect = rect
                if self.owner is not None:
                    self.owner.apply_mouse_crop(self, rect)
            self.set_crop_mode(False)
            return
        super().mouseReleaseEvent(event)


class HistogramWidget(QWidget):
    """Compact RGB histogram drawn directly in the right properties panel."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(78)
        self.setMaximumHeight(108)
        self.setSizePolicy(self.sizePolicy().horizontalPolicy(), self.sizePolicy().verticalPolicy())
        self.set_data(None)

    def set_data(self, pixmap):
        self.channels = None
        if pixmap is not None and not pixmap.isNull():
            image = pixmap.toImage().convertToFormat(QImage.Format.Format_RGBA8888)
            raw = np.frombuffer(bytes(image.bits()), dtype=np.uint8).reshape(image.height(), image.width(), 4)
            # Sample large images to keep the interface responsive.
            sample = raw[::max(1, image.height() // 500), ::max(1, image.width() // 500), :3]
            self.channels = [np.histogram(sample[:, :, i], bins=48, range=(0, 256))[0] for i in range(3)]
            peak = max(int(channel.max()) for channel in self.channels) or 1
            self.channels = [channel / peak for channel in self.channels]
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor('#0d1728'))
        painter.setPen(QColor('#263449'))
        painter.drawRoundedRect(self.rect().adjusted(0, 0, -1, -1), 6, 6)
        if self.channels is None:
            painter.setPen(QColor('#8293ad'))
            painter.drawText(
                self.rect().adjusted(8, 6, -8, -6),
                Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
                'Open an image\nto view RGB histogram'
            )
            return
        colors = [QColor('#f87171'), QColor('#4ade80'), QColor('#60a5fa')]
        area = self.rect().adjusted(8, 8, -8, -8)
        # Subtle guides make the chart easier to read at compact sidebar widths.
        painter.setPen(QColor('#1c2a40'))
        for fraction in (0.25, 0.5, 0.75):
            y = int(area.top() + area.height() * fraction)
            painter.drawLine(area.left(), y, area.right(), y)
        for values, color in zip(self.channels, colors):
            painter.setPen(color)
            points = []
            for i, value in enumerate(values):
                x = area.left() + i * area.width() / (len(values) - 1)
                y = area.bottom() - float(value) * (area.height() - 5)
                points.append((int(x), int(y)))
            for a, b in zip(points, points[1:]):
                painter.drawLine(a[0], a[1], b[0], b[1])


class AIPixelStudio(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.resize(1440, 860)
        self.setMinimumSize(900, 580)

        self.original = None
        self.current = None
        self.file_path = None
        self.undo_stack = []
        self.redo_stack = []
        self._slider_before = None
        self._last_adjustment_values = (0, 0, 0)
        self._caption_model = None
        self._caption_processor = None
        self._rembg_session = None
        self._comparison_active = False

        self.setStyleSheet("""
            QMainWindow, QWidget {
                background: #0b1120; color: #e5eaf3;
                font-family: 'Segoe UI'; font-size: 12px;
            }
            QToolTip { background: #172238; color: #f8fafc; border: 1px solid #3b82f6; padding: 5px; }
            QLabel#heading { font-size: 25px; font-weight: 700; color: #f8fafc; padding: 4px 2px; }
            QLabel#sectionTitle { color: #93c5fd; font-size: 10px; font-weight: 700; letter-spacing: 1px; padding: 9px 2px 4px 2px; border-bottom: 1px solid #263449; }
            QLabel#previewTitle { font-size: 11px; font-weight: 700; color: #bfdbfe; background: #172238; border: 1px solid #293750; border-radius: 7px; padding: 8px; }
            QLabel#infoValue { color: #cbd5e1; background: #111b2d; border: 1px solid #263449; border-radius: 8px; padding: 10px; }
            QPushButton {
                background: #1b2940; color: #eaf1fb; border: 1px solid #30415b;
                padding: 8px 12px; border-radius: 7px; min-height: 20px;
            }
            QPushButton:hover { background: #263c5c; border-color: #4b8df8; }
            QPushButton:pressed { background: #1d4ed8; }
            QPushButton:disabled { color: #718096; background: #131c2b; border-color: #202b3c; }
            QPushButton#primaryButton { background: #2563eb; border-color: #3b82f6; font-weight: 600; }
            QPushButton#primaryButton:hover { background: #1d4ed8; }
            QFrame#sidePanel { background: #101a2b; border: 1px solid #263449; border-radius: 10px; }
            QFrame#previewPanel { background: #131e30; border: 1px solid #2b3a50; border-radius: 10px; }
            QScrollArea { border: none; background: transparent; }
            QScrollBar:vertical { background: #101a2b; width: 10px; margin: 2px; border-radius: 5px; }
            QScrollBar::handle:vertical { background: #35445c; min-height: 28px; border-radius: 5px; }
            QScrollBar::handle:vertical:hover { background: #4b8df8; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }
            QSlider::groove:horizontal { height: 5px; background: #344155; border-radius: 3px; }
            QSlider::handle:horizontal { background: #60a5fa; border: 2px solid #0b1120; width: 14px; height: 14px; margin: -6px 0; border-radius: 8px; }
            QComboBox { background: #1b2940; padding: 7px; border: 1px solid #30415b; border-radius: 7px; }
            QStatusBar { background: #0a1020; color: #94a3b8; border-top: 1px solid #25334a; }
        """)

        root = QWidget()
        self.setCentralWidget(root)
        main = QVBoxLayout(root)
        main.setContentsMargins(12, 10, 12, 8)
        main.setSpacing(9)
        self.main_layout = main

        # Top toolbar
        toolbar = QHBoxLayout()
        toolbar.setSpacing(6)
        self.toolbar_buttons = []
        heading = QLabel(APP_TITLE)
        heading.setObjectName("heading")
        toolbar.addWidget(heading)
        toolbar.addStretch()
        for text, callback in [
            ("＋  Open", self.open_image), ("Save", self.save_image),
            ("Export", self.save_image), ("↶  Undo", self.undo),
            ("↷  Redo", self.redo), ("Reset", self.reset),
        ]:
            button = QPushButton(text)
            if text.startswith("＋") or text == "Export":
                button.setObjectName("primaryButton")
            button.clicked.connect(callback)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            self.toolbar_buttons.append((text, button))
            button.setAccessibleName(text.replace("＋", "").strip())
            toolbar.addWidget(button)
        self.compare_button = QPushButton("Before / After")
        self.compare_button.setToolTip("Temporarily show the original image in the processed preview")
        self.compare_button.clicked.connect(self.toggle_comparison)
        toolbar.addWidget(self.compare_button)
        main.addLayout(toolbar)

        # Standard keyboard shortcuts for common desktop-editor actions.
        self._shortcuts = []
        for sequence, callback in [("Ctrl+O", self.open_image), ("Ctrl+S", self.save_image),
                                   ("Ctrl+Z", self.undo), ("Ctrl+Y", self.redo),
                                   ("Ctrl+Shift+S", self.save_image), ("Ctrl+0", self.fit_to_window)]:
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.activated.connect(callback)
            self._shortcuts.append(shortcut)

        workspace = QHBoxLayout()
        workspace.setSpacing(9)
        self.workspace_layout = workspace

        # Left tools
        left_content = QWidget()
        left_content.setStyleSheet("background: transparent;")
        left = QVBoxLayout(left_content)
        left.setContentsMargins(8, 8, 8, 8)
        left.setSpacing(7)
        tools_heading = QLabel("EDITING TOOLS")
        tools_heading.setObjectName("sectionTitle")
        left.addWidget(tools_heading)
        tool_groups = [
            ("CROP & TRANSFORM", [
                ("Crop: drag on preview", self.start_crop_mode),
                ("Crop by dimensions", self.crop_by_dimensions),
                ("Rotate 90°", self.rotate),
                ("Flip Horizontal", self.flip),
                ("Resize", self.resize_image),
            ]),
            ("FILTERS", [
                ("Grayscale", lambda: self.apply_filter("gray")),
                ("Gaussian Blur", lambda: self.apply_filter("blur")),
                ("Median Filter", lambda: self.apply_filter("median")),
                ("Sharpen", lambda: self.apply_filter("sharpen")),
                ("Noise Reduction", lambda: self.apply_filter("denoise")),
                ("Histogram Equalization", lambda: self.apply_filter("equalize")),
            ]),
            ("EDGE & ANALYSIS", [
                ("Sobel Edge Detection", lambda: self.apply_filter("sobel")),
                ("Canny Edge Detection", lambda: self.apply_filter("canny")),
                ("Thresholding", lambda: self.apply_filter("threshold")),
                ("View RGB Histogram", self.histogram),
                ("Image Enhancement", self.enhance_image),
            ]),
        ]
        for group_title, group_tools in tool_groups:
            group_label = QLabel(group_title)
            group_label.setObjectName("sectionTitle")
            left.addWidget(group_label)
            for label, callback in group_tools:
                btn = QPushButton(label)
                btn.setMinimumHeight(34)
                btn.setToolTip(label)
                btn.setCursor(Qt.CursorShape.PointingHandCursor)
                btn.clicked.connect(callback)
                left.addWidget(btn)
        left.addStretch()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(left_content)
        scroll.setMinimumWidth(180)
        scroll.setMaximumWidth(235)
        self.tools_scroll = scroll
        workspace.addWidget(scroll)


        # Original/processed preview
        previews = QWidget()
        preview_layout = QHBoxLayout(previews)
        preview_layout.setContentsMargins(0, 0, 0, 0)
        preview_layout.setSpacing(9)
        self.preview_widget = previews

        original_frame, original_layout = self.make_preview_panel("ORIGINAL IMAGE")
        self.original_canvas = original_layout.itemAt(1).widget()
        processed_frame, processed_layout = self.make_preview_panel("PROCESSED IMAGE")
        self.processed_canvas = processed_layout.itemAt(1).widget()
        self.processed_canvas.owner = self
        preview_layout.addWidget(original_frame, 1)
        preview_layout.addWidget(processed_frame, 1)
        workspace.addWidget(previews, 1)

        # Right properties
        right = QFrame()
        right.setObjectName("sidePanel")
        right.setMinimumWidth(190)
        right.setMaximumWidth(230)
        self.properties_panel = right
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(10, 10, 10, 10)
        right_layout.setSpacing(6)
        adjustments_heading = QLabel("ADJUSTMENTS")
        adjustments_heading.setObjectName("sectionTitle")
        right_layout.addWidget(adjustments_heading)
        self.brightness = self.add_slider(right_layout, "Brightness")
        self.contrast = self.add_slider(right_layout, "Contrast")
        self.sharpness = self.add_slider(right_layout, "Sharpness")
        for slider in (self.brightness, self.contrast, self.sharpness):
            slider.valueChanged.connect(self.adjust_image)
            slider.sliderPressed.connect(self.begin_slider_edit)
            slider.sliderReleased.connect(self.finish_slider_edit)

        right_layout.addSpacing(7)
        zoom_heading = QLabel("ZOOM & VIEW")
        zoom_heading.setObjectName("sectionTitle")
        right_layout.addWidget(zoom_heading)
        zoom_row = QHBoxLayout()
        self.zoom_label = QLabel("Fit")
        zoom_out = QPushButton("−")
        zoom_in = QPushButton("+")
        fit_btn = QPushButton("Fit")
        zoom_out.clicked.connect(lambda: self.change_zoom(-10))
        zoom_in.clicked.connect(lambda: self.change_zoom(10))
        fit_btn.clicked.connect(self.fit_to_window)
        zoom_row.addWidget(zoom_out)
        zoom_row.addWidget(self.zoom_label)
        zoom_row.addWidget(zoom_in)
        zoom_row.addWidget(fit_btn)
        right_layout.addLayout(zoom_row)

        right_layout.addSpacing(7)
        info_heading = QLabel("IMAGE INFORMATION")
        info_heading.setObjectName("sectionTitle")
        right_layout.addWidget(info_heading)
        self.info_label = QLabel("No image loaded")
        self.info_label.setObjectName("infoValue")
        self.info_label.setWordWrap(True)
        right_layout.addWidget(self.info_label)

        histogram_heading = QLabel("RGB HISTOGRAM")
        histogram_heading.setObjectName("sectionTitle")
        right_layout.addWidget(histogram_heading)
        self.histogram_widget = HistogramWidget()
        right_layout.addWidget(self.histogram_widget)
        histogram_hint = QLabel("Red  ·  Green  ·  Blue")
        histogram_hint.setStyleSheet("color:#94a3b8; font-size:10px;")
        histogram_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        histogram_hint.setWordWrap(True)
        right_layout.addWidget(histogram_hint)

        # Retained internally for backward compatibility; AI caption UI is intentionally hidden for now.
        self.caption_label = QLabel("Caption will appear here.")
        self.caption_label.hide()
        right_layout.addStretch()
        workspace.addWidget(right)
        main.addLayout(workspace, 1)

        self.status = QLabel("Ready  ·  No image loaded")
        self.status.setStyleSheet("color:#94a3b8; padding:3px 2px;")
        main.addWidget(self.status)
        self.zoom_percent = 100

    def make_preview_panel(self, title):
        frame = QFrame()
        frame.setObjectName("previewPanel")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)
        title_label = QLabel(title)
        title_label.setObjectName("previewTitle")
        title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        if title == "PROCESSED IMAGE":
            self.processed_title_label = title_label
        canvas = ImageCanvas(self)
        layout.addWidget(title_label)
        layout.addWidget(canvas, 1)
        return frame, layout

    def add_slider(self, layout, title):
        header = QHBoxLayout()
        title_label = QLabel(title)
        value_label = QLabel("0")
        value_label.setMinimumWidth(34)
        value_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        value_label.setStyleSheet("color:#93c5fd; font-weight:600;")
        header.addWidget(title_label)
        header.addStretch()
        header.addWidget(value_label)
        layout.addLayout(header)

        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(-100, 100)
        slider.setValue(0)
        slider.value_label = value_label
        slider.valueChanged.connect(lambda value, label=value_label: label.setText(f"{value:+d}" if value else "0"))
        layout.addWidget(slider)
        return slider

    def open_image(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Image", "",
            "Images (*.png *.jpg *.jpeg *.bmp *.webp *.tif *.tiff)"
        )
        if not path:
            return
        pixmap = QPixmap(path)
        if pixmap.isNull():
            QMessageBox.warning(self, "Open Image", "This image could not be opened.")
            return
        self.file_path = path
        self._comparison_active = False
        self.compare_button.setText("Before / After")
        self.original = QPixmap(pixmap)
        self.current = QPixmap(pixmap)
        self.undo_stack.clear()
        self.redo_stack.clear()
        self._last_adjustment_values = (0, 0, 0)
        self.reset_sliders()
        self.caption_label.setText("Caption will appear here.")
        self.show_image()
        self.update_status("Image loaded")

    def show_image(self):
        self._show_on_canvas(self.original, self.original_canvas)
        preview_pixmap = self.original if self._comparison_active else self.current
        self._show_on_canvas(preview_pixmap, self.processed_canvas)
        if hasattr(self, "processed_title_label"):
            self.processed_title_label.setText("BEFORE / AFTER · ORIGINAL" if self._comparison_active else "PROCESSED IMAGE")
        if hasattr(self, "histogram_widget"):
            self.histogram_widget.set_data(self.current)
        if self.current is not None:
            fmt = Path(self.file_path).suffix.upper().replace(".", "") if self.file_path else "Image"
            self.info_label.setText(
                f"Size: {self.current.width()} × {self.current.height()} px\n"
                f"Format: {fmt}\nZoom: {self.zoom_percent}%"
            )
        else:
            self.info_label.setText("No image loaded")

    def _show_on_canvas(self, pixmap, canvas):
        if pixmap is None:
            canvas.setPixmap(QPixmap())
            canvas.setText("Open an image to get started")
            return
        canvas.setText("")
        target = canvas.size()
        if self.zoom_percent != 100:
            factor = self.zoom_percent / 100
            scaled = pixmap.scaled(
                max(1, int(target.width() * factor)),
                max(1, int(target.height() * factor)),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation
            )
        else:
            scaled = pixmap.scaled(
                target, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation
            )
        canvas.setPixmap(scaled)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Responsive workspace: compact sidebars on narrower windows while
        # preserving both image previews and all existing editing controls.
        width = self.width()
        if hasattr(self, "tools_scroll") and hasattr(self, "properties_panel"):
            if width < 1080:
                self.tools_scroll.setMaximumWidth(190)
                self.properties_panel.setMaximumWidth(195)
                self.workspace_layout.setSpacing(5)
                self.main_layout.setContentsMargins(7, 6, 7, 5)
                self.main_layout.setSpacing(6)
            elif width < 1320:
                self.tools_scroll.setMaximumWidth(210)
                self.properties_panel.setMaximumWidth(210)
                self.workspace_layout.setSpacing(7)
                self.main_layout.setContentsMargins(9, 8, 9, 6)
                self.main_layout.setSpacing(7)
            else:
                self.tools_scroll.setMaximumWidth(235)
                self.properties_panel.setMaximumWidth(230)
                self.workspace_layout.setSpacing(9)
                self.main_layout.setContentsMargins(12, 10, 12, 8)
                self.main_layout.setSpacing(9)
            if width < 1050:
                self.original_canvas.setMinimumSize(130, 170)
                self.processed_canvas.setMinimumSize(130, 170)
                self.histogram_widget.setMaximumHeight(88)
            else:
                self.original_canvas.setMinimumSize(180, 220)
                self.processed_canvas.setMinimumSize(180, 220)
                self.histogram_widget.setMaximumHeight(108)
        self.show_image()

    def toggle_comparison(self):
        if not self.require_image():
            return
        self._comparison_active = not self._comparison_active
        self.compare_button.setText("Show Processed" if self._comparison_active else "Before / After")
        self.show_image()
        self.update_status("Showing original for comparison" if self._comparison_active else "Showing processed image")

    def change_zoom(self, delta):
        self.zoom_percent = max(10, min(300, self.zoom_percent + delta))
        self.zoom_label.setText(f"{self.zoom_percent}%")
        self.show_image()

    def fit_to_window(self):
        self.zoom_percent = 100
        self.zoom_label.setText("Fit")
        self.show_image()

    # Undo/redo
    def save_history(self):
        self._comparison_active = False
        if hasattr(self, "compare_button"):
            self.compare_button.setText("Before / After")
        if self.current is not None:
            self.undo_stack.append(QPixmap(self.current))
            if len(self.undo_stack) > 25:
                self.undo_stack.pop(0)
            self.redo_stack.clear()

    def undo(self):
        if not self.undo_stack or self.current is None:
            self.status.setText("Nothing to undo")
            return
        self.redo_stack.append(QPixmap(self.current))
        self.current = self.undo_stack.pop()
        self.reset_sliders()
        self.show_image()
        self.update_status("Undo successful")

    def redo(self):
        if not self.redo_stack or self.current is None:
            self.status.setText("Nothing to redo")
            return
        self.undo_stack.append(QPixmap(self.current))
        self.current = self.redo_stack.pop()
        self.reset_sliders()
        self.show_image()
        self.update_status("Redo successful")

    # Conversions
    def pixmap_to_pil(self, pixmap):
        qimage = pixmap.toImage().convertToFormat(QImage.Format.Format_RGBA8888)
        width, height = qimage.width(), qimage.height()
        ptr = qimage.bits()
        data = bytes(ptr)
        return Image.frombytes("RGBA", (width, height), data)

    def pil_to_pixmap(self, image):
        image = image.convert("RGBA")
        raw = image.tobytes("raw", "RGBA")
        qimage = QImage(raw, image.width, image.height, image.width * 4,
                        QImage.Format.Format_RGBA8888).copy()
        return QPixmap.fromImage(qimage)

    def reset_sliders(self):
        for slider in (self.brightness, self.contrast, self.sharpness):
            slider.blockSignals(True)
            slider.setValue(0)
            slider.blockSignals(False)

    # Adjustment history: one snapshot per slider drag
    def begin_slider_edit(self):
        if self.current is not None and self._slider_before is None:
            self._slider_before = QPixmap(self.current)

    def finish_slider_edit(self):
        if self._slider_before is not None and self.current is not None:
            if self._slider_before.toImage() != self.current.toImage():
                self.undo_stack.append(self._slider_before)
                self.redo_stack.clear()
            self._slider_before = None
        self.update_status("Adjustments applied")

    def adjust_image(self):
        if self.original is None:
            return
        # Keep a stable base for slider adjustments; filters are applied to current image.
        base = self.pixmap_to_pil(self.original)
        image = ImageEnhance.Brightness(base).enhance(max(0, 1 + self.brightness.value() / 100))
        image = ImageEnhance.Contrast(image).enhance(max(0, 1 + self.contrast.value() / 100))
        image = ImageEnhance.Sharpness(image).enhance(max(0, 1 + self.sharpness.value() / 100))
        self.current = self.pil_to_pixmap(image)
        self.show_image()

    def require_image(self):
        if self.current is None:
            self.status.setText("Please open an image first.")
            return False
        return True

    # Mouse crop maps the selection on the displayed image to original pixels.
    def start_crop_mode(self):
        if not self.require_image():
            return
        QMessageBox.information(
            self, "Crop", "Drag a rectangle over the PROCESSED IMAGE preview to crop it."
        )
        self.processed_canvas.set_crop_mode(True)
        self.status.setText("Crop mode: drag over the processed image.")

    def apply_mouse_crop(self, canvas, rect):
        if self.current is None:
            return
        displayed = canvas.pixmap()
        if displayed is None or displayed.isNull():
            return
        # QLabel centers the pixmap. Account for letterboxing around it.
        pixmap = self.current
        shown_w, shown_h = displayed.width(), displayed.height()
        offset_x = (canvas.width() - shown_w) // 2
        offset_y = (canvas.height() - shown_h) // 2
        x1 = max(0, rect.left() - offset_x)
        y1 = max(0, rect.top() - offset_y)
        x2 = min(shown_w, rect.right() - offset_x)
        y2 = min(shown_h, rect.bottom() - offset_y)
        if x2 <= x1 or y2 <= y1:
            self.status.setText("Crop selection was outside the image.")
            return
        scale_x = pixmap.width() / shown_w
        scale_y = pixmap.height() / shown_h
        left = int(x1 * scale_x)
        top = int(y1 * scale_y)
        width = max(1, int((x2 - x1) * scale_x))
        height = max(1, int((y2 - y1) * scale_y))
        width = min(width, pixmap.width() - left)
        height = min(height, pixmap.height() - top)
        if width < 2 or height < 2:
            self.status.setText("Crop area is too small.")
            return
        self.save_history()
        self.current = self.current.copy(left, top, width, height)
        self.show_image()
        self.update_status("Crop applied")

    def crop_by_dimensions(self):
        if not self.require_image():
            return
        w, ok = QInputDialog.getInt(self, "Crop", "Crop width:", max(1, self.current.width() * 3 // 4), 1, self.current.width())
        if not ok:
            return
        h, ok = QInputDialog.getInt(self, "Crop", "Crop height:", max(1, self.current.height() * 3 // 4), 1, self.current.height())
        if not ok:
            return
        x = (self.current.width() - w) // 2
        y = (self.current.height() - h) // 2
        self.save_history()
        self.current = self.current.copy(x, y, w, h)
        self.show_image()
        self.update_status("Center crop applied")

    def rotate(self):
        if not self.require_image():
            return
        self.save_history()
        self.current = self.current.transformed(QTransform().rotate(90), Qt.TransformationMode.SmoothTransformation)
        self.show_image()
        self.update_status("Rotated 90°")

    def flip(self):
        if not self.require_image():
            return
        self.save_history()
        self.current = self.current.transformed(QTransform().scale(-1, 1))
        self.show_image()
        self.update_status("Flipped horizontally")

    def resize_image(self):
        if not self.require_image():
            return
        w, ok = QInputDialog.getInt(self, "Resize", "New width (pixels):", self.current.width(), 1, 20000)
        if not ok:
            return
        h, ok = QInputDialog.getInt(self, "Resize", "New height (pixels):", self.current.height(), 1, 20000)
        if not ok:
            return
        self.save_history()
        self.current = self.current.scaled(w, h, Qt.AspectRatioMode.IgnoreAspectRatio,
                                           Qt.TransformationMode.SmoothTransformation)
        self.show_image()
        self.update_status("Image resized")

    def apply_filter(self, operation):
        if not self.require_image():
            return
        self.save_history()
        rgba = np.array(self.pixmap_to_pil(self.current))
        rgb = cv2.cvtColor(rgba, cv2.COLOR_RGBA2RGB)
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
        if operation == "gray":
            result = cv2.cvtColor(gray, cv2.COLOR_GRAY2RGBA)
        elif operation == "blur":
            result = cv2.cvtColor(cv2.GaussianBlur(rgb, (15, 15), 0), cv2.COLOR_RGB2RGBA)
        elif operation == "median":
            result = cv2.cvtColor(cv2.medianBlur(rgb, 5), cv2.COLOR_RGB2RGBA)
        elif operation == "sharpen":
            kernel = np.array([[0, -1, 0], [-1, 5, -1], [0, -1, 0]])
            result = cv2.cvtColor(cv2.filter2D(rgb, -1, kernel), cv2.COLOR_RGB2RGBA)
        elif operation == "denoise":
            result = cv2.cvtColor(cv2.fastNlMeansDenoisingColored(rgb, None, 10, 10, 7, 21), cv2.COLOR_RGB2RGBA)
        elif operation == "sobel":
            sx = cv2.Sobel(gray, cv2.CV_64F, 1, 0, ksize=3)
            sy = cv2.Sobel(gray, cv2.CV_64F, 0, 1, ksize=3)
            result = cv2.cvtColor(cv2.convertScaleAbs(cv2.magnitude(sx, sy)), cv2.COLOR_GRAY2RGBA)
        elif operation == "canny":
            result = cv2.cvtColor(cv2.Canny(gray, 100, 200), cv2.COLOR_GRAY2RGBA)
        elif operation == "threshold":
            _, binary = cv2.threshold(gray, 127, 255, cv2.THRESH_BINARY)
            result = cv2.cvtColor(binary, cv2.COLOR_GRAY2RGBA)
        elif operation == "equalize":
            ycrcb = cv2.cvtColor(rgb, cv2.COLOR_RGB2YCrCb)
            ycrcb[:, :, 0] = cv2.equalizeHist(ycrcb[:, :, 0])
            equalized = cv2.cvtColor(ycrcb, cv2.COLOR_YCrCb2RGB)
            result = cv2.cvtColor(equalized, cv2.COLOR_RGB2RGBA)
        else:
            self.undo_stack.pop()
            return
        self.current = self.pil_to_pixmap(Image.fromarray(result))
        self.reset_sliders()
        self.show_image()
        self.update_status(f"{operation.title()} applied")

    def reset(self):
        if self.original is None:
            self.status.setText("Please open an image first.")
            return
        self.save_history()
        self._comparison_active = False
        self.compare_button.setText("Before / After")
        self.current = QPixmap(self.original)
        self.reset_sliders()
        self.show_image()
        self.update_status("Reset to original image")

    def save_image(self):
        if not self.require_image():
            return
        path, selected = QFileDialog.getSaveFileName(
            self, "Export Image", "edited_image.png",
            "PNG Image (*.png);;JPEG Image (*.jpg *.jpeg)"
        )
        if not path:
            return
        if not path.lower().endswith((".png", ".jpg", ".jpeg")):
            path += ".jpg" if "JPEG" in selected else ".png"
        try:
            image = self.pixmap_to_pil(self.current)
            if path.lower().endswith((".jpg", ".jpeg")):
                image = image.convert("RGB")
            image.save(path)
            self.status.setText(f"Exported: {path}")
        except Exception as exc:
            QMessageBox.warning(self, "Export Error", str(exc))

    def histogram(self):
        if not self.require_image():
            return
        image = np.array(self.pixmap_to_pil(self.current))
        plt.figure("AI Pixel Studio - RGB Histogram")
        for channel, color, name in [(0, "red", "Red"), (1, "green", "Green"), (2, "blue", "Blue")]:
            plt.hist(image[:, :, channel].ravel(), bins=256, range=(0, 256), color=color, alpha=0.4, label=name)
        plt.legend()
        plt.xlabel("Pixel Intensity")
        plt.ylabel("Pixel Count")
        plt.tight_layout()
        plt.show()

    # Optional AI: install dependencies before use.
    def background_removal(self):
        if not self.require_image():
            return
        try:
            from rembg import remove, new_session
            self.status.setText("Loading background-removal model...")
            QApplication.processEvents()
            if self._rembg_session is None:
                self._rembg_session = new_session("u2net")
            image = self.pixmap_to_pil(self.current).convert("RGBA")
            result = remove(image, session=self._rembg_session)
            self.save_history()
            self.current = self.pil_to_pixmap(result)
            self.show_image()
            self.update_status("AI background removed (transparent PNG supported)")
        except ImportError:
            QMessageBox.information(
                self, "Install AI background removal",
                "Run this command in the VS Code terminal:\n\npython -m pip install rembg onnxruntime"
            )
        except Exception as exc:
            QMessageBox.warning(self, "Background Removal Error", f"{exc}\n\nCheck your internet connection on first use; the model may need downloading.")

    def enhance_image(self):
        if not self.require_image():
            return
        # A transparent, dependency-light enhancement tool using classic image processing.
        # This is not a pretrained AI super-resolution model.
        try:
            factor, ok = QInputDialog.getItem(
                self, "Image Enhancement",
                "Choose enhancement:",
                ["Improve sharpness and contrast", "Upscale 2× (Lanczos)", "Upscale 2× + sharpen"],
                0, False
            )
            if not ok:
                return
            self.save_history()
            image = self.pixmap_to_pil(self.current)
            if factor == "Improve sharpness and contrast":
                image = ImageEnhance.Contrast(image).enhance(1.12)
                image = ImageEnhance.Sharpness(image).enhance(1.35)
                image = ImageEnhance.Color(image).enhance(1.05)
            else:
                image = image.resize((image.width * 2, image.height * 2), Image.Resampling.LANCZOS)
                if factor == "Upscale 2× + sharpen":
                    image = ImageEnhance.Sharpness(image).enhance(1.3)
            self.current = self.pil_to_pixmap(image)
            self.show_image()
            self.update_status("Classical enhancement applied (not AI super-resolution)")
        except Exception as exc:
            QMessageBox.warning(self, "Enhancement Error", str(exc))

    def caption_image(self):
        if not self.require_image():
            return
        try:
            from transformers import BlipProcessor, BlipForConditionalGeneration
            import torch
            self.status.setText("Loading image-captioning model (first use can take time)...")
            QApplication.processEvents()
            if self._caption_model is None or self._caption_processor is None:
                model_name = "Salesforce/blip-image-captioning-base"
                self._caption_processor = BlipProcessor.from_pretrained(model_name)
                self._caption_model = BlipForConditionalGeneration.from_pretrained(model_name)
                self._caption_model.eval()
            image = self.pixmap_to_pil(self.current).convert("RGB")
            inputs = self._caption_processor(images=image, return_tensors="pt")
            with torch.no_grad():
                output = self._caption_model.generate(**inputs, max_new_tokens=50)
            caption = self._caption_processor.decode(output[0], skip_special_tokens=True)
            self.caption_label.setText(caption)
            self.update_status("Image caption generated")
        except ImportError:
            QMessageBox.information(
                self, "Install image captioning",
                "Run this command in the VS Code terminal:\n\npython -m pip install transformers torch"
            )
        except Exception as exc:
            QMessageBox.warning(self, "Captioning Error", f"{exc}\n\nThe pretrained model may need internet access on first use.")


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = AIPixelStudio()
    window.show()
    sys.exit(app.exec())
