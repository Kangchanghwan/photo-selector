#!/usr/bin/env python3
"""
Photo Curator - 대량 사진에서 원하는 수만큼 선별하는 도구
PyQt5 기반, 고해상도(15MB+) 사진 최적화
라운드별 선별 시스템
1장 뷰 / 그리드 뷰 전환 지원
다중 폴더 / 드래그앤드롭 / 하위 폴더 재귀 스캔
하단 썸네일 큐 + 동적 프리로딩 + 확대/축소/원본 보기

단축키:
  Space    = 선택 토글 (넘어가지 않음)
  →        = 다음 사진
  ←        = 이전 사진
  U        = 선택 토글
  G        = 뷰 모드 전환
  +/=      = 확대
  -        = 축소
  0        = 화면 맞춤
  F        = 원본 크기
  Ctrl+휠  = 확대/축소
"""

import sys
import os
import json
import shutil
from pathlib import Path
from datetime import datetime
from collections import OrderedDict

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QFileDialog, QProgressBar, QSpinBox,
    QMessageBox, QStackedWidget, QScrollArea,
    QGridLayout, QSizePolicy, QShortcut, QGroupBox,
    QSlider, QCheckBox, QListWidget, QListWidgetItem,
    QAbstractItemView, QGraphicsView, QGraphicsScene, QGraphicsPixmapItem,
    QGraphicsOpacityEffect
)
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QTimer, QRect, QSize, QRectF
from PyQt5.QtGui import (
    QPixmap, QImage, QKeySequence, QFont, QColor, QPainter,
    QPen, QBrush, QWheelEvent, QImageReader
)


SUPPORTED_FORMATS = {'.jpg', '.jpeg', '.png', '.bmp', '.tiff', '.tif', '.webp', '.heic', '.heif'}
THUMB_SIZE = 80          # 하단 큐 썸네일 크기
GRID_THUMB_SIZE = 400    # 그리드 뷰 썸네일 최대 크기


class FullImageLoader(QThread):
    # 이미지 로드가 완료되면 (경로, 결과 픽스맵)을 전달하는 시그널
    finished = pyqtSignal(str, QPixmap)

    def __init__(self, path):
        super().__init__()
        self.path = path
        self._is_cancelled = False

    def cancel(self):
        self._is_cancelled = True

    def run(self):
        if self._is_cancelled: return

        reader = QImageReader(self.path)
        reader.setAutoTransform(True)
        image = reader.read()

        if not image.isNull() and not self._is_cancelled:
            pixmap = QPixmap.fromImage(image)
            self.finished.emit(self.path, pixmap)

def scan_folder(folder, recursive=False):
    photos = []
    if recursive:
        for root, dirs, files in os.walk(folder):
            dirs[:] = [d for d in dirs if not d.startswith('.')]
            for f in sorted(files):
                if Path(f).suffix.lower() in SUPPORTED_FORMATS:
                    photos.append(os.path.join(root, f))
    else:
        try:
            entries = sorted(os.listdir(folder))
        except OSError:
            return photos
        for f in entries:
            if Path(f).suffix.lower() in SUPPORTED_FORMATS:
                full = os.path.join(folder, f)
                if os.path.isfile(full):
                    photos.append(full)
    return photos


def load_image_with_exif(path):
    """EXIF orientation을 적용하여 QImage를 로드"""
    reader = QImageReader(path)
    reader.setAutoTransform(True)
    return reader.read()


# ─────────────────────────────────────────────
# 백그라운드 썸네일 로더
# ─────────────────────────────────────────────
class ThumbLoaderWorker(QThread):
    """배치 단위로 썸네일을 로드하는 워커 (큐 + 그리드 공용)"""
    thumb_ready = pyqtSignal(str, QPixmap, QPixmap)  # path, small thumb, grid thumb
    batch_done = pyqtSignal()

    def __init__(self, paths, small_size=THUMB_SIZE, grid_size=GRID_THUMB_SIZE):
        super().__init__()
        self.paths = paths
        self.small_size = small_size
        self.grid_size = grid_size
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        for path in self.paths:
            if self._cancelled:
                return
            try:
                img = load_image_with_exif(path)
                if img.isNull():
                    continue
                small = img.scaled(
                    self.small_size, self.small_size,
                    Qt.KeepAspectRatio, Qt.SmoothTransformation
                )
                grid = img.scaled(
                    self.grid_size, self.grid_size,
                    Qt.KeepAspectRatio, Qt.SmoothTransformation
                )
                self.thumb_ready.emit(
                    path, QPixmap.fromImage(small), QPixmap.fromImage(grid)
                )
            except Exception:
                pass
        self.batch_done.emit()


# ─────────────────────────────────────────────
# 썸네일 캐시 (LRU)
# ─────────────────────────────────────────────
class ThumbnailCache:
    """메모리 기반 LRU 썸네일 캐시"""
    def __init__(self, max_size=500):
        self._cache = OrderedDict()
        self._max = max_size

    def get(self, path):
        if path in self._cache:
            self._cache.move_to_end(path)
            return self._cache[path]
        return None

    def put(self, path, pixmap):
        if path in self._cache:
            self._cache.move_to_end(path)
        else:
            if len(self._cache) >= self._max:
                self._cache.popitem(last=False)
            self._cache[path] = pixmap

    def has(self, path):
        return path in self._cache

    def clear(self):
        self._cache.clear()


# ─────────────────────────────────────────────
# 사진 뷰어 오버레이 컨테이너
# ─────────────────────────────────────────────
class PhotoOverlayContainer(QWidget):
    """
    사진 뷰어가 전체 영역을 차지하고, 컨트롤(버튼·큐·라벨)이
    반투명 오버레이로 그 위에 절대 좌표로 배치되는 컨테이너.
    resizeEvent 마다 자식 위젯들의 위치·크기를 재계산한다.
    """
    NAV_W, NAV_H = 48, 90        # 이전/다음 버튼

    def resizeEvent(self, event):
        super().resizeEvent(event)
        w, h = self.width(), self.height()

        # 이미지 뷰어: 컨테이너 전체
        iv = getattr(self, '_image_viewer', None)
        if iv:
            iv.setGeometry(0, 0, w, h)

        # 이전/다음 버튼: 컨테이너 좌우 중앙
        bw = self.NAV_W
        bh = min(self.NAV_H, max(44, h - 120))
        by = (h - bh) // 2
        bp = getattr(self, '_btn_prev', None)
        if bp:
            bp.setGeometry(10, by, bw, bh)
            bp.raise_()
        bn = getattr(self, '_btn_next', None)
        if bn:
            bn.setGeometry(w - bw - 10, by, bw, bh)
            bn.raise_()

        # 파일명 라벨: 좌상단
        il = getattr(self, '_info_label', None)
        if il:
            il.setGeometry(10, 10, w - 90, 24)
            il.raise_()

        # 줌 퍼센트 라벨: 우상단
        zl = getattr(self, '_zoom_label', None)
        if zl:
            zl.setGeometry(w - 68, 10, 60, 22)
            zl.raise_()


# ─────────────────────────────────────────────
# 드래그앤드롭 소스 리스트
# ─────────────────────────────────────────────
class DropZoneList(QListWidget):
    items_dropped = pyqtSignal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DropOnly)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self._base_style = """
            QListWidget {
                background-color: #16213e; color: #e0e0e0;
                border: 2px dashed #0f3460; border-radius: 8px;
                padding: 8px; font-size: 13px;
            }
            QListWidget::item { padding: 6px 4px; border-bottom: 1px solid #0f3460; }
            QListWidget::item:selected { background-color: #533483; }
        """
        self.setStyleSheet(self._base_style)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self.setStyleSheet(self._base_style.replace("dashed #0f3460", "solid #4ecca3"))

    def dragLeaveEvent(self, event):
        self.setStyleSheet(self._base_style)

    def dropEvent(self, event):
        self.setStyleSheet(self._base_style)
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.toLocalFile()]
        if paths:
            self.items_dropped.emit(paths)
            event.acceptProposedAction()


# ─────────────────────────────────────────────
# 하단 썸네일 큐 - 개별 아이템
# ─────────────────────────────────────────────
class QueueThumbnail(QLabel):
    clicked = pyqtSignal(int)

    def __init__(self, index, parent=None):
        super().__init__(parent)
        self.index = index
        self.is_selected = False
        self.is_current = False
        self.setAlignment(Qt.AlignCenter)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(THUMB_SIZE, THUMB_SIZE)
        self._pixmap_src = None
        self._update_style()

    def set_thumbnail(self, pixmap):
        self._pixmap_src = pixmap
        self._render()

    def set_state(self, is_current, is_selected):
        self.is_current = is_current
        self.is_selected = is_selected
        self._render()

    def _update_style(self):
        pass  # 스타일은 _render에서 QPainter로 직접 그림

    def _render(self):
        size = self.size()
        canvas = QPixmap(size)

        # 배경색: 현재 보고 있는 사진이면 밝게
        if self.is_current:
            canvas.fill(QColor("#2a2a4e"))
        else:
            canvas.fill(QColor("#0f0f23"))

        painter = QPainter(canvas)
        painter.setRenderHint(QPainter.Antialiasing)

        if self._pixmap_src and not self._pixmap_src.isNull():
            pad = 4
            scaled = self._pixmap_src.scaled(
                size.width() - pad * 2, size.height() - pad * 2,
                Qt.KeepAspectRatio, Qt.SmoothTransformation
            )
            x = (size.width() - scaled.width()) // 2
            y = (size.height() - scaled.height()) // 2
            painter.drawPixmap(x, y, scaled)
        else:
            # 로딩 중 표시
            painter.setPen(QColor("#555"))
            painter.drawText(canvas.rect(), Qt.AlignCenter, "...")

        # 현재 사진 표시 (상단 파란 바)
        if self.is_current:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(QColor("#e94560")))
            painter.drawRect(0, 0, size.width(), 3)

        # 선택됨 표시
        if self.is_selected:
            # 녹색 테두리
            painter.setPen(QPen(QColor("#4ecca3"), 3))
            painter.setBrush(Qt.NoBrush)
            painter.drawRect(1, 1, size.width() - 2, size.height() - 2)
            # 체크 뱃지
            bs = 18
            br = QRect(size.width() - bs - 2, 2, bs, bs)
            painter.setBrush(QBrush(QColor("#4ecca3")))
            painter.setPen(Qt.NoPen)
            painter.drawEllipse(br)
            painter.setPen(QPen(QColor("#1a1a2e"), 2))
            f = painter.font()
            f.setPixelSize(11)
            f.setBold(True)
            painter.setFont(f)
            painter.drawText(br, Qt.AlignCenter, "✓")

        painter.end()
        self.setPixmap(canvas)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self.index)


# ─────────────────────────────────────────────
# 확대/축소 가능한 이미지 뷰어
# ─────────────────────────────────────────────
class ZoomableImageView(QGraphicsView):
    """마우스 휠 확대/축소, 드래그 이동 지원"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.AnchorViewCenter)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setStyleSheet("background-color: #0f0f23; border: none; border-radius: 10px;")
        self.setMinimumHeight(300)

        self._pixmap_item = None
        self._current_pixmap = None
        self._zoom_level = 1.0

    def set_image(self, pixmap):
        """이미지 설정 및 화면 중앙 정렬"""
        self.scene().clear()
        if pixmap and not pixmap.isNull():
            self._pixmap_item = self.scene().addPixmap(pixmap)
            # 장면의 범위를 이미지 크기로 고정하여 중앙 정렬의 기준을 잡음
            self.setSceneRect(QRectF(pixmap.rect()))
            self.fit_in_view()
        else:
            self._pixmap_item = None
            self.setSceneRect(QRectF())

    def fit_in_view(self):
        """화면에 맞춤"""
        if self._pixmap_item:
            self.resetTransform()
            self.fitInView(self._pixmap_item, Qt.KeepAspectRatio)
            self._zoom_level = self.transform().m11()

    def show_original(self):
        """원본 크기 (1:1)"""
        if self._pixmap_item:
            self.resetTransform()
            self._zoom_level = 1.0

    def zoom_in(self):
        factor = 1.25
        self._zoom_level *= factor
        self.scale(factor, factor)

    def zoom_out(self):
        factor = 0.8
        self._zoom_level *= factor
        self.scale(factor, factor)

    def get_zoom_percent(self):
        return int(self._zoom_level * 100)

    def wheelEvent(self, event: QWheelEvent):
        if event.angleDelta().y() > 0:
            self.zoom_in()
        else:
            self.zoom_out()
        # 부모에게 줌 레벨 갱신 알림
        parent = self.parent()
        while parent:
            if isinstance(parent, PhotoCurator):
                parent._update_zoom_label()
                break
            parent = parent.parent()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._pixmap_item and self._zoom_level <= 1.0:
            self.fit_in_view()


# ─────────────────────────────────────────────
# 그리드용 클릭 가능한 썸네일
# ─────────────────────────────────────────────
class ClickableThumbnail(QLabel):
    clicked = pyqtSignal(int)
    double_clicked = pyqtSignal(int)

    def __init__(self, index, parent=None):
        super().__init__(parent)
        self.index = index
        self.is_selected = False
        self.setAlignment(Qt.AlignCenter)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(200, 200)
        self._pixmap_original = None

    def set_thumbnail(self, pixmap):
        self._pixmap_original = pixmap
        self._render()

    def set_selected(self, selected):
        self.is_selected = selected
        self._render()

    def _render(self):
        if self._pixmap_original is None:
            return
        size = self.size()
        canvas = QPixmap(size)
        canvas.fill(QColor("#0f0f23"))
        painter = QPainter(canvas)
        painter.setRenderHint(QPainter.Antialiasing)

        scaled = self._pixmap_original.scaled(
            size.width() - 8, size.height() - 8,
            Qt.KeepAspectRatio, Qt.SmoothTransformation
        )
        x = (size.width() - scaled.width()) // 2
        y = (size.height() - scaled.height()) // 2
        painter.drawPixmap(x, y, scaled)

        if self.is_selected:
            painter.setPen(QPen(QColor("#4ecca3"), 4))
            painter.setBrush(Qt.NoBrush)
            painter.drawRoundedRect(2, 2, size.width() - 4, size.height() - 4, 6, 6)
            bs = 28
            br = QRect(size.width() - bs - 6, 6, bs, bs)
            painter.setBrush(QBrush(QColor("#4ecca3")))
            painter.setPen(Qt.NoPen)
            painter.drawEllipse(br)
            painter.setPen(QPen(QColor("#1a1a2e"), 3))
            painter.drawText(br, Qt.AlignCenter, "✓")

        painter.end()
        self.setPixmap(canvas)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self.index)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.double_clicked.emit(self.index)


# ─────────────────────────────────────────────
# 메인 윈도우
# ─────────────────────────────────────────────
class PhotoCurator(QMainWindow):
    VIEW_SINGLE = 0
    VIEW_GRID = 1

    def __init__(self):
        super().__init__()
        self.setWindowTitle("📸 Photo Curator - 사진 선별 도구")
        self.setMinimumSize(1000, 700)
        self.resize(1300, 850)
        self.setAcceptDrops(True)
        self._full_loader = None  # 현재 실행 중인 고해상도 로더 저장
        self._pending_loaders = []  # 취소됐지만 아직 실행 중인 로더 (GC 방지용)

        self.source_entries = []
        self.all_photos = []
        self.current_round_photos = []
        self.selected_photos = []
        self.current_index = 0
        self.round_number = 1
        self.target_count = 33
        self.session_file = os.path.join(Path.home(), ".photo_curator_session.json")
        self.round_history = []
        self.current_view_mode = self.VIEW_SINGLE
        self._grid_widgets = []
        self._queue_widgets = []
        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.timeout.connect(self._on_resize_done)

        self._nav_debounce_timer = QTimer(self)
        self._nav_debounce_timer.setSingleShot(True)
        self._nav_debounce_timer.timeout.connect(self._on_nav_debounced)

        # 썸네일 캐시 & 로더
        self.thumb_cache = ThumbnailCache(max_size=1080)       # 큐용 (작은 썸네일)
        self.grid_thumb_cache = ThumbnailCache(max_size=1080)  # 그리드용 (큰 썸네일)
        self._thumb_loader = None

        self._apply_dark_theme()
        self._build_ui()
        self._setup_shortcuts()
        self._check_saved_session()

    def dragEnterEvent(self, event):
        if self.stack.currentIndex() == 0 and event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        if self.stack.currentIndex() != 0:
            return
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.toLocalFile()]
        if paths:
            self._add_dropped_paths(paths)
            event.acceptProposedAction()

    def _apply_dark_theme(self):
        self.setStyleSheet("""
            QMainWindow { background-color: #1a1a2e; }
            QLabel { color: #e0e0e0; }
            QPushButton {
                background-color: #16213e; color: #e0e0e0;
                border: 1px solid #0f3460; border-radius: 8px;
                padding: 10px 20px; font-size: 14px; font-weight: bold;
            }
            QPushButton:hover { background-color: #0f3460; }
            QPushButton:pressed { background-color: #533483; }
            QPushButton:disabled { background-color: #2a2a3e; color: #666; border-color: #333; }
            QPushButton:checked { background-color: #533483; border-color: #7c3aed; }
            QProgressBar {
                border: 1px solid #0f3460; border-radius: 5px;
                text-align: center; color: #e0e0e0; background-color: #16213e;
            }
            QProgressBar::chunk { background-color: #533483; border-radius: 5px; }
            QSpinBox {
                background-color: #16213e; color: #e0e0e0;
                border: 1px solid #0f3460; border-radius: 5px;
                padding: 5px; font-size: 14px;
            }
            QGroupBox {
                color: #e0e0e0; border: 1px solid #0f3460;
                border-radius: 8px; margin-top: 10px; padding-top: 15px; font-weight: bold;
            }
            QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px; }
            QCheckBox { color: #e0e0e0; font-size: 13px; spacing: 8px; }
            QCheckBox::indicator {
                width: 18px; height: 18px; border-radius: 4px;
                border: 1px solid #0f3460; background: #16213e;
            }
            QCheckBox::indicator:checked { background: #533483; border-color: #7c3aed; }
            QStatusBar { background-color: #16213e; color: #e0e0e0; }
            QScrollArea { border: none; background-color: #1a1a2e; }
            QScrollBar:vertical, QScrollBar:horizontal {
                background: #16213e; border-radius: 5px;
            }
            QScrollBar:vertical { width: 10px; }
            QScrollBar:horizontal { height: 10px; }
            QScrollBar::handle:vertical, QScrollBar::handle:horizontal {
                background: #533483; border-radius: 5px; min-height: 30px; min-width: 30px;
            }
            QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
            QSlider::groove:horizontal {
                border: 1px solid #0f3460; height: 6px;
                background: #16213e; border-radius: 3px;
            }
            QSlider::handle:horizontal {
                background: #533483; border: 1px solid #7c3aed;
                width: 16px; margin: -5px 0; border-radius: 8px;
            }
        """)

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        self.main_layout = QVBoxLayout(central)
        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.stack = QStackedWidget()
        self.main_layout.addWidget(self.stack)
        self._build_setup_page()
        self._build_curator_page()
        self._build_result_page()
        self.statusBar().showMessage("폴더 또는 파일을 추가하여 시작하세요")

    # ══════════════════════════════════
    # 설정 화면
    # ══════════════════════════════════
    def _build_setup_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setAlignment(Qt.AlignCenter)
        layout.setSpacing(15)

        title = QLabel("📸 Photo Curator")
        title.setFont(QFont("", 32, QFont.Bold))
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet("color: #e94560;")
        layout.addWidget(title)

        subtitle = QLabel("대량의 사진에서 원하는 수만큼 라운드별로 선별하세요")
        subtitle.setFont(QFont("", 14))
        subtitle.setAlignment(Qt.AlignCenter)
        subtitle.setStyleSheet("color: #999;")
        layout.addWidget(subtitle)
        layout.addSpacing(15)

        source_group = QGroupBox("사진 소스 (폴더/파일)")
        source_layout = QVBoxLayout(source_group)
        hint = QLabel("폴더나 사진 파일을 아래에 드래그하거나 버튼으로 추가하세요")
        hint.setStyleSheet("color: #888; font-size: 12px;")
        hint.setAlignment(Qt.AlignCenter)
        source_layout.addWidget(hint)

        self.source_list = DropZoneList()
        self.source_list.setMinimumHeight(140)
        self.source_list.setMaximumHeight(200)
        self.source_list.items_dropped.connect(self._add_dropped_paths)
        source_layout.addWidget(self.source_list)

        btn_bar = QHBoxLayout()
        for text, slot, style in [
            ("📁 폴더 추가", self._add_folder_dialog, ""),
            ("🖼 파일 추가", self._add_files_dialog, ""),
            ("🗑 선택 제거", self._remove_selected_sources,
             "QPushButton{background:#5c1a1a;border-color:#8b2500}QPushButton:hover{background:#8b2500}"),
            ("전체 초기화", self._clear_sources,
             "QPushButton{background:#3a1a1a;border-color:#5c2a2a;font-size:12px}QPushButton:hover{background:#5c2a2a}")
        ]:
            btn = QPushButton(text)
            btn.setFixedHeight(36)
            btn.clicked.connect(slot)
            if style:
                btn.setStyleSheet(style)
            btn_bar.addWidget(btn)
        source_layout.addLayout(btn_bar)

        option_bar = QHBoxLayout()
        self.chk_recursive = QCheckBox("하위 폴더 포함 (재귀 스캔)")
        self.chk_recursive.stateChanged.connect(lambda: self._update_photo_count())
        option_bar.addWidget(self.chk_recursive)
        option_bar.addStretch()
        self.photo_count_label = QLabel("사진: 0장")
        self.photo_count_label.setStyleSheet("color: #888; font-size: 14px; font-weight: bold;")
        option_bar.addWidget(self.photo_count_label)
        source_layout.addLayout(option_bar)
        layout.addWidget(source_group)

        target_group = QGroupBox("최종 선택할 사진 수")
        tl = QHBoxLayout(target_group)
        tl.addWidget(QLabel("최종 목표:"))
        self.target_spin = QSpinBox()
        self.target_spin.setRange(1, 9999)
        self.target_spin.setValue(33)
        self.target_spin.setSuffix(" 장")
        self.target_spin.setFixedWidth(120)
        tl.addWidget(self.target_spin)
        tl.addStretch()
        layout.addWidget(target_group)

        btn_row = QHBoxLayout()
        resume_col = QVBoxLayout()
        self.btn_resume = QPushButton("📂 이전 세션 이어서 하기")
        self.btn_resume.setFixedHeight(42)
        self.btn_resume.clicked.connect(self._resume_session)
        self.btn_resume.setStyleSheet(
            "QPushButton{background:#1a472a;border-color:#2d6a4f}QPushButton:hover{background:#2d6a4f}")
        resume_col.addWidget(self.btn_resume)
        self.session_info_label = QLabel("")
        self.session_info_label.setStyleSheet("color:#4ecca3;font-size:11px;")
        self.session_info_label.setAlignment(Qt.AlignCenter)
        resume_col.addWidget(self.session_info_label)
        btn_row.addLayout(resume_col)
        btn_row.addSpacing(20)
        self.btn_start = QPushButton("🚀 선별 시작!")
        self.btn_start.setFixedHeight(50)
        self.btn_start.setEnabled(False)
        self.btn_start.setStyleSheet(
            "QPushButton:enabled{background:#e94560;border-color:#e94560;font-size:18px}"
            "QPushButton:enabled:hover{background:#c81e45}")
        self.btn_start.clicked.connect(self._start_curation)
        btn_row.addWidget(self.btn_start, 1)
        layout.addLayout(btn_row)
        layout.addStretch()
        self.stack.addWidget(page)

    # ── 소스 관리 ──
    def _add_folder_dialog(self):
        folder = QFileDialog.getExistingDirectory(self, "사진 폴더 선택")
        if folder:
            self._add_source("folder", folder)

    def _add_files_dialog(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, "사진 파일 선택", "",
            "이미지 (*.jpg *.jpeg *.png *.bmp *.tiff *.tif *.webp *.heic *.heif);;모든 파일 (*)")
        for f in files:
            self._add_source("file", f)

    def _add_dropped_paths(self, paths):
        for p in paths:
            if os.path.isdir(p):
                self._add_source("folder", p)
            elif os.path.isfile(p) and Path(p).suffix.lower() in SUPPORTED_FORMATS:
                self._add_source("file", p)

    def _add_source(self, stype, path):
        for e in self.source_entries:
            if e["path"] == path:
                return
        self.source_entries.append({"type": stype, "path": path})
        if stype == "folder":
            cnt = len(scan_folder(path, self.chk_recursive.isChecked()))
            display = f"📁  {path}  ({cnt}장)"
        else:
            display = f"🖼  {os.path.basename(path)}"
        item = QListWidgetItem(display)
        item.setData(Qt.UserRole, path)
        self.source_list.addItem(item)
        self._update_photo_count()

    def _remove_selected_sources(self):
        for item in self.source_list.selectedItems():
            path = item.data(Qt.UserRole)
            self.source_entries = [e for e in self.source_entries if e["path"] != path]
            self.source_list.takeItem(self.source_list.row(item))
        self._update_photo_count()

    def _clear_sources(self):
        self.source_entries.clear()
        self.source_list.clear()
        self._update_photo_count()

    def _collect_all_photos(self):
        photos, seen = [], set()
        recursive = self.chk_recursive.isChecked()
        for entry in self.source_entries:
            if entry["type"] == "folder":
                for p in scan_folder(entry["path"], recursive):
                    if p not in seen:
                        seen.add(p)
                        photos.append(p)
            else:
                p = entry["path"]
                if p not in seen and os.path.isfile(p):
                    seen.add(p)
                    photos.append(p)
        return sorted(photos)

    def _update_photo_count(self):
        count = len(self._collect_all_photos())
        self.photo_count_label.setText(f"사진: {count}장")
        self.btn_start.setEnabled(count > 0)
        self.photo_count_label.setStyleSheet(
            f"color:{'#4ecca3' if count > 0 else '#888'};font-size:14px;font-weight:bold")

    # ══════════════════════════════════
    # 선별 화면
    # ══════════════════════════════════
    def _build_curator_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(3)

        # ── 상단 정보 바 (compact) ──
        info_bar = QHBoxLayout()
        info_bar.setSpacing(8)
        self.round_label = QLabel("라운드 1")
        self.round_label.setFont(QFont("", 13, QFont.Bold))
        self.round_label.setStyleSheet("color: #e94560;")
        info_bar.addWidget(self.round_label)
        info_bar.addStretch()

        self.btn_view_single = QPushButton("🖼 1장")
        self.btn_view_single.setCheckable(True)
        self.btn_view_single.setChecked(True)
        self.btn_view_single.setFixedHeight(26)
        self.btn_view_single.setStyleSheet("font-size:11px;padding:2px 10px")
        self.btn_view_single.clicked.connect(lambda: self._switch_view_mode(self.VIEW_SINGLE))
        info_bar.addWidget(self.btn_view_single)

        self.btn_view_grid = QPushButton("▦ 그리드")
        self.btn_view_grid.setCheckable(True)
        self.btn_view_grid.setFixedHeight(26)
        self.btn_view_grid.setStyleSheet("font-size:11px;padding:2px 10px")
        self.btn_view_grid.clicked.connect(lambda: self._switch_view_mode(self.VIEW_GRID))
        info_bar.addWidget(self.btn_view_grid)

        info_bar.addSpacing(10)
        self.progress_label = QLabel("0 / 0")
        self.progress_label.setFont(QFont("", 12))
        info_bar.addWidget(self.progress_label)
        info_bar.addSpacing(8)
        self.selected_label = QLabel("선택: 0장")
        self.selected_label.setFont(QFont("", 12))
        self.selected_label.setStyleSheet("color: #4ecca3;")
        info_bar.addWidget(self.selected_label)

        info_bar.addSpacing(10)
        self.btn_toggle = QPushButton("♡ 선택 (Space)")
        self.btn_toggle.setFixedHeight(26)
        self.btn_toggle.setStyleSheet(
            "QPushButton{background:#27ae60;color:white;border:1px solid #2ecc71;"
            "border-radius:6px;font-size:12px;font-weight:bold;padding:2px 14px}"
            "QPushButton:hover{background:#2ecc71}")
        self.btn_toggle.clicked.connect(self._toggle_select)
        info_bar.addWidget(self.btn_toggle)

        info_bar.addSpacing(10)
        self.btn_finish_round = QPushButton("라운드 마치기 →")
        self.btn_finish_round.clicked.connect(self._finish_round)
        self.btn_finish_round.setFixedHeight(26)
        self.btn_finish_round.setStyleSheet(
            "QPushButton{background:#533483;border-color:#7c3aed;font-size:11px;padding:2px 12px}"
            "QPushButton:hover{background:#7c3aed}")
        info_bar.addWidget(self.btn_finish_round)
        layout.addLayout(info_bar)

        self.curator_progress = QProgressBar()
        self.curator_progress.setFixedHeight(4)
        self.curator_progress.setTextVisible(False)
        self.curator_progress.setStyleSheet(
            "QProgressBar{border:none;background:#16213e;border-radius:2px}"
            "QProgressBar::chunk{background:#e94560;border-radius:2px}")
        layout.addWidget(self.curator_progress)

        # ── 뷰 스택 ──
        self.view_stack = QStackedWidget()
        layout.addWidget(self.view_stack, 1)

        # ── 1장 뷰: PhotoOverlayContainer ──
        container = PhotoOverlayContainer()

        # 이미지 뷰어 (컨테이너 전체 채움, 레이아웃 없이 절대 좌표)
        self.image_viewer = ZoomableImageView(container)
        container._image_viewer = self.image_viewer

        # 파일명 오버레이 (좌상단)
        self.filename_label = QLabel("", container)
        self.filename_label.setFont(QFont("", 10))
        self.filename_label.setStyleSheet(
            "color:#ddd; background-color:rgba(0,0,0,150);"
            "border-radius:4px; padding:2px 8px;")
        self.filename_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        container._info_label = self.filename_label

        # 줌 퍼센트 오버레이 (우상단)
        self.zoom_label = QLabel("100%", container)
        self.zoom_label.setFont(QFont("", 9))
        self.zoom_label.setStyleSheet(
            "color:#bbb; background-color:rgba(0,0,0,150);"
            "border-radius:4px; padding:2px 6px;")
        self.zoom_label.setAlignment(Qt.AlignCenter)
        container._zoom_label = self.zoom_label

        # source_path_label: 숨김 (하위 호환용)
        self.source_path_label = QLabel("", container)
        self.source_path_label.hide()

        # 이전 버튼 (좌측 오버레이)
        self.btn_prev = QPushButton("◀", container)
        self.btn_prev.setStyleSheet(
            "QPushButton{background:rgba(0,0,0,160);color:rgba(255,255,255,200);"
            "border:1px solid rgba(255,255,255,50);border-radius:6px;font-size:20px}"
            "QPushButton:hover{background:rgba(60,60,120,220);color:white}")
        self.btn_prev.clicked.connect(self._go_previous)
        eff_prev = QGraphicsOpacityEffect()
        eff_prev.setOpacity(0.75)
        self.btn_prev.setGraphicsEffect(eff_prev)
        container._btn_prev = self.btn_prev

        # 다음 버튼 (우측 오버레이)
        self.btn_next = QPushButton("▶", container)
        self.btn_next.setStyleSheet(
            "QPushButton{background:rgba(0,0,0,160);color:rgba(255,255,255,200);"
            "border:1px solid rgba(255,255,255,50);border-radius:6px;font-size:20px}"
            "QPushButton:hover{background:rgba(60,60,120,220);color:white}")
        self.btn_next.clicked.connect(self._go_next)
        eff_next = QGraphicsOpacityEffect()
        eff_next.setOpacity(0.75)
        self.btn_next.setGraphicsEffect(eff_next)
        container._btn_next = self.btn_next

        # 썸네일 큐 (별도 영역 - 사진 아래 고정)
        queue_container = QWidget()
        queue_container.setFixedHeight(THUMB_SIZE + 18)
        queue_container.setStyleSheet("background-color: #0d0d1f;")
        ql = QHBoxLayout(queue_container)
        ql.setContentsMargins(4, 4, 4, 4)
        ql.setSpacing(0)

        self.queue_scroll = QScrollArea()
        self.queue_scroll.setWidgetResizable(True)
        self.queue_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.queue_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.queue_scroll.setStyleSheet(
            "QScrollArea{background:transparent;border:none}"
            "QScrollBar:horizontal{height:4px;background:transparent}"
            "QScrollBar::handle:horizontal{background:#444;border-radius:2px}")

        self.queue_widget = QWidget()
        self.queue_widget.setStyleSheet("background:transparent;")
        self.queue_layout = QHBoxLayout(self.queue_widget)
        self.queue_layout.setContentsMargins(4, 0, 4, 0)
        self.queue_layout.setSpacing(4)
        self.queue_scroll.setWidget(self.queue_widget)

        ql.addWidget(self.queue_scroll)

        self.view_stack.addWidget(container)
        self.queue_container = queue_container
        layout.addWidget(queue_container)

        # ── 그리드 뷰 ──
        grid_container = QWidget()
        gl = QVBoxLayout(grid_container)
        gl.setContentsMargins(0, 0, 0, 0)
        gl.setSpacing(5)

        size_bar = QHBoxLayout()
        size_bar.addWidget(QLabel("크기:"))
        self.grid_size_slider = QSlider(Qt.Horizontal)
        self.grid_size_slider.setRange(120, 550)
        self.grid_size_slider.setValue(200)
        self.grid_size_slider.setFixedWidth(200)
        self.grid_size_slider.valueChanged.connect(self._on_grid_size_changed)
        size_bar.addWidget(self.grid_size_slider)
        self.grid_size_label = QLabel("200px")
        self.grid_size_label.setFixedWidth(50)
        size_bar.addWidget(self.grid_size_label)
        size_bar.addSpacing(20)

        for text, mode in [("전체", "all"), ("♥ 선택됨만", "selected"), ("미선택만", "unselected")]:
            btn = QPushButton(text)
            btn.setCheckable(True)
            btn.setChecked(mode == "all")
            btn.setFixedHeight(30)
            btn.setStyleSheet("font-size:11px;padding:4px 10px")
            btn.clicked.connect(lambda _, m=mode: self._filter_grid(m))
            size_bar.addWidget(btn)
            setattr(self, f"btn_show_{'all' if mode == 'all' else mode}", btn)
        size_bar.addStretch()
        gl.addLayout(size_bar)

        self.grid_scroll = QScrollArea()
        self.grid_scroll.setWidgetResizable(True)
        self.grid_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.grid_widget = QWidget()
        self.grid_layout = QGridLayout(self.grid_widget)
        self.grid_layout.setSpacing(8)
        self.grid_layout.setContentsMargins(8, 8, 8, 8)
        self.grid_scroll.setWidget(self.grid_widget)
        gl.addWidget(self.grid_scroll, 1)
        self.view_stack.addWidget(grid_container)

        self.stack.addWidget(page)

    # ══════════════════════════════════
    # 결과 화면
    # ══════════════════════════════════
    def _build_result_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setAlignment(Qt.AlignCenter)
        layout.setSpacing(20)

        done_label = QLabel("🎉 선별 완료!")
        done_label.setFont(QFont("", 28, QFont.Bold))
        done_label.setAlignment(Qt.AlignCenter)
        done_label.setStyleSheet("color: #4ecca3;")
        layout.addWidget(done_label)

        self.result_info = QLabel("")
        self.result_info.setFont(QFont("", 14))
        self.result_info.setAlignment(Qt.AlignCenter)
        self.result_info.setWordWrap(True)
        layout.addWidget(self.result_info)
        layout.addSpacing(20)

        self.btn_export = QPushButton("📤 선택한 사진 내보내기 (복사)")
        self.btn_export.setFixedSize(300, 50)
        self.btn_export.clicked.connect(self._export_photos)
        self.btn_export.setStyleSheet(
            "QPushButton{background:#4ecca3;color:#1a1a2e;border-color:#4ecca3;font-size:16px}"
            "QPushButton:hover{background:#3dbb8f}")
        layout.addWidget(self.btn_export, alignment=Qt.AlignCenter)

        btn_restart = QPushButton("🔄 처음부터 다시 하기")
        btn_restart.setFixedSize(250, 40)
        btn_restart.clicked.connect(self._restart)
        layout.addWidget(btn_restart, alignment=Qt.AlignCenter)
        layout.addStretch()
        self.stack.addWidget(page)

    # ── 단축키 ──
    def _setup_shortcuts(self):
        QShortcut(QKeySequence(Qt.Key_Space), self, self._toggle_select)
        QShortcut(QKeySequence(Qt.Key_U), self, self._toggle_select)
        QShortcut(QKeySequence(Qt.Key_Right), self, self._go_next)
        QShortcut(QKeySequence(Qt.Key_Left), self, self._go_previous)
        QShortcut(QKeySequence(Qt.Key_G), self, self._toggle_view_mode)
        QShortcut(QKeySequence(Qt.Key_1), self, lambda: self._switch_view_mode(self.VIEW_SINGLE))
        QShortcut(QKeySequence(Qt.Key_2), self, lambda: self._switch_view_mode(self.VIEW_GRID))
        QShortcut(QKeySequence(Qt.Key_Plus), self, self._zoom_in)
        QShortcut(QKeySequence(Qt.Key_Equal), self, self._zoom_in)
        QShortcut(QKeySequence(Qt.Key_Minus), self, self._zoom_out)
        QShortcut(QKeySequence(Qt.Key_0), self, self._zoom_fit)
        QShortcut(QKeySequence(Qt.Key_F), self, self._zoom_original)

    # ═══════════════════════════════════
    # 줌 컨트롤
    # ═══════════════════════════════════
    def _zoom_in(self):
        if self.stack.currentIndex() == 1 and self.current_view_mode == self.VIEW_SINGLE:
            self.image_viewer.zoom_in()
            self._update_zoom_label()

    def _zoom_out(self):
        if self.stack.currentIndex() == 1 and self.current_view_mode == self.VIEW_SINGLE:
            self.image_viewer.zoom_out()
            self._update_zoom_label()

    def _zoom_fit(self):
        if self.stack.currentIndex() == 1 and self.current_view_mode == self.VIEW_SINGLE:
            self.image_viewer.fit_in_view()
            self._update_zoom_label()

    def _zoom_original(self):
        if self.stack.currentIndex() == 1 and self.current_view_mode == self.VIEW_SINGLE:
            self.image_viewer.show_original()
            self._update_zoom_label()

    def _update_zoom_label(self):
        pct = self.image_viewer.get_zoom_percent()
        self.zoom_label.setText(f"{pct}%")

    # ═══════════════════════════════════
    # 뷰 모드 전환
    # ═══════════════════════════════════
    def _toggle_view_mode(self):
        if self.stack.currentIndex() != 1:
            return
        self._switch_view_mode(
            self.VIEW_GRID if self.current_view_mode == self.VIEW_SINGLE else self.VIEW_SINGLE)

    def _switch_view_mode(self, mode):
        if self.stack.currentIndex() != 1:
            return
        self.current_view_mode = mode
        self.view_stack.setCurrentIndex(mode)
        self.btn_view_single.setChecked(mode == self.VIEW_SINGLE)
        self.btn_view_grid.setChecked(mode == self.VIEW_GRID)
        is_single = mode == self.VIEW_SINGLE
        self.btn_toggle.setVisible(is_single)
        self.queue_container.setVisible(is_single)
        if mode == self.VIEW_GRID:
            self._populate_grid()
        else:
            self._show_current_photo()
        self.statusBar().showMessage(
            "1장 뷰  |  Space: 선택  →: 다음  ←: 이전  +/-: 줌  0: 맞춤  F: 원본  G: 그리드"
            if mode == self.VIEW_SINGLE else
            "그리드 뷰  |  클릭: 선택 토글  더블클릭: 1장 뷰  G: 1장 뷰")

    # ═══════════════════════════════════
    # 하단 썸네일 큐
    # ═══════════════════════════════════
    def _build_queue(self):
        """큐 위젯 생성 (현재 라운드 사진 전체)"""
        self._clear_queue()
        total = len(self.current_round_photos)
        for i in range(total):
            tw = QueueThumbnail(i)
            tw.clicked.connect(self._on_queue_click)
            # 캐시에 있으면 바로 표시
            path = self.current_round_photos[i]
            cached = self.thumb_cache.get(path)
            if cached:
                tw.set_thumbnail(cached)
            tw.set_state(i == self.current_index, path in self.selected_photos)
            self.queue_layout.addWidget(tw)
            self._queue_widgets.append(tw)

    def _clear_queue(self):
        for w in self._queue_widgets:
            self.queue_layout.removeWidget(w)
            w.deleteLater()
        self._queue_widgets.clear()

    def _update_queue_states(self):
        """큐 위젯들의 선택/현재 상태만 갱신"""
        for w in self._queue_widgets:
            if w.index < len(self.current_round_photos):
                path = self.current_round_photos[w.index]
                w.set_state(w.index == self.current_index, path in self.selected_photos)

    def _scroll_queue_to_current(self):
        """현재 사진이 큐 가운데에 오도록 스크롤"""
        def do_scroll():
            if self.current_index < len(self._queue_widgets):
                widget = self._queue_widgets[self.current_index]
                widget_center_x = widget.x() + widget.width() // 2
                viewport_half = self.queue_scroll.viewport().width() // 2
                scroll_x = widget_center_x - viewport_half
                self.queue_scroll.horizontalScrollBar().setValue(scroll_x)
        QTimer.singleShot(0, do_scroll)

    def _on_queue_click(self, index):
        """큐 썸네일 클릭 → 해당 사진으로 이동"""
        self.current_index = index
        self._show_current_photo()

    # ═══════════════════════════════════
    # 전체 썸네일 프리로드 (현재 위치 가까운 순)
    # ═══════════════════════════════════
    def _sorted_indices_by_proximity(self, center_idx):
        """center_idx 기준으로 가까운 인덱스부터 정렬하여 반환"""
        total = len(self.current_round_photos)
        indices = []
        for offset in range(total):
            for idx in (center_idx + offset, center_idx - offset):
                if 0 <= idx < total and idx not in indices:
                    indices.append(idx)
            if len(indices) >= total:
                break
        return indices

    def _preload_all_thumbs(self, center_idx):
        """전체 사진 썸네일을 현재 위치 기준 가까운 순으로 프리로드"""
        if not self.current_round_photos:
            return

        paths_to_load = []
        for i in self._sorted_indices_by_proximity(center_idx):
            path = self.current_round_photos[i]
            if not self.thumb_cache.has(path):
                paths_to_load.append(path)

        if not paths_to_load:
            return

        # 이전 로더가 있으면 취소 후 안전하게 정리 (_pending_loaders로 참조 유지)
        if self._thumb_loader and self._thumb_loader.isRunning():
            self._thumb_loader.cancel()
            try:
                self._thumb_loader.thumb_ready.disconnect()
                self._thumb_loader.batch_done.disconnect()
            except TypeError:
                pass
            old_thumb = self._thumb_loader
            self._pending_loaders.append(old_thumb)
            def _cleanup_thumb(old_thumb=old_thumb):
                try:
                    self._pending_loaders.remove(old_thumb)
                except ValueError:
                    pass
                old_thumb.deleteLater()
            old_thumb.finished.connect(lambda: _cleanup_thumb())
            self._thumb_loader = None

        self._thumb_loader = ThumbLoaderWorker(paths_to_load)
        self._thumb_loader.thumb_ready.connect(self._on_thumb_loaded)
        self._thumb_loader.start()

    def _on_thumb_loaded(self, path, small_pixmap, grid_pixmap):
        """썸네일 로드 완료 콜백"""
        self.thumb_cache.put(path, small_pixmap)
        self.grid_thumb_cache.put(path, grid_pixmap)
        # 큐 위젯에 반영
        try:
            idx = self.current_round_photos.index(path)
        except ValueError:
            return
        if idx < len(self._queue_widgets):
            self._queue_widgets[idx].set_thumbnail(small_pixmap)
            self._queue_widgets[idx].set_state(
                idx == self.current_index,
                path in self.selected_photos
            )
        # 그리드 뷰 열려 있으면 해당 위젯에도 반영
        if self.current_view_mode == self.VIEW_GRID:
            for w in self._grid_widgets:
                if isinstance(w, ClickableThumbnail) and w.index == idx:
                    w.set_thumbnail(grid_pixmap)
                    break

    # ═══════════════════════════════════
    # 그리드 뷰
    # ═══════════════════════════════════
    def _populate_grid(self, filter_mode=None):
        if filter_mode is None:
            filter_mode = self._get_current_filter()
        self._clear_grid()

        if filter_mode == "selected":
            photos = [(i, p) for i, p in enumerate(self.current_round_photos) if p in self.selected_photos]
        elif filter_mode == "unselected":
            photos = [(i, p) for i, p in enumerate(self.current_round_photos) if p not in self.selected_photos]
        else:
            photos = list(enumerate(self.current_round_photos))

        if not photos:
            lbl = QLabel("표시할 사진이 없습니다")
            lbl.setAlignment(Qt.AlignCenter)
            lbl.setStyleSheet("color:#666;font-size:16px")
            self.grid_layout.addWidget(lbl, 0, 0)
            self._grid_widgets.append(lbl)
            return

        ts = self.grid_size_slider.value()
        sw = self.grid_scroll.viewport().width() - 20
        cols = max(1, sw // (ts + 8))

        for gi, (ri, pp) in enumerate(photos):
            tw = ClickableThumbnail(ri)
            tw.setFixedSize(ts, ts)
            tw.clicked.connect(self._on_grid_click)
            tw.double_clicked.connect(self._on_grid_dblclick)
            cached = self.grid_thumb_cache.get(pp)
            if cached:
                tw.set_thumbnail(cached)
            tw.set_selected(pp in self.selected_photos)
            self.grid_layout.addWidget(tw, gi // cols, gi % cols)
            self._grid_widgets.append(tw)

    def _clear_grid(self):
        for w in self._grid_widgets:
            self.grid_layout.removeWidget(w)
            w.deleteLater()
        self._grid_widgets.clear()
        while self.grid_layout.count():
            item = self.grid_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _on_grid_click(self, index):
        if index >= len(self.current_round_photos):
            return
        photo = self.current_round_photos[index]
        if photo in self.selected_photos:
            self.selected_photos.remove(photo)
        else:
            self.selected_photos.append(photo)
        for w in self._grid_widgets:
            if isinstance(w, ClickableThumbnail) and w.index == index:
                w.set_selected(photo in self.selected_photos)
                break
        self._update_curator_ui()
        self._save_session()

    def _on_grid_dblclick(self, index):
        self.current_index = index
        self._switch_view_mode(self.VIEW_SINGLE)

    def _on_grid_size_changed(self, value):
        self.grid_size_label.setText(f"{value}px")
        self._resize_timer.start(300)

    def _filter_grid(self, mode):
        self.btn_show_all.setChecked(mode == "all")
        self.btn_show_selected.setChecked(mode == "selected")
        self.btn_show_unselected.setChecked(mode == "unselected")
        self._populate_grid(filter_mode=mode)

    def _get_current_filter(self):
        if self.btn_show_selected.isChecked():
            return "selected"
        if self.btn_show_unselected.isChecked():
            return "unselected"
        return "all"

    # ═══════════════════════════════════
    # 세션
    # ═══════════════════════════════════
    def _save_session(self):
        session = {
            "source_entries": self.source_entries,
            "target_count": self.target_count,
            "round_number": self.round_number,
            "current_index": self.current_index,
            "current_round_photos": self.current_round_photos,
            "selected_photos": self.selected_photos,
            "round_history": self.round_history,
            "saved_at": datetime.now().isoformat()
        }
        try:
            with open(self.session_file, 'w', encoding='utf-8') as f:
                json.dump(session, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _check_saved_session(self):
        """앱 시작 시 저장된 세션이 있으면 버튼에 정보를 표시한다."""
        if not os.path.exists(self.session_file):
            self.btn_resume.setEnabled(False)
            self.session_info_label.setText("저장된 세션 없음")
            self.session_info_label.setStyleSheet("color:#666;font-size:11px;")
            return
        try:
            with open(self.session_file, 'r', encoding='utf-8') as f:
                s = json.load(f)
            saved_at = s.get("saved_at", "")
            round_num = s.get("round_number", 1)
            total = len(s.get("current_round_photos", []))
            selected = len(s.get("selected_photos", []))
            idx = s.get("current_index", 0)
            # 날짜/시간 포맷
            try:
                dt = datetime.fromisoformat(saved_at)
                time_str = dt.strftime("%m/%d %H:%M")
            except Exception:
                time_str = saved_at[:16] if saved_at else "?"
            self.session_info_label.setText(
                f"라운드 {round_num} | {idx+1}/{total}장 진행 | 선택 {selected}장 | 저장: {time_str}")
            self.session_info_label.setStyleSheet("color:#4ecca3;font-size:11px;")
            self.btn_resume.setEnabled(True)
        except Exception:
            self.btn_resume.setEnabled(False)
            self.session_info_label.setText("세션 파일 읽기 실패")
            self.session_info_label.setStyleSheet("color:#e74c3c;font-size:11px;")

    def _resume_session(self):
        """자동 저장된 세션 파일에서 바로 불러온다."""
        if not os.path.exists(self.session_file):
            QMessageBox.information(self, "세션 없음", "저장된 세션이 없습니다.")
            return
        try:
            with open(self.session_file, 'r', encoding='utf-8') as f:
                s = json.load(f)
            self.source_entries = s.get("source_entries", [])
            self.target_count = s["target_count"]
            self.round_number = s["round_number"]
            self.current_index = s["current_index"]
            self.current_round_photos = s["current_round_photos"]
            self.selected_photos = s["selected_photos"]
            self.round_history = s.get("round_history", [])
            self.target_spin.setValue(self.target_count)
            self.thumb_cache.clear()
            self.grid_thumb_cache.clear()
            self._build_queue()
            self._preload_all_thumbs(self.current_index)
            self._switch_to_curator()
        except Exception as e:
            QMessageBox.critical(self, "오류", f"세션 불러오기 실패:\n{e}")

    # ═══════════════════════════════════
    # 선별 시작
    # ═══════════════════════════════════
    def _start_curation(self):
        self.target_count = self.target_spin.value()
        self.all_photos = self._collect_all_photos()
        if not self.all_photos:
            QMessageBox.warning(self, "사진 없음", "추가된 소스에서 사진을 찾을 수 없습니다.")
            return
        if self.target_count >= len(self.all_photos):
            QMessageBox.warning(self, "설정 오류",
                f"목표({self.target_count})가 전체({len(self.all_photos)}) 이상입니다.")
            return
        self.round_number = 1
        self.current_index = 0
        self.current_round_photos = list(self.all_photos)
        self.selected_photos = []
        self.round_history = []
        self.thumb_cache.clear()
        self.grid_thumb_cache.clear()
        self._build_queue()
        self._preload_all_thumbs(0)
        self._switch_to_curator()

    # ═══════════════════════════════════
    # 선별 화면 로직
    # ═══════════════════════════════════
    def _switch_to_curator(self):
        self.stack.setCurrentIndex(1)
        self._update_curator_ui()
        if self.current_view_mode == self.VIEW_SINGLE:
            self._show_current_photo()
        else:
            self._populate_grid()

    def _update_curator_ui(self):
        total = len(self.current_round_photos)
        self.round_label.setText(f"라운드 {self.round_number}  ({total}장 → 목표 {self.target_count}장)")
        if self.current_view_mode == self.VIEW_SINGLE:
            self.progress_label.setText(f"{min(self.current_index + 1, total)} / {total}")
        else:
            self.progress_label.setText(f"총 {total}장")
        self.selected_label.setText(f"선택: {len(self.selected_photos)}장")
        self.curator_progress.setMaximum(max(total, 1))
        self.curator_progress.setValue(min(self.current_index + 1, total))

    def _show_current_photo(self):
        if not self.current_round_photos:
            return

        # 인덱스 방어 로직
        if self.current_index >= len(self.current_round_photos):
            self.current_index = len(self.current_round_photos) - 1

        photo_path = self.current_round_photos[self.current_index]

        # ---------------------------------------------------------
        # 1단계: 즉시성 확보 (썸네일 우선 표시)
        # ---------------------------------------------------------
        # 이미 로드되어 있는 그리드용 썸네일(큰 썸네일)을 먼저 보여줍니다.
        cached_thumb = self.grid_thumb_cache.get(photo_path)
        if cached_thumb:
            self.image_viewer.set_image(cached_thumb)
        else:
            # 썸네일 캐시조차 없다면 아주 작은 큐 썸네일이라도 시도
            small_thumb = self.thumb_cache.get(photo_path)
            self.image_viewer.set_image(small_thumb if small_thumb else None)

        # ---------------------------------------------------------
        # 2단계: 기존 로딩 작업 취소 및 정리
        # ---------------------------------------------------------
        if self._full_loader and self._full_loader.isRunning():
            self._full_loader.cancel()
            try:
                self._full_loader.finished.disconnect()
            except TypeError:
                pass
            # 실행 중인 스레드가 종료될 때까지 Python 참조를 유지해야 함.
            # self._full_loader = None 하면 GC가 즉시 소멸시켜 QThread fatal 발생.
            # _pending_loaders에 보관하다가 finished 후 안전하게 정리.
            old = self._full_loader
            self._pending_loaders.append(old)
            def _cleanup_loader(old=old):
                try:
                    self._pending_loaders.remove(old)
                except ValueError:
                    pass
                old.deleteLater()
            old.finished.connect(lambda *_: _cleanup_loader())
            self._full_loader = None

        # ---------------------------------------------------------
        # 3단계: 비동기 고해상도 로딩 시작
        # ---------------------------------------------------------
        self._full_loader = FullImageLoader(photo_path)
        self._full_loader.finished.connect(self._on_full_image_loaded)
        self._full_loader.start()

        # ---------------------------------------------------------
        # 4단계: 기타 UI 정보 업데이트 (기존 로직 유지)
        # ---------------------------------------------------------
        fname = os.path.basename(photo_path)
        sel = photo_path in self.selected_photos
        self.filename_label.setText(f"{fname}{'  ♥ 선택됨' if sel else ''}")
        self.filename_label.setStyleSheet("color:#4ecca3" if sel else "color:#888")
        self.source_path_label.setText(f"📂 {os.path.dirname(photo_path)}")

        # 버튼 상태 및 큐 스크롤
        if sel:
            self.btn_toggle.setText("♥ 선택 해제 (Space)")
            self.btn_toggle.setStyleSheet(
                "QPushButton{background:#c0392b;color:white;"
                "border:1px solid #e74c3c;border-radius:6px;font-size:12px;font-weight:bold;padding:2px 14px}"
                "QPushButton:hover{background:#e74c3c}")
        else:
            self.btn_toggle.setText("♡ 선택 (Space)")
            self.btn_toggle.setStyleSheet(
                "QPushButton{background:#27ae60;color:white;"
                "border:1px solid #2ecc71;border-radius:6px;font-size:12px;font-weight:bold;padding:2px 14px}"
                "QPushButton:hover{background:#2ecc71}")

        self._update_queue_states()
        self._scroll_queue_to_current()
        self._update_curator_ui()
        self._nav_debounce_timer.start(300)

    def _on_nav_debounced(self):
        """키 연속 입력이 멈춘 뒤 300ms 후에 한 번만 실행 (썸네일 프리로드 + 세션 저장)"""
        self._preload_all_thumbs(self.current_index)
        self._save_session()

    # [추가된 콜백 함수]
    def _on_full_image_loaded(self, path, pixmap):
        """백그라운드에서 고해상도 로드가 완료되었을 때 호출"""
        # 현재 사용자가 보고 있는 사진 경로와 로드된 사진 경로가 일치하는지 최종 확인
        if not self.current_round_photos or self.current_index >= len(self.current_round_photos):
            return

        current_path = self.current_round_photos[self.current_index]
        if path == current_path:
            self.image_viewer.set_image(pixmap)
            self._update_zoom_label()

    # ── 1장 뷰 액션 ──
    def _toggle_select(self):
        if self.stack.currentIndex() != 1 or self.current_view_mode != self.VIEW_SINGLE:
            return
        if self.current_index >= len(self.current_round_photos):
            return

        photo = self.current_round_photos[self.current_index]

        # 선택 상태 반전
        if photo in self.selected_photos:
            self.selected_photos.remove(photo)
        else:
            self.selected_photos.append(photo)

        # 전체를 새로 그리는 대신 '상태'만 업데이트 (줌 유지 핵심)
        self._update_selection_ui_only(photo)
        self._save_session()

    def _update_selection_ui_only(self, photo_path):
        """이미지는 건드리지 않고 선택 관련 UI만 즉시 갱신"""
        sel = photo_path in self.selected_photos
        fname = os.path.basename(photo_path)

        # 1. 파일명 라벨 업데이트
        self.filename_label.setText(f"{fname}{'  ♥ 선택됨' if sel else ''}")
        self.filename_label.setStyleSheet("color:#4ecca3" if sel else "color:#888")

        # 2. 선택 버튼 스타일 업데이트
        if sel:
            self.btn_toggle.setText("♥ 선택 해제 (Space)")
            self.btn_toggle.setStyleSheet(
                "QPushButton{background:#c0392b;color:white;"
                "border:1px solid #e74c3c;border-radius:6px;font-size:12px;font-weight:bold;padding:2px 14px}"
                "QPushButton:hover{background:#e74c3c}")
        else:
            self.btn_toggle.setText("♡ 선택 (Space)")
            self.btn_toggle.setStyleSheet(
                "QPushButton{background:#27ae60;color:white;"
                "border:1px solid #2ecc71;border-radius:6px;font-size:12px;font-weight:bold;padding:2px 14px}"
                "QPushButton:hover{background:#2ecc71}")

        # 3. 하단 큐와 상단 정보 바 갱신
        self._update_queue_states()
        self.selected_label.setText(f"선택: {len(self.selected_photos)}장")

    def _go_next(self):
        if self.stack.currentIndex() != 1 or self.current_view_mode != self.VIEW_SINGLE:
            return
        if self.current_index < len(self.current_round_photos) - 1:
            self.current_index += 1
            self._show_current_photo()

    def _go_previous(self):
        if self.stack.currentIndex() != 1 or self.current_view_mode != self.VIEW_SINGLE:
            return
        if self.current_index > 0:
            self.current_index -= 1
            self._show_current_photo()

    # ── 라운드 ──
    def _finish_round(self):
        sc = len(self.selected_photos)
        if sc == 0:
            QMessageBox.warning(self, "선택 없음", "최소 1장 이상 선택해야 합니다.")
            return
        self.round_history.append({
            "round": self.round_number,
            "input_count": len(self.current_round_photos),
            "selected_count": sc
        })
        if sc <= self.target_count:
            self._show_results()
        else:
            reply = QMessageBox.question(
                self, f"라운드 {self.round_number} 완료",
                f"이번 라운드: {sc}장 선택 (목표: {self.target_count}장)\n\n"
                f"다음 라운드에서 더 줄이시겠습니까?\n"
                f"(아니오 = 현재 {sc}장으로 최종 확정)",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
            if reply == QMessageBox.Yes:
                self.round_number += 1
                self.current_round_photos = list(self.selected_photos)
                self.selected_photos = []
                self.current_index = 0
                self.thumb_cache.clear()
                self.grid_thumb_cache.clear()
                self._build_queue()
                self._preload_all_thumbs(0)
                self._switch_to_curator()
            else:
                self._show_results()

    def _show_results(self):
        count = len(self.selected_photos)
        folder_counts = {}
        for p in self.selected_photos:
            parent = os.path.dirname(p)
            folder_counts[parent] = folder_counts.get(parent, 0) + 1
        source_summary = "\n".join(
            f"  📂 {f}: {c}장" for f, c in sorted(folder_counts.items()))
        history = "\n".join(
            f"  라운드 {r['round']}: {r['input_count']}장 → {r['selected_count']}장"
            for r in self.round_history)
        self.result_info.setText(
            f"총 {len(self.all_photos)}장에서 {count}장을 선별했습니다!\n\n"
            f"선별 과정:\n{history}\n\n"
            f"소스별 선택:\n{source_summary}\n\n"
            f"선택한 사진을 원하는 폴더로 내보내세요.")
        self.stack.setCurrentIndex(2)
        self._save_session()

    def _export_photos(self):
        export_dir = QFileDialog.getExistingDirectory(self, "내보낼 폴더 선택")
        if not export_dir:
            return
        errors, used = [], set()
        for photo in self.selected_photos:
            try:
                base = os.path.basename(photo)
                name = base
                if name in used:
                    stem, ext = os.path.splitext(base)
                    c = 1
                    while name in used:
                        name = f"{stem}_{c}{ext}"
                        c += 1
                used.add(name)
                shutil.copy2(photo, os.path.join(export_dir, name))
            except Exception as e:
                errors.append(str(e))
        if errors:
            QMessageBox.warning(self, "오류",
                f"{len(self.selected_photos) - len(errors)}장 복사, 오류 {len(errors)}건")
        else:
            QMessageBox.information(self, "완료",
                f"{len(self.selected_photos)}장 복사!\n위치: {export_dir}")

    def _restart(self):
        self.round_number = 1
        self.current_index = 0
        self.current_round_photos = []
        self.selected_photos = []
        self.round_history = []
        self.thumb_cache.clear()
        self.grid_thumb_cache.clear()
        self._clear_grid()
        self._clear_queue()
        self.stack.setCurrentIndex(0)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.stack.currentIndex() == 1:
            self._resize_timer.start(150)

    def _on_resize_done(self):
        if self.stack.currentIndex() != 1:
            return
        if self.current_view_mode == self.VIEW_SINGLE:
            pass  # ZoomableImageView handles its own resize
        else:
            self._populate_grid()


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = PhotoCurator()
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()