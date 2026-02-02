#!/usr/bin/env python3
"""
Video Concatenation & UGC Processing App
=========================================

A desktop app with two main features:
1. Concatenate videos from two folders matched by filename
2. Process UGC videos with overlays, captions, and end stings

Requirements:
    pip install pyside6 assemblyai

FFmpeg Installation:
    macOS:   brew install ffmpeg
    Ubuntu:  sudo apt install ffmpeg
    Windows: Download from https://ffmpeg.org/download.html and add to PATH

Usage:
    python app.py
"""

import sys
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from PySide6.QtCore import (
    Qt, Signal, QThread, QObject, QUrl, QSize
)
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QLineEdit, QSpinBox, QCheckBox,
    QTableWidget, QTableWidgetItem, QTextEdit, QProgressBar,
    QFileDialog, QMessageBox, QGroupBox, QFrame, QHeaderView, QSplitter,
    QAbstractItemView, QTabWidget, QDoubleSpinBox, QGridLayout, QListWidget,
    QListWidgetItem, QComboBox, QListView, QScrollArea
)
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QDesktopServices, QColor

from processor import (
    ConcatOrder, VideoMatch, TextOverlayConfig,
    apply_text_overlay, check_ffmpeg_available, find_matches, get_match_counts,
    process_video_pair, scan_video_files
)

from ugc_processor import (
    scan_ugc_videos, process_ugc_video, UGCProcessingResult, ASSETS_DIR
)


class DropZone(QFrame):
    """A drag-and-drop zone for folder selection."""

    pathChanged = Signal(str)

    def __init__(self, label: str, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setMinimumHeight(80)
        self.setFrameStyle(QFrame.StyledPanel | QFrame.Sunken)

        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignCenter)

        self.title_label = QLabel(label)
        self.title_label.setStyleSheet("font-weight: bold; font-size: 14px;")
        self.title_label.setAlignment(Qt.AlignCenter)

        self.path_label = QLabel("Drop folder here or click Browse")
        self.path_label.setStyleSheet("color: #666; font-style: italic;")
        self.path_label.setAlignment(Qt.AlignCenter)
        self.path_label.setWordWrap(True)

        self.count_label = QLabel("")
        self.count_label.setAlignment(Qt.AlignCenter)

        layout.addWidget(self.title_label)
        layout.addWidget(self.path_label)
        layout.addWidget(self.count_label)

        self._path: Optional[str] = None
        self._update_style(False)

    def _update_style(self, has_path: bool):
        if has_path:
            self.setStyleSheet("""
                DropZone {
                    background-color: #e8f5e9;
                    border: 2px solid #4caf50;
                    border-radius: 8px;
                }
            """)
        else:
            self.setStyleSheet("""
                DropZone {
                    background-color: #f5f5f5;
                    border: 2px dashed #aaa;
                    border-radius: 8px;
                }
                DropZone:hover {
                    background-color: #e3f2fd;
                    border-color: #2196f3;
                }
            """)

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            urls = event.mimeData().urls()
            if urls and urls[0].isLocalFile():
                path = urls[0].toLocalFile()
                if os.path.isdir(path):
                    event.acceptProposedAction()
                    self.setStyleSheet("""
                        DropZone {
                            background-color: #e3f2fd;
                            border: 2px solid #2196f3;
                            border-radius: 8px;
                        }
                    """)

    def dragLeaveEvent(self, event):
        self._update_style(self._path is not None)

    def dropEvent(self, event: QDropEvent):
        urls = event.mimeData().urls()
        if urls:
            path = urls[0].toLocalFile()
            if os.path.isdir(path):
                self.set_path(path)
                event.acceptProposedAction()

    def set_path(self, path: str):
        self._path = path
        # Truncate display if too long
        display_path = path
        if len(display_path) > 50:
            display_path = "..." + display_path[-47:]
        self.path_label.setText(display_path)
        self.path_label.setStyleSheet("color: #333;")
        self.path_label.setToolTip(path)

        # Count videos
        videos = scan_video_files(Path(path))
        count = len(videos)
        self.count_label.setText(f"{count} video{'s' if count != 1 else ''} found")
        self.count_label.setStyleSheet("color: #4caf50; font-weight: bold;" if count > 0 else "color: #f44336;")

        self._update_style(True)
        self.pathChanged.emit(path)

    def get_path(self) -> Optional[str]:
        return self._path

    def clear(self):
        self._path = None
        self.path_label.setText("Drop folder here or click Browse")
        self.path_label.setStyleSheet("color: #666; font-style: italic;")
        self.path_label.setToolTip("")
        self.count_label.setText("")
        self._update_style(False)


class TimelineList(QListWidget):
    """Drag-and-drop timeline list for ordering video segments."""

    orderChanged = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setDragDropMode(QAbstractItemView.InternalMove)
        self.setDefaultDropAction(Qt.MoveAction)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setFlow(QListView.LeftToRight)
        self.setWrapping(False)
        self.setSpacing(6)

    def dropEvent(self, event):
        super().dropEvent(event)
        self.orderChanged.emit()


@dataclass
class OverlayJob:
    input_video: Path
    output_path: Path
    overlay: TextOverlayConfig


class ProcessingWorker(QObject):
    """Worker for processing video pairs in a background thread."""

    progress = Signal(int, int)  # current, total
    log = Signal(str)
    finished = Signal(int, int, int)  # success, failed, skipped
    single_complete = Signal(str, bool)  # basename, success

    def __init__(self):
        super().__init__()
        self._cancel_requested = False

    def cancel(self):
        self._cancel_requested = True

    def is_cancelled(self) -> bool:
        return self._cancel_requested

    def process(
        self,
        matches: list[VideoMatch],
        output_base: Path,
        flat_folder: str,
        nested_folder: str,
        order: ConcatOrder,
        crf: int,
        try_fast_copy: bool,
        overlay_a: Optional[TextOverlayConfig] = None,
        overlay_b: Optional[TextOverlayConfig] = None
    ):
        self._cancel_requested = False

        # Filter to only matched pairs
        matched = [m for m in matches if m.is_matched]
        total = len(matched)

        if total == 0:
            self.log.emit("No matched pairs to process!")
            self.finished.emit(0, 0, 0)
            return

        flat_dir = output_base / flat_folder
        nested_dir = output_base / nested_folder

        flat_dir.mkdir(parents=True, exist_ok=True)
        nested_dir.mkdir(parents=True, exist_ok=True)

        success_count = 0
        fail_count = 0

        self.log.emit(f"Starting processing of {total} matched pairs...")
        self.log.emit(f"Order: {order.value}")
        self.log.emit(f"CRF: {crf}, Fast copy: {try_fast_copy}")
        if (overlay_a and overlay_a.is_enabled()) or (overlay_b and overlay_b.is_enabled()):
            self.log.emit("Text overlays: enabled")
        self.log.emit(f"Output: {output_base}")
        self.log.emit("-" * 50)

        for idx, match in enumerate(matched, 1):
            if self._cancel_requested:
                self.log.emit("\n*** CANCELLED BY USER ***")
                break

            self.progress.emit(idx, total)

            # Output paths
            output_name = f"{match.basename}.mp4"
            output_flat = flat_dir / output_name
            nested_subdir = nested_dir / str(idx)
            nested_subdir.mkdir(parents=True, exist_ok=True)
            output_nested = nested_subdir / output_name

            result = process_video_pair(
                match=match,
                output_flat=output_flat,
                output_nested=output_nested,
                order=order,
                crf=crf,
                try_fast_copy=try_fast_copy,
                overlay_a=overlay_a,
                overlay_b=overlay_b,
                log_callback=lambda msg: self.log.emit(msg),
                cancel_check=self.is_cancelled
            )

            if result.success:
                success_count += 1
            else:
                fail_count += 1

            self.single_complete.emit(match.basename, result.success)
            self.log.emit("")

        skipped = total - success_count - fail_count
        self.log.emit("=" * 50)
        self.log.emit(f"COMPLETE: {success_count} success, {fail_count} failed, {skipped} skipped")
        self.finished.emit(success_count, fail_count, skipped)

    def process_overlays(
        self,
        jobs: list[OverlayJob],
        crf: int
    ):
        self._cancel_requested = False
        total = len(jobs)

        if total == 0:
            self.log.emit("No videos to process!")
            self.finished.emit(0, 0, 0)
            return

        success_count = 0
        fail_count = 0

        self.log.emit(f"Starting text overlay processing of {total} videos...")
        self.log.emit(f"CRF: {crf}")
        self.log.emit("-" * 50)

        for idx, job in enumerate(jobs, 1):
            if self._cancel_requested:
                self.log.emit("\n*** CANCELLED BY USER ***")
                break

            self.progress.emit(idx, total)

            success, error = apply_text_overlay(
                input_video=job.input_video,
                output_path=job.output_path,
                overlay=job.overlay,
                crf=crf,
                log_callback=lambda msg: self.log.emit(msg),
                cancel_check=self.is_cancelled
            )

            if success:
                success_count += 1
            else:
                fail_count += 1
                if error:
                    self.log.emit(f"  FAILED: {error[:200]}")

            self.single_complete.emit(job.input_video.name, success)
            self.log.emit("")

        skipped = total - success_count - fail_count
        self.log.emit("=" * 50)
        self.log.emit(f"COMPLETE: {success_count} success, {fail_count} failed, {skipped} skipped")
        self.finished.emit(success_count, fail_count, skipped)


class UGCProcessingWorker(QObject):
    """Worker for processing UGC videos in a background thread."""

    progress = Signal(int, int)  # current, total
    log = Signal(str)
    finished = Signal(int, int)  # success, failed
    single_complete = Signal(str, bool)  # filename, success

    def __init__(self):
        super().__init__()
        self._cancel_requested = False

    def cancel(self):
        self._cancel_requested = True

    def is_cancelled(self) -> bool:
        return self._cancel_requested

    def process(
        self,
        videos: list[Path],
        output_dir: Path,
        api_key: str,
        add1_path: Path,
        add2_path: Path,
        clip_end_path: Path,
        add1_x: int,
        add1_y: int,
        add2_opacity: float,
        crf: int,
        enable_captions: bool = True
    ):
        self._cancel_requested = False
        total = len(videos)

        if total == 0:
            self.log.emit("No videos to process!")
            self.finished.emit(0, 0)
            return

        output_dir.mkdir(parents=True, exist_ok=True)

        success_count = 0
        fail_count = 0

        self.log.emit(f"Starting UGC processing of {total} videos...")
        self.log.emit(f"Output: {output_dir}")
        self.log.emit(f"Captions: {'Enabled' if enable_captions else 'Disabled'}")
        self.log.emit(f"Overlay 1: {add1_path.name} at ({add1_x}, {add1_y})")
        self.log.emit(f"Overlay 2: {add2_path.name} at {add2_opacity*100:.0f}% opacity")
        self.log.emit(f"End sting: {clip_end_path.name}")
        self.log.emit("-" * 50)

        for idx, video in enumerate(videos, 1):
            if self._cancel_requested:
                self.log.emit("\n*** CANCELLED BY USER ***")
                break

            self.progress.emit(idx, total)

            output_path = output_dir / f"{video.stem}_processed.mp4"

            result = process_ugc_video(
                input_video=video,
                output_path=output_path,
                api_key=api_key,
                add1_overlay=add1_path,
                add2_overlay=add2_path,
                clip_end=clip_end_path,
                add1_position=(add1_x, add1_y),
                add2_opacity=add2_opacity,
                crf=crf,
                enable_captions=enable_captions,
                log_callback=lambda msg: self.log.emit(msg),
                cancel_check=self.is_cancelled
            )

            if result.success:
                success_count += 1
            else:
                fail_count += 1

            self.single_complete.emit(video.name, result.success)
            self.log.emit("")

        self.log.emit("=" * 50)
        self.log.emit(f"COMPLETE: {success_count} success, {fail_count} failed")
        self.finished.emit(success_count, fail_count)


class ConcatTab(QWidget):
    """Tab for video concatenation functionality."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.matches: list[VideoMatch] = []
        self.worker: Optional[ProcessingWorker] = None
        self.worker_thread: Optional[QThread] = None
        self._setup_ui()

    def _setup_ui(self):
        outer_layout = QVBoxLayout(self)
        outer_layout.setSpacing(10)

        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setFrameShape(QFrame.NoFrame)

        scroll_container = QWidget()
        scroll_layout = QVBoxLayout(scroll_container)
        scroll_layout.setSpacing(10)

        scroll_area.setWidget(scroll_container)
        outer_layout.addWidget(scroll_area, 0)

        main_layout = scroll_layout

        # === Folder Selection Area ===
        folders_group = QGroupBox("Input Folders")
        folders_layout = QHBoxLayout(folders_group)

        # Folder A
        folder_a_layout = QVBoxLayout()
        self.drop_zone_a = DropZone("Folder A")
        self.drop_zone_a.pathChanged.connect(self._on_path_changed)
        self.btn_browse_a = QPushButton("Browse...")
        self.btn_browse_a.clicked.connect(lambda: self._browse_folder(self.drop_zone_a))
        folder_a_layout.addWidget(self.drop_zone_a)
        folder_a_layout.addWidget(self.btn_browse_a)

        # Folder B
        folder_b_layout = QVBoxLayout()
        self.drop_zone_b = DropZone("Folder B")
        self.drop_zone_b.pathChanged.connect(self._on_path_changed)
        self.btn_browse_b = QPushButton("Browse...")
        self.btn_browse_b.clicked.connect(lambda: self._browse_folder(self.drop_zone_b))
        folder_b_layout.addWidget(self.drop_zone_b)
        folder_b_layout.addWidget(self.btn_browse_b)

        folders_layout.addLayout(folder_a_layout)
        folders_layout.addLayout(folder_b_layout)
        main_layout.addWidget(folders_group)

        # === Output Configuration ===
        output_group = QGroupBox("Output Configuration")
        output_layout = QVBoxLayout(output_group)

        # Output base directory
        output_dir_layout = QHBoxLayout()
        output_dir_layout.addWidget(QLabel("Output Directory:"))
        self.drop_zone_output = DropZone("Output Base")
        self.drop_zone_output.setMinimumHeight(60)
        self.drop_zone_output.pathChanged.connect(self._on_path_changed)
        self.btn_browse_output = QPushButton("Browse...")
        self.btn_browse_output.clicked.connect(lambda: self._browse_folder(self.drop_zone_output))
        output_dir_layout.addWidget(self.drop_zone_output, 1)
        output_dir_layout.addWidget(self.btn_browse_output)
        output_layout.addLayout(output_dir_layout)

        # Folder names
        names_layout = QHBoxLayout()
        names_layout.addWidget(QLabel("Flat Folder Name:"))
        self.input_flat_name = QLineEdit("flat_outputs")
        names_layout.addWidget(self.input_flat_name)
        names_layout.addSpacing(20)
        names_layout.addWidget(QLabel("Nested Folder Name:"))
        self.input_nested_name = QLineEdit("nested_outputs")
        names_layout.addWidget(self.input_nested_name)
        output_layout.addLayout(names_layout)

        main_layout.addWidget(output_group)

        # === Interactive Timeline ===
        timeline_group = QGroupBox("Interactive Timeline")
        timeline_layout = QVBoxLayout(timeline_group)
        timeline_layout.addWidget(QLabel("Drag blocks to set the final output order:"))

        self.timeline_list = TimelineList()
        self.timeline_list.setFixedHeight(80)
        self.timeline_list.setStyleSheet(
            "QListWidget { background-color: #f8f9fb; border: 1px solid #ddd; border-radius: 6px; padding: 6px; }"
            "QListWidget::item { border: 1px solid #cfd8dc; border-radius: 6px; padding: 8px; margin: 4px; }"
            "QListWidget::item:selected { background-color: #ffe082; }"
        )

        item_a = QListWidgetItem("Video A")
        item_a.setData(Qt.UserRole, "A")
        item_a.setTextAlignment(Qt.AlignCenter)
        item_a.setSizeHint(QSize(120, 40))
        item_a.setBackground(QColor("#BBDEFB"))
        item_a.setForeground(QColor("#0D47A1"))

        item_b = QListWidgetItem("Video B")
        item_b.setData(Qt.UserRole, "B")
        item_b.setTextAlignment(Qt.AlignCenter)
        item_b.setSizeHint(QSize(120, 40))
        item_b.setBackground(QColor("#C8E6C9"))
        item_b.setForeground(QColor("#1B5E20"))

        self.timeline_list.addItem(item_a)
        self.timeline_list.addItem(item_b)
        self.timeline_list.orderChanged.connect(self._update_timeline_sequence)

        self.timeline_sequence_label = QLabel("")
        self.timeline_sequence_label.setStyleSheet("font-weight: bold;")
        self._update_timeline_sequence()

        timeline_layout.addWidget(self.timeline_list)
        timeline_layout.addWidget(self.timeline_sequence_label)
        main_layout.addWidget(timeline_group)

        # === Text Overlays ===
        overlay_group = QGroupBox("Text Overlays")
        overlay_layout = QGridLayout(overlay_group)
        overlay_layout.setColumnStretch(1, 1)
        overlay_layout.setColumnStretch(2, 1)

        overlay_layout.addWidget(QLabel(""), 0, 0)
        overlay_layout.addWidget(QLabel("<b>Video A</b>"), 0, 1)
        overlay_layout.addWidget(QLabel("<b>Video B</b>"), 0, 2)

        overlay_layout.addWidget(QLabel("Text"), 1, 0)
        self.input_overlay_a_text = QLineEdit()
        self.input_overlay_a_text.setPlaceholderText("Text for Video A overlay")
        self.input_overlay_b_text = QLineEdit()
        self.input_overlay_b_text.setPlaceholderText("Text for Video B overlay")
        overlay_layout.addWidget(self.input_overlay_a_text, 1, 1)
        overlay_layout.addWidget(self.input_overlay_b_text, 1, 2)

        overlay_layout.addWidget(QLabel("Layout"), 2, 0)
        self.combo_overlay_a_layout = QComboBox()
        self.combo_overlay_a_layout.addItems(["Top Center (auto fit)", "Manual (x/y)"])
        self.combo_overlay_b_layout = QComboBox()
        self.combo_overlay_b_layout.addItems(["Top Center (auto fit)", "Manual (x/y)"])
        overlay_layout.addWidget(self.combo_overlay_a_layout, 2, 1)
        overlay_layout.addWidget(self.combo_overlay_b_layout, 2, 2)

        overlay_layout.addWidget(QLabel("Text Box Width"), 3, 0)
        self.spin_overlay_a_box_width = QSpinBox()
        self.spin_overlay_a_box_width.setRange(0, 10000)
        self.spin_overlay_a_box_width.setValue(900)
        self.spin_overlay_b_box_width = QSpinBox()
        self.spin_overlay_b_box_width.setRange(0, 10000)
        self.spin_overlay_b_box_width.setValue(900)
        overlay_layout.addWidget(self.spin_overlay_a_box_width, 3, 1)
        overlay_layout.addWidget(self.spin_overlay_b_box_width, 3, 2)

        overlay_layout.addWidget(QLabel("Text Box Height"), 4, 0)
        self.spin_overlay_a_box_height = QSpinBox()
        self.spin_overlay_a_box_height.setRange(0, 10000)
        self.spin_overlay_a_box_height.setValue(0)
        self.spin_overlay_b_box_height = QSpinBox()
        self.spin_overlay_b_box_height.setRange(0, 10000)
        self.spin_overlay_b_box_height.setValue(0)
        overlay_layout.addWidget(self.spin_overlay_a_box_height, 4, 1)
        overlay_layout.addWidget(self.spin_overlay_b_box_height, 4, 2)

        overlay_layout.addWidget(QLabel("X Position"), 5, 0)
        self.spin_overlay_a_x = QSpinBox()
        self.spin_overlay_a_x.setRange(-5000, 5000)
        self.spin_overlay_a_x.setValue(100)
        self.spin_overlay_b_x = QSpinBox()
        self.spin_overlay_b_x.setRange(-5000, 5000)
        self.spin_overlay_b_x.setValue(100)
        overlay_layout.addWidget(self.spin_overlay_a_x, 5, 1)
        overlay_layout.addWidget(self.spin_overlay_b_x, 5, 2)

        overlay_layout.addWidget(QLabel("Y Position"), 6, 0)
        self.spin_overlay_a_y = QSpinBox()
        self.spin_overlay_a_y.setRange(-5000, 5000)
        self.spin_overlay_a_y.setValue(300)
        self.spin_overlay_b_y = QSpinBox()
        self.spin_overlay_b_y.setRange(-5000, 5000)
        self.spin_overlay_b_y.setValue(300)
        overlay_layout.addWidget(self.spin_overlay_a_y, 6, 1)
        overlay_layout.addWidget(self.spin_overlay_b_y, 6, 2)

        overlay_layout.addWidget(QLabel("Duration (s)"), 7, 0)
        self.spin_overlay_a_duration = QDoubleSpinBox()
        self.spin_overlay_a_duration.setRange(0, 600)
        self.spin_overlay_a_duration.setDecimals(1)
        self.spin_overlay_a_duration.setSingleStep(0.5)
        self.spin_overlay_a_duration.setSpecialValueText("Full length")
        self.spin_overlay_a_duration.setValue(0.0)
        self.spin_overlay_b_duration = QDoubleSpinBox()
        self.spin_overlay_b_duration.setRange(0, 600)
        self.spin_overlay_b_duration.setDecimals(1)
        self.spin_overlay_b_duration.setSingleStep(0.5)
        self.spin_overlay_b_duration.setSpecialValueText("Full length")
        self.spin_overlay_b_duration.setValue(0.0)
        overlay_layout.addWidget(self.spin_overlay_a_duration, 7, 1)
        overlay_layout.addWidget(self.spin_overlay_b_duration, 7, 2)

        overlay_layout.addWidget(QLabel("Font Size"), 8, 0)
        self.spin_overlay_a_size = QSpinBox()
        self.spin_overlay_a_size.setRange(8, 200)
        self.spin_overlay_a_size.setValue(72)
        self.spin_overlay_b_size = QSpinBox()
        self.spin_overlay_b_size.setRange(8, 200)
        self.spin_overlay_b_size.setValue(72)
        overlay_layout.addWidget(self.spin_overlay_a_size, 8, 1)
        overlay_layout.addWidget(self.spin_overlay_b_size, 8, 2)

        overlay_layout.addWidget(QLabel("Font Color"), 9, 0)
        self.input_overlay_a_color = QLineEdit("#FFFFFF")
        self.input_overlay_a_color.setPlaceholderText("#FFFFFF or white")
        self.input_overlay_b_color = QLineEdit("#FFFFFF")
        self.input_overlay_b_color.setPlaceholderText("#FFFFFF or white")
        overlay_layout.addWidget(self.input_overlay_a_color, 9, 1)
        overlay_layout.addWidget(self.input_overlay_b_color, 9, 2)

        overlay_layout.addWidget(QLabel("Font Name (optional)"), 10, 0)
        self.input_overlay_a_font = QLineEdit()
        self.input_overlay_a_font.setPlaceholderText("e.g., Arial")
        self.input_overlay_b_font = QLineEdit()
        self.input_overlay_b_font.setPlaceholderText("e.g., Arial")
        overlay_layout.addWidget(self.input_overlay_a_font, 10, 1)
        overlay_layout.addWidget(self.input_overlay_b_font, 10, 2)

        overlay_layout.addWidget(QLabel("Font Style (optional)"), 11, 0)
        self.combo_overlay_a_style = QComboBox()
        self.combo_overlay_a_style.addItems(["Normal", "Bold", "Italic", "Bold Italic"])
        self.combo_overlay_b_style = QComboBox()
        self.combo_overlay_b_style.addItems(["Normal", "Bold", "Italic", "Bold Italic"])
        overlay_layout.addWidget(self.combo_overlay_a_style, 11, 1)
        overlay_layout.addWidget(self.combo_overlay_b_style, 11, 2)

        self.input_overlay_a_text.textChanged.connect(self._update_button_states)
        self.input_overlay_b_text.textChanged.connect(self._update_button_states)
        self.combo_overlay_a_layout.currentIndexChanged.connect(self._update_button_states)
        self.combo_overlay_b_layout.currentIndexChanged.connect(self._update_button_states)

        main_layout.addWidget(overlay_group)

        # === Encoding Options ===
        encoding_group = QGroupBox("Encoding Options")
        encoding_layout = QHBoxLayout(encoding_group)

        # CRF setting
        crf_layout = QVBoxLayout()
        crf_layout.addWidget(QLabel("CRF (Quality):"))
        crf_input_layout = QHBoxLayout()
        self.spin_crf = QSpinBox()
        self.spin_crf.setRange(0, 51)
        self.spin_crf.setValue(18)
        self.spin_crf.setToolTip("Lower = better quality, larger file. 18 is visually lossless.")
        crf_input_layout.addWidget(self.spin_crf)
        crf_input_layout.addWidget(QLabel("(0-51, lower=better)"))
        crf_input_layout.addStretch()
        crf_layout.addLayout(crf_input_layout)
        encoding_layout.addLayout(crf_layout)

        encoding_layout.addSpacing(30)

        # Fast copy option
        fast_layout = QVBoxLayout()
        self.check_fast_copy = QCheckBox("Fast copy when possible")
        self.check_fast_copy.setToolTip("Try lossless concat first; fall back to re-encode if incompatible")
        fast_layout.addWidget(self.check_fast_copy)
        fast_layout.addStretch()
        encoding_layout.addLayout(fast_layout)

        encoding_layout.addStretch()
        main_layout.addWidget(encoding_group)

        # === Action Buttons ===
        buttons_layout = QHBoxLayout()

        self.btn_scan = QPushButton("Scan / Preview Matches")
        self.btn_scan.clicked.connect(self._scan_matches)
        self.btn_scan.setMinimumHeight(35)

        self.btn_start = QPushButton("Start Processing")
        self.btn_start.clicked.connect(self._start_processing)
        self.btn_start.setEnabled(False)
        self.btn_start.setMinimumHeight(35)
        self.btn_start.setStyleSheet("QPushButton { background-color: #4caf50; color: white; font-weight: bold; }"
                                     "QPushButton:disabled { background-color: #ccc; color: #666; }")

        self.btn_overlay_only = QPushButton("Add Text Overlay to All Videos (No Concatenation)")
        self.btn_overlay_only.clicked.connect(self._start_overlay_only)
        self.btn_overlay_only.setEnabled(False)
        self.btn_overlay_only.setMinimumHeight(35)
        self.btn_overlay_only.setStyleSheet("QPushButton { background-color: #1976d2; color: white; font-weight: bold; }"
                                            "QPushButton:disabled { background-color: #ccc; color: #666; }")

        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.clicked.connect(self._cancel_processing)
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.setMinimumHeight(35)
        self.btn_cancel.setStyleSheet("QPushButton { background-color: #f44336; color: white; }")

        self.btn_open_output = QPushButton("Open Output Folder")
        self.btn_open_output.clicked.connect(self._open_output_folder)
        self.btn_open_output.setEnabled(False)
        self.btn_open_output.setMinimumHeight(35)

        buttons_layout.addWidget(self.btn_scan)
        buttons_layout.addWidget(self.btn_start)
        buttons_layout.addWidget(self.btn_overlay_only)
        buttons_layout.addWidget(self.btn_cancel)
        buttons_layout.addWidget(self.btn_open_output)
        main_layout.addLayout(buttons_layout)

        # === Match Stats ===
        self.stats_label = QLabel("")
        self.stats_label.setStyleSheet("font-size: 12px; padding: 5px;")
        main_layout.addWidget(self.stats_label)

        # === Splitter for table and log ===
        splitter = QSplitter(Qt.Vertical)

        # Match preview table
        table_container = QWidget()
        table_layout = QVBoxLayout(table_container)
        table_layout.setContentsMargins(0, 0, 0, 0)
        table_layout.addWidget(QLabel("Preview (matched pairs will be processed):"))

        self.table = QTableWidget()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(["Name", "File A", "File B", "Status", "Result"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table_layout.addWidget(self.table)
        splitter.addWidget(table_container)

        # Log and progress
        log_container = QWidget()
        log_layout = QVBoxLayout(log_container)
        log_layout.setContentsMargins(0, 0, 0, 0)

        progress_layout = QHBoxLayout()
        self.progress_label = QLabel("Ready")
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        progress_layout.addWidget(self.progress_label)
        progress_layout.addWidget(self.progress_bar, 1)
        log_layout.addLayout(progress_layout)

        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setStyleSheet("font-family: monospace; font-size: 11px;")
        log_layout.addWidget(self.log_text)
        splitter.addWidget(log_container)

        splitter.setSizes([300, 200])
        outer_layout.addWidget(splitter, 1)

    def _update_timeline_sequence(self):
        """Update the displayed final output order."""
        labels = []
        for i in range(self.timeline_list.count()):
            item = self.timeline_list.item(i)
            labels.append(item.text())
        sequence = " -> ".join(labels) if labels else "-"
        self.timeline_sequence_label.setText(f"Final output order: {sequence}")

    def _get_concat_order(self) -> ConcatOrder:
        """Return ConcatOrder based on the timeline list order."""
        if self.timeline_list.count() < 2:
            return ConcatOrder.A_THEN_B
        first_item = self.timeline_list.item(0)
        first_role = first_item.data(Qt.UserRole)
        return ConcatOrder.A_THEN_B if first_role == "A" else ConcatOrder.B_THEN_A

    def _build_overlay_config(
        self,
        text_input: QLineEdit,
        x_spin: QSpinBox,
        y_spin: QSpinBox,
        duration_spin: QDoubleSpinBox,
        size_spin: QSpinBox,
        color_input: QLineEdit,
        font_input: QLineEdit,
        style_combo: QComboBox,
        layout_combo: QComboBox,
        box_width_spin: QSpinBox,
        box_height_spin: QSpinBox
    ) -> TextOverlayConfig:
        color = color_input.text().strip() or "white"
        font_family = font_input.text().strip() or None
        layout_text = layout_combo.currentText().lower()
        align = "top_center" if "top center" in layout_text else "manual"
        return TextOverlayConfig(
            text=text_input.text().strip(),
            x=x_spin.value(),
            y=y_spin.value(),
            duration=duration_spin.value(),
            font_size=size_spin.value(),
            font_color=color,
            font_family=font_family,
            font_style=style_combo.currentText(),
            align=align,
            box_width=box_width_spin.value(),
            box_height=box_height_spin.value()
        )

    def _browse_folder(self, drop_zone: DropZone):
        """Open folder browser dialog."""
        folder = QFileDialog.getExistingDirectory(
            self,
            "Select Folder",
            "",
            QFileDialog.ShowDirsOnly
        )
        if folder:
            drop_zone.set_path(folder)

    def _on_path_changed(self, path: str):
        """Handle path changes - update UI state."""
        self._update_button_states()

    def _update_button_states(self):
        """Update enabled state of buttons based on current state."""
        if self.worker is not None:
            self.btn_scan.setEnabled(False)
            self.btn_start.setEnabled(False)
            self.btn_overlay_only.setEnabled(False)
            self.btn_open_output.setEnabled(self.drop_zone_output.get_path() is not None)
            return

        has_a = self.drop_zone_a.get_path() is not None
        has_b = self.drop_zone_b.get_path() is not None
        has_output = self.drop_zone_output.get_path() is not None
        has_matches = any(m.is_matched for m in self.matches)
        overlay_text_a = self.input_overlay_a_text.text().strip()
        overlay_text_b = self.input_overlay_b_text.text().strip()
        if has_a and has_b:
            has_overlay_text = bool(overlay_text_a or overlay_text_b)
        elif has_a:
            has_overlay_text = bool(overlay_text_a)
        elif has_b:
            has_overlay_text = bool(overlay_text_b)
        else:
            has_overlay_text = False

        self.btn_scan.setEnabled(has_a and has_b)
        self.btn_start.setEnabled(has_a and has_b and has_output and has_matches)
        self.btn_overlay_only.setEnabled(has_output and (has_a or has_b) and has_overlay_text)
        self.btn_open_output.setEnabled(has_output)

    def _scan_matches(self):
        """Scan folders and find matches."""
        path_a = self.drop_zone_a.get_path()
        path_b = self.drop_zone_b.get_path()

        if not path_a or not path_b:
            return

        self.matches = find_matches(Path(path_a), Path(path_b))
        matched, only_a, only_b = get_match_counts(self.matches)

        # Update stats label
        self.stats_label.setText(
            f"<b>Matched:</b> {matched} | "
            f"<span style='color: #ff9800'>Only in A:</span> {only_a} | "
            f"<span style='color: #2196f3'>Only in B:</span> {only_b}"
        )

        # Populate table
        self.table.setRowCount(len(self.matches))
        for row, match in enumerate(self.matches):
            self.table.setItem(row, 0, QTableWidgetItem(match.basename))
            self.table.setItem(row, 1, QTableWidgetItem(match.file_a.name if match.file_a else "-"))
            self.table.setItem(row, 2, QTableWidgetItem(match.file_b.name if match.file_b else "-"))

            status_item = QTableWidgetItem(match.status)
            if match.is_matched:
                status_item.setBackground(Qt.green)
            elif match.file_a is None:
                status_item.setBackground(Qt.cyan)
            else:
                status_item.setBackground(Qt.yellow)
            self.table.setItem(row, 3, status_item)

            self.table.setItem(row, 4, QTableWidgetItem(""))

        self._log(f"Scan complete: {matched} matched, {only_a} only in A, {only_b} only in B")
        self._update_button_states()

    def _start_processing(self):
        """Start the processing operation."""
        path_a = self.drop_zone_a.get_path()
        path_b = self.drop_zone_b.get_path()
        path_output = self.drop_zone_output.get_path()

        if not all([path_a, path_b, path_output]):
            QMessageBox.warning(self, "Missing Paths", "Please select all required folders.")
            return

        # Validate paths
        for path, name in [(path_a, "Folder A"), (path_b, "Folder B"), (path_output, "Output")]:
            if not os.path.isdir(path):
                QMessageBox.warning(self, "Invalid Path", f"{name} is not a valid directory.")
                return

        # Check FFmpeg again
        available, _ = check_ffmpeg_available()
        if not available:
            QMessageBox.critical(self, "FFmpeg Not Found", "FFmpeg is required but not available.")
            return

        # Get options
        order = self._get_concat_order()
        crf = self.spin_crf.value()
        fast_copy = self.check_fast_copy.isChecked()
        flat_name = self.input_flat_name.text().strip() or "flat_outputs"
        nested_name = self.input_nested_name.text().strip() or "nested_outputs"
        overlay_a = self._build_overlay_config(
            self.input_overlay_a_text,
            self.spin_overlay_a_x,
            self.spin_overlay_a_y,
            self.spin_overlay_a_duration,
            self.spin_overlay_a_size,
            self.input_overlay_a_color,
            self.input_overlay_a_font,
            self.combo_overlay_a_style,
            self.combo_overlay_a_layout,
            self.spin_overlay_a_box_width,
            self.spin_overlay_a_box_height
        )
        overlay_b = self._build_overlay_config(
            self.input_overlay_b_text,
            self.spin_overlay_b_x,
            self.spin_overlay_b_y,
            self.spin_overlay_b_duration,
            self.spin_overlay_b_size,
            self.input_overlay_b_color,
            self.input_overlay_b_font,
            self.combo_overlay_b_style,
            self.combo_overlay_b_layout,
            self.spin_overlay_b_box_width,
            self.spin_overlay_b_box_height
        )

        if (overlay_a.is_enabled() or overlay_b.is_enabled()) and fast_copy:
            self._log("Text overlays enabled; fast copy will be disabled.")
            fast_copy = False

        # Setup worker
        self.worker = ProcessingWorker()
        self.worker_thread = QThread()
        self.worker.moveToThread(self.worker_thread)

        # Connect signals
        self.worker.progress.connect(self._on_progress)
        self.worker.log.connect(self._log)
        self.worker.finished.connect(self._on_finished)
        self.worker.single_complete.connect(self._on_single_complete)

        # UI state
        self.btn_start.setEnabled(False)
        self.btn_scan.setEnabled(False)
        self.btn_overlay_only.setEnabled(False)
        self.btn_cancel.setEnabled(True)
        self.progress_bar.setValue(0)
        self.log_text.clear()

        # Clear result column
        for row in range(self.table.rowCount()):
            self.table.setItem(row, 4, QTableWidgetItem(""))

        # Start processing
        self.worker_thread.started.connect(
            lambda: self.worker.process(
                self.matches,
                Path(path_output),
                flat_name,
                nested_name,
                order,
                crf,
                fast_copy,
                overlay_a,
                overlay_b
            )
        )
        self.worker_thread.start()

    def _start_overlay_only(self):
        """Apply text overlays to all input videos without concatenation."""
        path_a = self.drop_zone_a.get_path()
        path_b = self.drop_zone_b.get_path()
        path_output = self.drop_zone_output.get_path()

        if not path_output or not (path_a or path_b):
            QMessageBox.warning(self, "Missing Paths", "Please select at least one input folder and an output folder.")
            return

        overlay_a = self._build_overlay_config(
            self.input_overlay_a_text,
            self.spin_overlay_a_x,
            self.spin_overlay_a_y,
            self.spin_overlay_a_duration,
            self.spin_overlay_a_size,
            self.input_overlay_a_color,
            self.input_overlay_a_font,
            self.combo_overlay_a_style,
            self.combo_overlay_a_layout,
            self.spin_overlay_a_box_width,
            self.spin_overlay_a_box_height
        )
        overlay_b = self._build_overlay_config(
            self.input_overlay_b_text,
            self.spin_overlay_b_x,
            self.spin_overlay_b_y,
            self.spin_overlay_b_duration,
            self.spin_overlay_b_size,
            self.input_overlay_b_color,
            self.input_overlay_b_font,
            self.combo_overlay_b_style,
            self.combo_overlay_b_layout,
            self.spin_overlay_b_box_width,
            self.spin_overlay_b_box_height
        )

        if not (overlay_a.is_enabled() or overlay_b.is_enabled()):
            QMessageBox.warning(self, "Missing Text", "Please enter overlay text for Video A or Video B.")
            return

        # Check FFmpeg again
        available, _ = check_ffmpeg_available()
        if not available:
            QMessageBox.critical(self, "FFmpeg Not Found", "FFmpeg is required but not available.")
            return

        output_base = Path(path_output)
        overlay_root = output_base / "overlay_only_outputs"
        jobs: list[OverlayJob] = []

        if path_a and overlay_a.is_enabled():
            videos_a = list(scan_video_files(Path(path_a)).values())
            videos_a.sort(key=lambda p: p.name.lower())
            for video in videos_a:
                output_path = overlay_root / "A" / f"{video.stem}_overlay{video.suffix}"
                jobs.append(OverlayJob(video, output_path, overlay_a))

        if path_b and overlay_b.is_enabled():
            videos_b = list(scan_video_files(Path(path_b)).values())
            videos_b.sort(key=lambda p: p.name.lower())
            for video in videos_b:
                output_path = overlay_root / "B" / f"{video.stem}_overlay{video.suffix}"
                jobs.append(OverlayJob(video, output_path, overlay_b))

        if not jobs:
            QMessageBox.information(
                self,
                "No Videos Found",
                "No videos were found or no overlay text was configured for the selected folders."
            )
            return

        # Setup worker
        self.worker = ProcessingWorker()
        self.worker_thread = QThread()
        self.worker.moveToThread(self.worker_thread)

        # Connect signals
        self.worker.progress.connect(self._on_progress)
        self.worker.log.connect(self._log)
        self.worker.finished.connect(self._on_finished)
        self.worker.single_complete.connect(self._on_single_complete)

        # UI state
        self.btn_start.setEnabled(False)
        self.btn_scan.setEnabled(False)
        self.btn_overlay_only.setEnabled(False)
        self.btn_cancel.setEnabled(True)
        self.progress_bar.setValue(0)
        self.log_text.clear()
        self._log(f"Overlay-only output: {overlay_root}")

        # Start processing
        crf = self.spin_crf.value()
        self.worker_thread.started.connect(
            lambda: self.worker.process_overlays(
                jobs,
                crf
            )
        )
        self.worker_thread.start()

    def _cancel_processing(self):
        """Request cancellation of processing."""
        if self.worker:
            self._log("Requesting cancellation...")
            self.worker.cancel()
            self.btn_cancel.setEnabled(False)

    def _on_progress(self, current: int, total: int):
        """Handle progress updates."""
        self.progress_label.setText(f"Processing {current}/{total}")
        self.progress_bar.setRange(0, total)
        self.progress_bar.setValue(current)

    def _on_single_complete(self, basename: str, success: bool):
        """Update table when a single item completes."""
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item and item.text() == basename:
                result_item = QTableWidgetItem("OK" if success else "FAILED")
                result_item.setBackground(Qt.green if success else Qt.red)
                self.table.setItem(row, 4, result_item)
                break

    def _on_finished(self, success: int, failed: int, skipped: int):
        """Handle processing completion."""
        self.btn_cancel.setEnabled(False)
        self.progress_label.setText(f"Complete: {success} success, {failed} failed, {skipped} skipped")

        # Cleanup thread
        if self.worker_thread:
            self.worker_thread.quit()
            self.worker_thread.wait()
            self.worker_thread = None
            self.worker = None

        self._update_button_states()

        # Show summary
        QMessageBox.information(
            self,
            "Processing Complete",
            f"Processing finished.\n\n"
            f"Success: {success}\n"
            f"Failed: {failed}\n"
            f"Skipped: {skipped}"
        )

    def _open_output_folder(self):
        """Open the output folder in file manager."""
        path = self.drop_zone_output.get_path()
        if path and os.path.isdir(path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def _log(self, message: str):
        """Add message to log panel."""
        self.log_text.append(message)
        # Auto-scroll to bottom
        scrollbar = self.log_text.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())


class UGCOverlayTab(QWidget):
    """Tab for UGC video processing with overlays and captions."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.videos: list[Path] = []
        self.worker: Optional[UGCProcessingWorker] = None
        self.worker_thread: Optional[QThread] = None
        self._setup_ui()

    def _setup_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setSpacing(10)

        # === Input Folder ===
        input_group = QGroupBox("Input Videos")
        input_layout = QHBoxLayout(input_group)

        self.drop_zone_input = DropZone("Video Folder")
        self.drop_zone_input.pathChanged.connect(self._on_input_changed)
        self.btn_browse_input = QPushButton("Browse...")
        self.btn_browse_input.clicked.connect(self._browse_input)

        input_layout.addWidget(self.drop_zone_input, 1)
        input_layout.addWidget(self.btn_browse_input)
        main_layout.addWidget(input_group)

        # === Output Folder ===
        output_group = QGroupBox("Output")
        output_layout = QHBoxLayout(output_group)

        self.drop_zone_output = DropZone("Output Folder")
        self.drop_zone_output.pathChanged.connect(self._update_button_states)
        self.btn_browse_output = QPushButton("Browse...")
        self.btn_browse_output.clicked.connect(self._browse_output)

        output_layout.addWidget(self.drop_zone_output, 1)
        output_layout.addWidget(self.btn_browse_output)
        main_layout.addWidget(output_group)

        # === Captions Settings ===
        captions_group = QGroupBox("Captions")
        captions_layout = QVBoxLayout(captions_group)

        # Enable/disable captions toggle
        self.check_enable_captions = QCheckBox("Enable Captions")
        self.check_enable_captions.setChecked(True)
        self.check_enable_captions.setToolTip("When enabled, transcribes audio and adds word-by-word captions")
        self.check_enable_captions.stateChanged.connect(self._on_captions_toggled)
        captions_layout.addWidget(self.check_enable_captions)

        # API Key row
        api_layout = QHBoxLayout()
        api_layout.addWidget(QLabel("AssemblyAI API Key:"))
        self.input_api_key = QLineEdit()
        self.input_api_key.setPlaceholderText("Enter your AssemblyAI API key...")
        self.input_api_key.setEchoMode(QLineEdit.Password)
        self.input_api_key.textChanged.connect(self._update_button_states)

        self.btn_show_key = QPushButton("Show")
        self.btn_show_key.setCheckable(True)
        self.btn_show_key.clicked.connect(self._toggle_api_key_visibility)

        api_layout.addWidget(self.input_api_key, 1)
        api_layout.addWidget(self.btn_show_key)
        captions_layout.addLayout(api_layout)

        main_layout.addWidget(captions_group)

        # === Overlay Settings ===
        overlay_group = QGroupBox("Overlay Settings")
        overlay_layout = QVBoxLayout(overlay_group)

        # add1.png position
        add1_layout = QHBoxLayout()
        add1_layout.addWidget(QLabel("add1.png position:"))
        add1_layout.addWidget(QLabel("X:"))
        self.spin_add1_x = QSpinBox()
        self.spin_add1_x.setRange(0, 9999)
        self.spin_add1_x.setValue(190)
        add1_layout.addWidget(self.spin_add1_x)
        add1_layout.addWidget(QLabel("Y:"))
        self.spin_add1_y = QSpinBox()
        self.spin_add1_y.setRange(0, 9999)
        self.spin_add1_y.setValue(890)
        add1_layout.addWidget(self.spin_add1_y)
        add1_layout.addStretch()
        overlay_layout.addLayout(add1_layout)

        # add2.mov opacity
        add2_layout = QHBoxLayout()
        add2_layout.addWidget(QLabel("add2.mov opacity:"))
        self.spin_add2_opacity = QDoubleSpinBox()
        self.spin_add2_opacity.setRange(0.0, 1.0)
        self.spin_add2_opacity.setSingleStep(0.05)
        self.spin_add2_opacity.setValue(0.5)
        self.spin_add2_opacity.setToolTip("1.0 = fully visible, 0.0 = invisible")
        add2_layout.addWidget(self.spin_add2_opacity)
        add2_layout.addWidget(QLabel("(0.0 - 1.0)"))
        add2_layout.addStretch()
        overlay_layout.addLayout(add2_layout)

        # CRF
        crf_layout = QHBoxLayout()
        crf_layout.addWidget(QLabel("CRF (Quality):"))
        self.spin_crf = QSpinBox()
        self.spin_crf.setRange(0, 51)
        self.spin_crf.setValue(18)
        self.spin_crf.setToolTip("Lower = better quality. 18 is visually lossless.")
        crf_layout.addWidget(self.spin_crf)
        crf_layout.addWidget(QLabel("(0-51, lower=better)"))
        crf_layout.addStretch()
        overlay_layout.addLayout(crf_layout)

        # Caption info
        caption_info = QLabel(
            "<b>Caption Style:</b> Futura Bold, White, No Border<br>"
            "<b>Display:</b> One word at a time, synced to audio<br>"
            "<b>Assets:</b> Using add1.png, add2.mov, ClipEnd.mov from /assets folder"
        )
        caption_info.setStyleSheet("color: #666; padding: 10px; background: #f5f5f5; border-radius: 4px;")
        overlay_layout.addWidget(caption_info)

        main_layout.addWidget(overlay_group)

        # === Action Buttons ===
        buttons_layout = QHBoxLayout()

        self.btn_scan = QPushButton("Scan Videos")
        self.btn_scan.clicked.connect(self._scan_videos)
        self.btn_scan.setMinimumHeight(35)

        self.btn_start = QPushButton("Start Processing")
        self.btn_start.clicked.connect(self._start_processing)
        self.btn_start.setEnabled(False)
        self.btn_start.setMinimumHeight(35)
        self.btn_start.setStyleSheet("QPushButton { background-color: #4caf50; color: white; font-weight: bold; }"
                                     "QPushButton:disabled { background-color: #ccc; color: #666; }")

        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.clicked.connect(self._cancel_processing)
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.setMinimumHeight(35)
        self.btn_cancel.setStyleSheet("QPushButton { background-color: #f44336; color: white; }")

        self.btn_open_output = QPushButton("Open Output")
        self.btn_open_output.clicked.connect(self._open_output_folder)
        self.btn_open_output.setEnabled(False)
        self.btn_open_output.setMinimumHeight(35)

        buttons_layout.addWidget(self.btn_scan)
        buttons_layout.addWidget(self.btn_start)
        buttons_layout.addWidget(self.btn_cancel)
        buttons_layout.addWidget(self.btn_open_output)
        main_layout.addLayout(buttons_layout)

        # === Video List and Log ===
        splitter = QSplitter(Qt.Vertical)

        # Video list
        list_container = QWidget()
        list_layout = QVBoxLayout(list_container)
        list_layout.setContentsMargins(0, 0, 0, 0)

        self.video_count_label = QLabel("No videos scanned")
        list_layout.addWidget(self.video_count_label)

        self.table = QTableWidget()
        self.table.setColumnCount(3)
        self.table.setHorizontalHeaderLabels(["Filename", "Duration", "Status"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        list_layout.addWidget(self.table)
        splitter.addWidget(list_container)

        # Log
        log_container = QWidget()
        log_layout = QVBoxLayout(log_container)
        log_layout.setContentsMargins(0, 0, 0, 0)

        progress_layout = QHBoxLayout()
        self.progress_label = QLabel("Ready")
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        progress_layout.addWidget(self.progress_label)
        progress_layout.addWidget(self.progress_bar, 1)
        log_layout.addLayout(progress_layout)

        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setStyleSheet("font-family: monospace; font-size: 11px;")
        log_layout.addWidget(self.log_text)
        splitter.addWidget(log_container)

        splitter.setSizes([250, 200])
        main_layout.addWidget(splitter, 1)

    def _browse_input(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Video Folder", "", QFileDialog.ShowDirsOnly)
        if folder:
            self.drop_zone_input.set_path(folder)

    def _browse_output(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Output Folder", "", QFileDialog.ShowDirsOnly)
        if folder:
            self.drop_zone_output.set_path(folder)

    def _toggle_api_key_visibility(self):
        if self.btn_show_key.isChecked():
            self.input_api_key.setEchoMode(QLineEdit.Normal)
            self.btn_show_key.setText("Hide")
        else:
            self.input_api_key.setEchoMode(QLineEdit.Password)
            self.btn_show_key.setText("Show")

    def _on_captions_toggled(self, state):
        """Enable/disable API key input based on captions toggle."""
        captions_enabled = self.check_enable_captions.isChecked()
        self.input_api_key.setEnabled(captions_enabled)
        self.btn_show_key.setEnabled(captions_enabled)
        self._update_button_states()

    def _on_input_changed(self, path: str):
        self._update_button_states()
        # Auto-scan when folder is selected
        self._scan_videos()

    def _update_button_states(self):
        has_input = self.drop_zone_input.get_path() is not None
        has_output = self.drop_zone_output.get_path() is not None
        has_api_key = len(self.input_api_key.text().strip()) > 0
        has_videos = len(self.videos) > 0
        captions_enabled = self.check_enable_captions.isChecked()

        # API key only required if captions are enabled
        api_key_ok = has_api_key or not captions_enabled

        self.btn_scan.setEnabled(has_input)
        self.btn_start.setEnabled(has_input and has_output and api_key_ok and has_videos)
        self.btn_open_output.setEnabled(has_output)

    def _scan_videos(self):
        input_path = self.drop_zone_input.get_path()
        if not input_path:
            return

        self.videos = scan_ugc_videos(Path(input_path))
        self.video_count_label.setText(f"{len(self.videos)} video(s) found")

        self.table.setRowCount(len(self.videos))
        for row, video in enumerate(self.videos):
            self.table.setItem(row, 0, QTableWidgetItem(video.name))
            # Duration would require ffprobe, show placeholder for now
            self.table.setItem(row, 1, QTableWidgetItem("-"))
            self.table.setItem(row, 2, QTableWidgetItem("Pending"))

        self._log(f"Found {len(self.videos)} videos in folder")
        self._update_button_states()

    def _start_processing(self):
        input_path = self.drop_zone_input.get_path()
        output_path = self.drop_zone_output.get_path()
        api_key = self.input_api_key.text().strip()
        enable_captions = self.check_enable_captions.isChecked()

        if not input_path or not output_path:
            QMessageBox.warning(self, "Missing Info", "Please select input and output folders.")
            return

        if enable_captions and not api_key:
            QMessageBox.warning(self, "Missing API Key", "Please enter an AssemblyAI API key for captions.")
            return

        if not self.videos:
            QMessageBox.warning(self, "No Videos", "Please scan for videos first.")
            return

        # Check assets
        add1_path = ASSETS_DIR / "add1.png"
        add2_path = ASSETS_DIR / "add2.mov"
        clip_end_path = ASSETS_DIR / "ClipEnd.mov"

        missing_assets = []
        if not add1_path.exists():
            missing_assets.append("add1.png")
        if not add2_path.exists():
            missing_assets.append("add2.mov")
        if not clip_end_path.exists():
            missing_assets.append("ClipEnd.mov")

        if missing_assets:
            QMessageBox.warning(
                self,
                "Missing Assets",
                f"Missing asset files in /assets folder:\n{', '.join(missing_assets)}"
            )
            return

        # Check FFmpeg
        available, _ = check_ffmpeg_available()
        if not available:
            QMessageBox.critical(self, "FFmpeg Not Found", "FFmpeg is required but not available.")
            return

        # Setup worker
        self.worker = UGCProcessingWorker()
        self.worker_thread = QThread()
        self.worker.moveToThread(self.worker_thread)

        # Connect signals
        self.worker.progress.connect(self._on_progress)
        self.worker.log.connect(self._log)
        self.worker.finished.connect(self._on_finished)
        self.worker.single_complete.connect(self._on_single_complete)

        # UI state
        self.btn_start.setEnabled(False)
        self.btn_scan.setEnabled(False)
        self.btn_cancel.setEnabled(True)
        self.progress_bar.setValue(0)
        self.log_text.clear()

        # Reset status column
        for row in range(self.table.rowCount()):
            self.table.setItem(row, 2, QTableWidgetItem("Pending"))

        # Get settings
        add1_x = self.spin_add1_x.value()
        add1_y = self.spin_add1_y.value()
        add2_opacity = self.spin_add2_opacity.value()
        crf = self.spin_crf.value()
        enable_captions = self.check_enable_captions.isChecked()

        # Start processing
        self.worker_thread.started.connect(
            lambda: self.worker.process(
                self.videos,
                Path(output_path),
                api_key,
                add1_path,
                add2_path,
                clip_end_path,
                add1_x,
                add1_y,
                add2_opacity,
                crf,
                enable_captions
            )
        )
        self.worker_thread.start()

    def _cancel_processing(self):
        if self.worker:
            self._log("Requesting cancellation...")
            self.worker.cancel()
            self.btn_cancel.setEnabled(False)

    def _on_progress(self, current: int, total: int):
        self.progress_label.setText(f"Processing {current}/{total}")
        self.progress_bar.setRange(0, total)
        self.progress_bar.setValue(current)

    def _on_single_complete(self, filename: str, success: bool):
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item and item.text() == filename:
                status_item = QTableWidgetItem("OK" if success else "FAILED")
                status_item.setBackground(Qt.green if success else Qt.red)
                self.table.setItem(row, 2, status_item)
                break

    def _on_finished(self, success: int, failed: int):
        self.btn_start.setEnabled(True)
        self.btn_scan.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        self.progress_label.setText(f"Complete: {success} success, {failed} failed")

        if self.worker_thread:
            self.worker_thread.quit()
            self.worker_thread.wait()
            self.worker_thread = None
            self.worker = None

        QMessageBox.information(
            self,
            "Processing Complete",
            f"UGC processing finished.\n\nSuccess: {success}\nFailed: {failed}"
        )

    def _open_output_folder(self):
        path = self.drop_zone_output.get_path()
        if path and os.path.isdir(path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def _log(self, message: str):
        self.log_text.append(message)
        scrollbar = self.log_text.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())


class MainWindow(QMainWindow):
    """Main application window with tabbed interface."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Reclip UGC Editor")
        self.setMinimumSize(950, 750)

        self._setup_ui()
        self._check_ffmpeg()

    def _setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(10, 10, 10, 10)

        # Create tab widget
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)

        # Add tabs
        self.ugc_tab = UGCOverlayTab()
        self.concat_tab = ConcatTab()

        self.tabs.addTab(self.ugc_tab, "UGC Overlay + Captions")
        self.tabs.addTab(self.concat_tab, "Video Concatenation")

        main_layout.addWidget(self.tabs)

    def _check_ffmpeg(self):
        """Check if FFmpeg is available."""
        available, message = check_ffmpeg_available()
        if not available:
            QMessageBox.critical(
                self,
                "FFmpeg Not Found",
                f"FFmpeg is required but was not found.\n\n"
                f"Error: {message}\n\n"
                f"Please install FFmpeg:\n"
                f"  macOS: brew install ffmpeg\n"
                f"  Ubuntu: sudo apt install ffmpeg\n"
                f"  Windows: Download from ffmpeg.org"
            )
        else:
            # Log to both tabs
            self.ugc_tab._log(f"FFmpeg detected: {message}")
            self.concat_tab._log(f"FFmpeg detected: {message}")


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    # Set application-wide stylesheet
    app.setStyleSheet("""
        QGroupBox {
            font-weight: bold;
            border: 1px solid #ccc;
            border-radius: 6px;
            margin-top: 10px;
            padding-top: 10px;
        }
        QGroupBox::title {
            subcontrol-origin: margin;
            left: 10px;
            padding: 0 5px;
        }
        QPushButton {
            padding: 8px 16px;
            border-radius: 4px;
        }
        QTableWidget {
            gridline-color: #ddd;
        }
        QHeaderView::section {
            background-color: #f0f0f0;
            padding: 4px;
            border: 1px solid #ddd;
            font-weight: bold;
        }
        QTabWidget::pane {
            border: 1px solid #ccc;
            border-radius: 4px;
        }
        QTabBar::tab {
            padding: 8px 20px;
            margin-right: 2px;
            border-top-left-radius: 4px;
            border-top-right-radius: 4px;
        }
        QTabBar::tab:selected {
            background: #4caf50;
            color: white;
            font-weight: bold;
        }
        QTabBar::tab:!selected {
            background: #e0e0e0;
        }
        QTabBar::tab:hover:!selected {
            background: #d0d0d0;
        }
    """)

    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
