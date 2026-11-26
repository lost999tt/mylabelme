from __future__ import annotations

import enum
import functools
import json
import math
import os
import platform
import random
import re
import subprocess
import time
import webbrowser
from collections import Counter
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any
from typing import Final
from typing import Literal
from typing import NamedTuple
from typing import TypeAlias
from typing import cast
from typing import get_args

import imgviz
import natsort
import numpy as np
import osam
from loguru import logger
from numpy.typing import NDArray
from PyQt5 import QtCore
from PyQt5 import QtGui
from PyQt5 import QtWidgets
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QMessageBox

from labelme import __appname__
from labelme import __version__
from labelme import _automation
from labelme._folder_annotations import FolderLabelIndex
from labelme._folder_annotations import annotation_image_reference
from labelme._folder_annotations import frame_rectangle_anchors
from labelme._folder_annotations import interpolate_rectangle
from labelme._folder_annotations import plan_frame_copy
from labelme._folder_annotations import plan_frame_sync
from labelme._folder_annotations import plan_folder_label_edit
from labelme._folder_annotations import write_folder_label_changes
from labelme._label_file import LABEL_FILE_SUFFIX
from labelme._label_file import LabelData
from labelme._label_file import LabelFileError
from labelme._label_file import ShapeDict
from labelme._label_file import is_label_file_path
from labelme._label_file import read_image_file
from labelme._label_file import read_label_file
from labelme._label_file import write_label_file
from labelme._shape import Shape
from labelme._shape_clipboard import ShapeClipboard
from labelme.config import load_config
from labelme.widgets import AiAssistedAnnotationWidget
from labelme.widgets import AiTextToAnnotationWidget
from labelme.widgets import BrightnessContrastDialog
from labelme.widgets import Canvas
from labelme.widgets import LabelDialog
from labelme.widgets import LabelListWidget
from labelme.widgets import LabelListWidgetItem
from labelme.widgets import StatusStats
from labelme.widgets import ToolBar
from labelme.widgets import UniqueLabelQListWidget
from labelme.widgets import ZoomWidget
from labelme.widgets import download_ai_model
from labelme.widgets import format_shape_label
from labelme.widgets.label_dialog import position_left_of
from labelme.widgets.brightness_contrast_dialog import NEUTRAL_TONE, adjusted_image

from . import utils

# handle high-dpi scaling issue
# https://leomoon.com/journal/python/high-dpi-scaling-in-pyqt5
if hasattr(QtCore.Qt, "AA_EnableHighDpiScaling"):
    QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_EnableHighDpiScaling, True)
if hasattr(QtCore.Qt, "AA_UseHighDpiPixmaps"):
    QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_UseHighDpiPixmaps, True)


LABEL_COLORMAP: NDArray[np.uint8] = imgviz.label_colormap()


class _ZoomMode(enum.Enum):
    FIT_WINDOW = enum.auto()
    FIT_WIDTH = enum.auto()
    MANUAL_ZOOM = enum.auto()


_TextToAnnotationCreateMode: TypeAlias = Literal["polygon", "rectangle"]
_AI_CREATE_MODES: tuple[str, ...] = (
    "ai_points_to_shape",
    "ai_box_to_shape",
)
_AI_MODELS_WITHOUT_POINT_SUPPORT: tuple[str, ...] = ("sam3:latest",)


class _StatusBarWidgets(NamedTuple):
    message: QtWidgets.QLabel
    stats: StatusStats


class _CanvasWidgets(NamedTuple):
    canvas: Canvas
    zoom_widget: ZoomWidget
    scroll_bars: dict[Qt.Orientation, QtWidgets.QScrollBar]


class _DockWidgets(NamedTuple):
    flag_dock: QtWidgets.QDockWidget
    flag_list: QtWidgets.QListWidget
    shape_dock: QtWidgets.QDockWidget
    label_list: LabelListWidget
    label_dock: QtWidgets.QDockWidget
    unique_label_list: UniqueLabelQListWidget
    file_dock: QtWidgets.QDockWidget
    file_search: QtWidgets.QLineEdit
    file_list: QtWidgets.QListWidget


class _Actions(NamedTuple):
    about: QtWidgets.QAction
    save: QtWidgets.QAction
    save_as: QtWidgets.QAction
    save_auto: QtWidgets.QAction
    save_with_image_data: QtWidgets.QAction
    change_output_dir: QtWidgets.QAction
    open: QtWidgets.QAction
    close: QtWidgets.QAction
    delete_file: QtWidgets.QAction
    toggle_keep_prev_mode: QtWidgets.QAction
    toggle_keep_prev_brightness_contrast: QtWidgets.QAction
    delete: QtWidgets.QAction
    edit: QtWidgets.QAction
    duplicate: QtWidgets.QAction
    copy: QtWidgets.QAction
    paste: QtWidgets.QAction
    undo_last_point: QtWidgets.QAction
    undo: QtWidgets.QAction
    add_point_to_edge: QtWidgets.QAction
    remove_point: QtWidgets.QAction
    create_mode: QtWidgets.QAction
    edit_mode: QtWidgets.QAction
    create_rectangle_mode: QtWidgets.QAction
    create_oriented_rectangle_mode: QtWidgets.QAction
    create_circle_mode: QtWidgets.QAction
    create_line_mode: QtWidgets.QAction
    create_point_mode: QtWidgets.QAction
    create_line_strip_mode: QtWidgets.QAction
    create_ai_points_to_shape_mode: QtWidgets.QAction
    create_ai_box_to_shape_mode: QtWidgets.QAction
    open_next_img: QtWidgets.QAction
    open_prev_img: QtWidgets.QAction
    keep_prev_zoom: QtWidgets.QAction
    fit_window: QtWidgets.QAction
    fit_width: QtWidgets.QAction
    brightness_contrast: QtWidgets.QAction
    zoom_in: QtWidgets.QAction
    zoom_out: QtWidgets.QAction
    zoom_org: QtWidgets.QAction
    reset_layout: QtWidgets.QAction
    fill_drawing: QtWidgets.QAction
    hide_all: QtWidgets.QAction
    show_all: QtWidgets.QAction
    toggle_all: QtWidgets.QAction
    open_dir: QtWidgets.QAction
    zoom_widget_action: QtWidgets.QWidgetAction
    draw: list[tuple[str, QtWidgets.QAction]]
    zoom: tuple[ZoomWidget | QtWidgets.QAction, ...]
    on_load_active: tuple[QtWidgets.QAction, ...]
    on_shapes_present: tuple[QtWidgets.QAction, ...]
    context_menu: tuple[QtWidgets.QAction, ...]
    edit_menu: tuple[QtWidgets.QAction | None, ...]


class _Menus(NamedTuple):
    file: QtWidgets.QMenu
    edit: QtWidgets.QMenu
    view: QtWidgets.QMenu
    help: QtWidgets.QMenu
    label_list: QtWidgets.QMenu


class _FilePathDelegate(QtWidgets.QStyledItemDelegate):
    show_full_path = False

    def initStyleOption(self, option: QtWidgets.QStyleOptionViewItem, index: QtCore.QModelIndex) -> None:
        super().initStyleOption(option, index)
        if self.show_full_path:
            return
        option.text = Path(option.text).name
        if option.rect.width() > 0:
            style = option.widget.style() if option.widget else QtWidgets.QApplication.style()
            text_rect = style.subElementRect(QtWidgets.QStyle.SE_ItemViewItemText, option, option.widget)
            reserve = option.fontMetrics.horizontalAdvance(str(index.model().rowCount())) + 16
            option.text = option.fontMetrics.elidedText(option.text, Qt.ElideRight, max(0, text_rect.width() - reserve))

    def paint(self, painter: QtGui.QPainter, option: QtWidgets.QStyleOptionViewItem,
              index: QtCore.QModelIndex) -> None:
        super().paint(painter, option, index)
        if self.show_full_path:
            return
        painter.save()
        painter.setFont(option.font)
        selected = bool(option.state & QtWidgets.QStyle.State_Selected)
        color = option.palette.highlightedText() if selected else option.palette.text()
        painter.setPen(color.color())
        painter.drawText(option.rect.adjusted(0, 0, -9, 0),
                         Qt.AlignRight | Qt.AlignVCenter, str(index.row() + 1))
        painter.restore()


class _TrajectoryComboBox(QtWidgets.QComboBox):
    def showPopup(self) -> None:
        super().showPopup()
        popup = self.view().window()
        # Use the button edge as the anchor instead of the selected option.
        popup.move(self.mapToGlobal(QtCore.QPoint(0, self.height())))


class _FrameCopyMenuRow(QtWidgets.QWidget):
    def __init__(self, menu: QtWidgets.QMenu) -> None:
        super().__init__(menu)
        self._menu = menu

    def enterEvent(self, event: QtCore.QEvent) -> None:
        # Embedded controls do not update QMenu's active action on hover.
        self._menu.setActiveAction(None)
        super().enterEvent(event)


class MainWindow(QtWidgets.QMainWindow):
    _config_file: Path | None
    _config: dict

    _text_osam_session: _automation.OsamSession | None = None
    _is_changed: bool = False
    _shape_clipboard: ShapeClipboard
    _zoom_mode: _ZoomMode
    _prev_opened_dir: str | None
    _canvas_widgets: _CanvasWidgets
    _status_bar: _StatusBarWidgets
    _docks: _DockWidgets
    _actions: _Actions
    _menus: _Menus
    _label_dialog: LabelDialog
    _ai_annotation: AiAssistedAnnotationWidget
    _ai_text: AiTextToAnnotationWidget

    _output_dir: Path | None
    _image: QtGui.QImage
    _image_data: bytes | None
    _label_file_path: str | None
    _image_path: str | None
    _prev_image_path: str | None
    _other_data: dict | None
    _has_image_zoom: bool
    _brightness_contrast_values: dict[str, tuple[int | None, int | None]]
    _scroll_values: dict[Qt.Orientation, dict[str, float]]
    _default_state: QtCore.QByteArray

    def __init__(
        self,
        config_file: Path | None = None,
        config_overrides: dict | None = None,
        file_or_dir: str | None = None,
        output_dir: str | None = None,
    ) -> None:
        super().__init__()
        self.setWindowTitle(__appname__)

        self._config_file, self._config = self._load_config(
            config_file=config_file, config_overrides=config_overrides
        )

        # set default shape colors
        Shape.line_color = QtGui.QColor(*self._config["shape"]["line_color"])
        Shape.fill_color = QtGui.QColor(*self._config["shape"]["fill_color"])
        Shape.select_line_color = QtGui.QColor(
            *self._config["shape"]["select_line_color"]
        )
        Shape.select_fill_color = QtGui.QColor(
            *self._config["shape"]["select_fill_color"]
        )
        Shape.vertex_fill_color = QtGui.QColor(
            *self._config["shape"]["vertex_fill_color"]
        )
        Shape.hvertex_fill_color = QtGui.QColor(
            *self._config["shape"]["hvertex_fill_color"]
        )

        # Set point size from config file
        Shape.point_size = self._config["shape"]["point_size"]

        self._shape_clipboard = ShapeClipboard(self)

        self._label_dialog = LabelDialog(
            parent=self,
            labels=self._config["labels"],
            sort_labels=self._config["sort_labels"],
            show_text_field=self._config["show_label_text_field"],
            completion=self._config["label_completion"],
            fit_to_content=self._config["fit_to_content"],
            flags=self._config["label_flags"],
            group_counts=self._dialog_group_counts,
        )

        self._prev_opened_dir = None
        self._color_by_group_id = True
        self._target_colors: dict[tuple[str, int | None], tuple[int, int, int]] = {}
        self._next_group_color = 1
        self._random_label_colors: dict[str, tuple[int, int, int]] = {}
        self._bright_targets: set[tuple[str, int | None]] = set()
        self._bright_color_index = 0
        self._docks = self._setup_dock_widgets()

        self.setAcceptDrops(True)
        self._canvas_widgets = self._setup_canvas()

        self._actions = self._setup_actions()
        self._setup_file_dock_title(self._docks.file_dock)
        self._shape_clipboard.availability_changed.connect(
            self._actions.paste.setEnabled
        )
        self._menus = self._setup_menus()

        self._ai_annotation = AiAssistedAnnotationWidget(
            default_model=self._config["ai"]["default"],
            on_model_changed=self._canvas_widgets.canvas.set_ai_model_name,
            on_output_format_changed=self._canvas_widgets.canvas.set_ai_output_format,
            parent=self,
        )
        self._ai_annotation.setEnabled(False)

        self._ai_text = AiTextToAnnotationWidget(
            on_submit=self._submit_ai_prompt, parent=self
        )
        self._ai_text.setEnabled(False)

        self._setup_toolbars()

        self._status_bar = self._setup_status_bar()

        self._setup_app_state(file_or_dir=file_or_dir, output_dir=output_dir)

        self._canvas_widgets.zoom_widget.valueChanged.connect(self._paint_canvas)

        self.populate_mode_actions()

    def _setup_actions(self) -> _Actions:
        action = functools.partial(utils.new_action, self)
        shortcuts = self._config["shortcuts"]

        about = action(
            text=f"&About {__appname__}",
            slot=functools.partial(
                QMessageBox.about,
                self,
                f"About {__appname__}",
                f"""
<h3>{__appname__}</h3>
<p>Image Polygonal Annotation with Python</p>
<p>Version: {__version__}</p>
<p>Author: Kentaro Wada</p>
<p>
    <a href="https://labelme.io">Homepage</a> |
    <a href="https://labelme.io/docs">Documentation</a> |
    <a href="https://labelme.io/docs/troubleshoot">Troubleshooting</a>
</p>
<p>
    <a href="https://github.com/wkentaro/labelme">GitHub</a> |
    <a href="https://x.com/labelmeai">Twitter/X</a>
</p>
""",
            ),
        )
        save = action(
            text=self.tr("&Save\n"),
            slot=self._save_label_file,
            shortcut=shortcuts["save"],
            icon="phosphor/floppy-disk.svg",
            tip=self.tr("Save labels to file"),
            enabled=False,
        )
        save_as = action(
            text=self.tr("&Save As"),
            slot=lambda: self._save_label_file(save_as=True),
            shortcut=shortcuts["save_as"],
            icon="phosphor/floppy-disk.svg",
            tip=self.tr("Save labels to a different file"),
            enabled=False,
        )
        save_auto = action(
            text=self.tr("Save &Automatically"),
            tip=self.tr("Save automatically"),
            checkable=True,
            enabled=True,
        )
        save_auto.setChecked(self._config["auto_save"])
        save_with_image_data = action(
            text=self.tr("Save With Image Data"),
            slot=self.set_save_image_with_data,
            tip=self.tr("Save image data in label file"),
            checkable=True,
            checked=self._config["with_image_data"],
        )
        change_output_dir = action(
            text=self.tr("&Change Output Dir"),
            slot=self.prompt_output_dir,
            shortcut=shortcuts["save_to"],
            icon="phosphor/folders.svg",
            tip=self.tr("Change where annotations are loaded/saved"),
        )
        open_ = action(
            text=self.tr("&Open\n"),
            slot=self._open_file_with_dialog,
            shortcut=shortcuts["open"],
            icon="phosphor/folder-open.svg",
            tip=self.tr("Open image or label file"),
        )
        open_dir = action(
            text=self.tr("Open Dir"),
            slot=self._open_dir_with_dialog,
            shortcut=shortcuts["open_dir"],
            icon="phosphor/folder-open.svg",
            tip=self.tr("Open Dir"),
        )
        close = action(
            text=self.tr("&Close"),
            slot=self.close_file,
            shortcut=shortcuts["close"],
            icon="phosphor/x-circle.svg",
            tip=self.tr("Close current file"),
        )
        delete_file = action(
            text=self.tr("&Delete File"),
            slot=self.delete_file,
            shortcut=shortcuts["delete_file"],
            icon="phosphor/file-x.svg",
            tip=self.tr("Delete current label file"),
            enabled=False,
        )
        keep_prev_action = action(
            text=self.tr("Keep Previous Annotation"),
            slot=lambda: self._config.__setitem__(
                "keep_prev", not self._config["keep_prev"]
            ),
            shortcut=shortcuts["toggle_keep_prev_mode"],
            tip=self.tr('Toggle "keep previous annotation" mode'),
            checkable=True,
            checked=self._config["keep_prev"],
        )
        toggle_keep_prev_brightness_contrast = action(
            text=self.tr("Keep Previous Brightness/Contrast"),
            slot=lambda: self._config.__setitem__(
                "keep_prev_brightness_contrast",
                not self._config["keep_prev_brightness_contrast"],
            ),
            checkable=True,
            checked=self._config["keep_prev_brightness_contrast"],
        )
        delete = action(
            self.tr("Delete Shapes"),
            self.delete_selected_shapes,
            shortcuts["delete_shape"],
            icon="phosphor/trash.svg",
            tip=self.tr("Delete the selected shapes"),
            enabled=False,
        )
        edit = action(
            self.tr("&Edit Label"),
            self._edit_label,
            shortcuts["edit_label"],
            icon="phosphor/note-pencil.svg",
            tip=self.tr("Modify the label of the selected shape"),
            enabled=False,
        )
        duplicate = action(
            self.tr("Duplicate Shapes"),
            lambda: self._insert_shapes(
                [s.copy() for s in self._canvas_widgets.canvas.selected_shapes]
            ),
            shortcuts["duplicate_shape"],
            icon="phosphor/copy.svg",
            tip=self.tr("Create a duplicate of the selected shapes"),
            enabled=False,
        )
        copy = action(
            self.tr("Copy Shapes"),
            lambda: self._shape_clipboard.store(
                self._canvas_widgets.canvas.selected_shapes
            ),
            shortcuts["copy_shape"],
            "copy_clipboard",
            self.tr("Copy selected shapes to clipboard"),
            enabled=False,
        )
        paste = action(
            self.tr("Paste Shapes"),
            lambda: self._insert_shapes(self._shape_clipboard.paste()),
            shortcuts["paste_shape"],
            "paste",
            self.tr("Paste copied shapes"),
            enabled=False,
        )
        undo_last_point = action(
            self.tr("Undo last point"),
            self._canvas_widgets.canvas.undo_last_point,
            shortcuts["undo_last_point"],
            icon="phosphor/arrow-u-up-left.svg",
            tip=self.tr("Undo last drawn point"),
            enabled=False,
        )
        undo = action(
            self.tr("Undo\n"),
            self.undo_shape_edit,
            shortcuts["undo"],
            icon="phosphor/arrow-u-up-left.svg",
            tip=self.tr("Undo last add and edit of shape"),
            enabled=False,
        )
        remove_point = action(
            text=self.tr("Remove Selected Point"),
            slot=self.remove_selected_point,
            shortcut=shortcuts["remove_selected_point"],
            icon="phosphor/trash.svg",
            tip=self.tr("Remove selected point from polygon"),
            enabled=False,
        )
        add_point_to_edge = action(
            text=self.tr("Add Point to Edge"),
            slot=self._canvas_widgets.canvas.add_point_to_edge,
            tip=self.tr("Insert a new point at the hovered polygon edge"),
            enabled=False,
        )
        create_mode = action(
            text=self.tr("Polygon"),
            slot=lambda: self._switch_canvas_mode(edit=False, create_mode="polygon"),
            shortcut=shortcuts["create_polygon"],
            icon="phosphor/polygon.svg",
            tip=self.tr("Start drawing polygons"),
            enabled=False,
        )
        edit_mode = action(
            self.tr("Edit Shapes"),
            lambda: self._switch_canvas_mode(edit=True),
            shortcuts["edit_shape"],
            icon="phosphor/note-pencil.svg",
            tip=self.tr("Move and edit the selected shapes"),
            enabled=False,
        )
        create_rectangle_mode = action(
            text=self.tr("Rectangle"),
            slot=lambda: self._switch_canvas_mode(edit=False, create_mode="rectangle"),
            shortcut=shortcuts["create_rectangle"],
            icon="phosphor/rectangle.svg",
            tip=self.tr("Start drawing rectangles"),
            enabled=False,
        )
        create_oriented_rectangle_mode = action(
            text=self.tr("Oriented Rectangle"),
            slot=lambda: self._switch_canvas_mode(
                edit=False, create_mode="oriented_rectangle"
            ),
            shortcut=shortcuts["create_oriented_rectangle"],
            icon="oriented_rectangle.svg",
            tip=self.tr("Start drawing oriented rectangles"),
            enabled=False,
        )
        create_circle_mode = action(
            text=self.tr("Circle"),
            slot=lambda: self._switch_canvas_mode(edit=False, create_mode="circle"),
            shortcut=shortcuts["create_circle"],
            icon="phosphor/circle.svg",
            tip=self.tr("Start drawing circles"),
            enabled=False,
        )
        create_line_mode = action(
            text=self.tr("Line"),
            slot=lambda: self._switch_canvas_mode(edit=False, create_mode="line"),
            shortcut=shortcuts["create_line"],
            icon="phosphor/line-segment.svg",
            tip=self.tr("Start drawing lines"),
            enabled=False,
        )
        create_point_mode = action(
            text=self.tr("Point"),
            slot=lambda: self._switch_canvas_mode(edit=False, create_mode="point"),
            shortcut=shortcuts["create_point"],
            icon="phosphor/circles-four.svg",
            tip=self.tr("Start drawing points"),
            enabled=False,
        )
        create_line_strip_mode = action(
            text=self.tr("LineStrip"),
            slot=lambda: self._switch_canvas_mode(edit=False, create_mode="linestrip"),
            shortcut=shortcuts["create_linestrip"],
            icon="phosphor/line-segments.svg",
            tip=self.tr("Start drawing linestrip. Ctrl+LeftClick ends creation."),
            enabled=False,
        )
        create_ai_points_to_shape_mode = action(
            self.tr("AI-Points"),
            lambda: self._switch_canvas_mode(
                edit=False, create_mode="ai_points_to_shape"
            ),
            None,
            "ai-points.svg",
            self.tr("Click points to segment object. Ctrl+LeftClick ends creation."),
            enabled=False,
        )
        create_ai_box_to_shape_mode = action(
            self.tr("AI-Box"),
            lambda: self._switch_canvas_mode(edit=False, create_mode="ai_box_to_shape"),
            None,
            "ai-box.svg",
            self.tr("Draw a bounding box to segment object."),
            enabled=False,
        )
        open_next_img = action(
            text=self.tr("&Next Image"),
            slot=self._open_next_image,
            shortcut=shortcuts["open_next"],
            icon="phosphor/arrow-fat-right.svg",
            tip=self.tr("Open next (hold Ctl+Shift to copy labels)"),
            enabled=False,
        )
        open_prev_img = action(
            text=self.tr("&Prev Image"),
            slot=self._open_prev_image,
            shortcut=shortcuts["open_prev"],
            icon="phosphor/arrow-fat-left.svg",
            tip=self.tr("Open prev (hold Ctl+Shift to copy labels)"),
            enabled=False,
        )
        keep_prev_zoom = action(
            text=self.tr("&Keep Previous Zoom"),
            slot=lambda: self._config.__setitem__(
                "keep_prev_scale",
                not self._config["keep_prev_scale"],
            ),
            checkable=True,
            checked=self._config["keep_prev_scale"],
        )
        fit_window = action(
            self.tr("&Fit Window"),
            self.set_fit_window_mode,
            shortcuts["fit_window"],
            icon="phosphor/frame-corners.svg",
            tip=self.tr("Zoom follows window size"),
            checkable=True,
            enabled=False,
        )
        fit_width = action(
            self.tr("Fit &Width"),
            self.set_fit_width_mode,
            shortcuts["fit_width"],
            icon="frame-arrows-horizontal.svg",
            tip=self.tr("Zoom follows window width"),
            checkable=True,
            enabled=False,
        )
        brightness_contrast = action(
            self.tr("&Brightness Contrast"),
            self.open_brightness_contrast_dialog,
            None,
            "brightness-contrast.svg",
            self.tr("Adjust brightness and contrast"),
            enabled=False,
        )
        zoom_in = action(
            self.tr("Zoom &In"),
            lambda _: self._add_zoom(increment=1.1),
            shortcuts["zoom_in"],
            icon="phosphor/magnifying-glass-minus.svg",
            tip=self.tr("Increase zoom level"),
            enabled=False,
        )
        zoom_out = action(
            self.tr("&Zoom Out"),
            lambda _: self._add_zoom(increment=0.9),
            shortcuts["zoom_out"],
            icon="phosphor/magnifying-glass-plus.svg",
            tip=self.tr("Decrease zoom level"),
            enabled=False,
        )
        zoom_org = action(
            self.tr("&Original size"),
            self._set_zoom_to_original,
            shortcuts["zoom_to_original"],
            icon="phosphor/image-square.svg",
            tip=self.tr("Zoom to original size"),
            enabled=False,
        )
        reset_layout = action(
            text=self.tr("Reset Layout"),
            slot=self._reset_layout,
            icon="phosphor/layout-duotone.svg",
        )
        fill_drawing = action(
            self.tr("Fill Drawing Polygon"),
            self._canvas_widgets.canvas.set_fill_drawing,
            None,
            icon="phosphor/paint-bucket.svg",
            tip=self.tr("Fill polygon while drawing"),
            checkable=True,
            enabled=True,
        )
        if self._config["canvas"]["fill_drawing"]:
            fill_drawing.trigger()
        hide_all = action(
            self.tr("&Hide\nShapes"),
            functools.partial(self.toggle_shape_visibility, False),
            shortcuts["hide_all_shapes"],
            icon="phosphor/eye.svg",
            tip=self.tr("Hide all shapes"),
            enabled=False,
        )
        show_all = action(
            self.tr("&Show\nShapes"),
            functools.partial(self.toggle_shape_visibility, True),
            shortcuts["show_all_shapes"],
            icon="phosphor/eye.svg",
            tip=self.tr("Show all shapes"),
            enabled=False,
        )
        toggle_all = action(
            self.tr("&Toggle\nShapes"),
            functools.partial(self.toggle_shape_visibility, None),
            shortcuts["toggle_all_shapes"],
            icon="phosphor/eye.svg",
            tip=self.tr("Toggle all shapes"),
            enabled=False,
        )

        zoom_widget_action = QtWidgets.QWidgetAction(self)
        zoom_box_layout = QtWidgets.QVBoxLayout()
        zoom_label = QtWidgets.QLabel(self.tr("Zoom"))
        zoom_label.setAlignment(Qt.AlignCenter)
        zoom_box_layout.addWidget(zoom_label)
        zoom_box_layout.addWidget(self._canvas_widgets.zoom_widget)
        zoom_widget_action.setDefaultWidget(QtWidgets.QWidget())
        zoom_widget_action.defaultWidget().setLayout(zoom_box_layout)
        self._canvas_widgets.zoom_widget.setWhatsThis(
            str(
                self.tr(
                    "Zoom the image in or out. The shortcuts "
                    "{} and {} also work on the canvas."
                )
            ).format(
                utils.format_shortcut(
                    f"{shortcuts['zoom_in']},{shortcuts['zoom_out']}"
                ),
                utils.format_shortcut(self.tr("Ctrl+Wheel")),
            )
        )
        self._canvas_widgets.zoom_widget.setEnabled(False)

        self._zoom_mode = _ZoomMode.FIT_WINDOW
        fit_window.setChecked(Qt.Checked)

        self._canvas_widgets.canvas.vertex_selected.connect(remove_point.setEnabled)
        self._canvas_widgets.canvas.edge_selected.connect(add_point_to_edge.setEnabled)

        draw = [
            ("polygon", create_mode),
            ("rectangle", create_rectangle_mode),
            ("oriented_rectangle", create_oriented_rectangle_mode),
            ("circle", create_circle_mode),
            ("point", create_point_mode),
            ("line", create_line_mode),
            ("linestrip", create_line_strip_mode),
            ("ai_points_to_shape", create_ai_points_to_shape_mode),
            ("ai_box_to_shape", create_ai_box_to_shape_mode),
        ]
        zoom = (
            self._canvas_widgets.zoom_widget,
            zoom_in,
            zoom_out,
            zoom_org,
            fit_window,
            fit_width,
        )
        on_load_active = (
            close,
            create_mode,
            create_rectangle_mode,
            create_oriented_rectangle_mode,
            create_circle_mode,
            create_line_mode,
            create_point_mode,
            create_line_strip_mode,
            create_ai_points_to_shape_mode,
            create_ai_box_to_shape_mode,
            brightness_contrast,
        )
        on_shapes_present = (save_as, hide_all, show_all, toggle_all)
        context_menu = (
            delete,
            *[draw_action for _, draw_action in draw],
            edit_mode,
            edit,
            duplicate,
            copy,
            paste,
            undo,
            undo_last_point,
            add_point_to_edge,
            remove_point,
        )
        edit_menu = (
            edit,
            duplicate,
            copy,
            paste,
            delete,
            None,
            undo,
            undo_last_point,
            None,
            remove_point,
            None,
            keep_prev_action,
        )
        return _Actions(
            about=about,
            save=save,
            save_as=save_as,
            save_auto=save_auto,
            save_with_image_data=save_with_image_data,
            change_output_dir=change_output_dir,
            open=open_,
            close=close,
            delete_file=delete_file,
            toggle_keep_prev_mode=keep_prev_action,
            toggle_keep_prev_brightness_contrast=toggle_keep_prev_brightness_contrast,
            delete=delete,
            edit=edit,
            duplicate=duplicate,
            copy=copy,
            paste=paste,
            undo_last_point=undo_last_point,
            undo=undo,
            remove_point=remove_point,
            add_point_to_edge=add_point_to_edge,
            create_mode=create_mode,
            edit_mode=edit_mode,
            create_rectangle_mode=create_rectangle_mode,
            create_oriented_rectangle_mode=create_oriented_rectangle_mode,
            create_circle_mode=create_circle_mode,
            create_line_mode=create_line_mode,
            create_point_mode=create_point_mode,
            create_line_strip_mode=create_line_strip_mode,
            create_ai_points_to_shape_mode=create_ai_points_to_shape_mode,
            create_ai_box_to_shape_mode=create_ai_box_to_shape_mode,
            open_next_img=open_next_img,
            open_prev_img=open_prev_img,
            keep_prev_zoom=keep_prev_zoom,
            fit_window=fit_window,
            fit_width=fit_width,
            brightness_contrast=brightness_contrast,
            zoom_in=zoom_in,
            zoom_out=zoom_out,
            zoom_org=zoom_org,
            reset_layout=reset_layout,
            fill_drawing=fill_drawing,
            hide_all=hide_all,
            show_all=show_all,
            toggle_all=toggle_all,
            open_dir=open_dir,
            zoom_widget_action=zoom_widget_action,
            draw=draw,
            zoom=zoom,
            on_load_active=on_load_active,
            on_shapes_present=on_shapes_present,
            context_menu=context_menu,
            edit_menu=edit_menu,
        )

    def _setup_menus(self) -> _Menus:
        action = functools.partial(utils.new_action, self)
        shortcuts = self._config["shortcuts"]

        quit_ = action(
            text=self.tr("&Quit"),
            slot=self.close,
            shortcut=shortcuts["quit"],
            icon=None,
            tip=self.tr("Quit application"),
        )
        open_config = action(
            text=self.tr("Preferences…"),
            slot=self._open_config_file,
            shortcut="Ctrl+," if platform.system() == "Darwin" else "Ctrl+Shift+,",
            icon=None,
            tip=self.tr("Open config file in text editor"),
        )
        open_config.setMenuRole(QtWidgets.QAction.PreferencesRole)
        help_ = action(
            self.tr("&Tutorial"),
            self.tutorial,
            icon="phosphor/question.svg",
            tip=self.tr("Show tutorial page"),
        )

        file_menu = self.menu(self.tr("&File"))
        edit_menu = self.menu(self.tr("&Edit"))
        view_menu = self.menu(self.tr("&View"))
        help_menu = self.menu(self.tr("&Help"))
        label_menu = QtWidgets.QMenu()
        utils.add_actions(label_menu, (self._actions.edit, self._actions.delete))
        self._docks.label_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self._docks.label_list.customContextMenuRequested.connect(
            self.show_label_list_menu
        )

        utils.add_actions(
            file_menu,
            (
                self._actions.open,
                self._actions.open_next_img,
                self._actions.open_prev_img,
                self._actions.open_dir,
                self._actions.save,
                self._actions.save_as,
                self._actions.save_auto,
                self._actions.change_output_dir,
                self._actions.save_with_image_data,
                self._actions.close,
                self._actions.delete_file,
                None,
                open_config,
                None,
                quit_,
            ),
        )
        utils.add_actions(help_menu, (help_, self._actions.about))
        utils.add_actions(
            view_menu,
            (
                self._docks.flag_dock.toggleViewAction(),
                self._docks.label_dock.toggleViewAction(),
                self._docks.shape_dock.toggleViewAction(),
                self._docks.file_dock.toggleViewAction(),
                None,
                self._actions.reset_layout,
                None,
                self._actions.fill_drawing,
                None,
                self._actions.hide_all,
                self._actions.show_all,
                self._actions.toggle_all,
                None,
                self._actions.zoom_in,
                self._actions.zoom_out,
                self._actions.zoom_org,
                self._actions.keep_prev_zoom,
                None,
                self._actions.fit_window,
                self._actions.fit_width,
                None,
                self._actions.brightness_contrast,
                self._actions.toggle_keep_prev_brightness_contrast,
            ),
        )

        utils.add_actions(
            self._canvas_widgets.canvas.menus[1],
            (
                action("&Copy here", self.copy_shape),
                action("&Move here", self.move_shape),
            ),
        )

        return _Menus(
            file=file_menu,
            edit=edit_menu,
            view=view_menu,
            help=help_menu,
            label_list=label_menu,
        )

    def _make_frame_sync_controls(self) -> QtWidgets.QWidgetAction:
        controls = QtWidgets.QWidget(self)
        grid = QtWidgets.QGridLayout(controls)
        grid.setContentsMargins(32, 2, 0, 2)
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(3)
        row_height = max(19, controls.fontMetrics().height() + 3)

        self._frame_sync_toggle = QtWidgets.QPushButton("同步修改", controls)
        self._auto_fill_toggle = QtWidgets.QPushButton("自动补齐", controls)
        self._frame_sync_count = QtWidgets.QSpinBox(controls)
        self._frame_sync_toggle.setCheckable(True)
        self._frame_sync_toggle.setToolTip("向后调整已有的同标签、同 ID 方框。")
        self._auto_fill_toggle.setCheckable(True)
        self._auto_fill_toggle.setToolTip("按共同帧数补齐缺框，开启时同时打开同步修改。")
        self._auto_fill_toggle.toggled.connect(self._enable_sync_for_auto_fill)
        self._frame_sync_count.setRange(1, 2147483647)
        self._frame_sync_count.setFixedSize(54, row_height)
        self._frame_sync_count.setAccessibleName("同步修改和自动补齐帧数")
        frame_row = QtWidgets.QHBoxLayout()
        frame_row.setContentsMargins(0, 0, 0, 0)
        frame_row.setSpacing(4)
        frame_row.addWidget(QtWidgets.QLabel("帧数", controls))
        frame_row.addWidget(self._frame_sync_count)
        grid.addLayout(frame_row, 1, 1, Qt.AlignHCenter)

        for column in (0, 3):
            separator = QtWidgets.QFrame(controls)
            separator.setFrameShape(QtWidgets.QFrame.VLine)
            separator.setFrameShadow(QtWidgets.QFrame.Sunken)
            grid.addWidget(separator, 0, column, 2, 1)
        self._frame_sync_method = QtWidgets.QComboBox(controls)
        self._frame_sync_method.addItem("递减", "decay")
        self._frame_sync_method.addItem("插值", "interpolation")
        self._frame_sync_method.setFixedHeight(row_height)
        self._frame_sync_method.setAccessibleName("同步修改算法")
        self._frame_sync_method.setToolTip("插值使用下一帧和同步范围外的第一帧作参考；参考框缺失时使用递减。")
        grid.addWidget(self._frame_sync_method, 1, 2, Qt.AlignCenter)
        trajectory = QtWidgets.QPushButton("轨迹预测", controls)
        trajectory.setToolTip("选择标签、ID 和帧范围，插值补齐缺框。")
        trajectory.clicked.connect(self._fill_trajectory)
        self._trajectory_directory: Path | None = None
        self._trajectory_ids: dict[str, list[int]] = {}
        object_menu, self._trajectory_label, self._trajectory_id = self._make_trajectory_menu(
            controls, "对象", "标签", "ID", self._populate_trajectory_objects
        )
        range_menu, self._trajectory_start, self._trajectory_end = self._make_trajectory_menu(
            controls, "起止", "起始帧", "结束帧", self._populate_trajectory_range
        )
        self._trajectory_label.currentIndexChanged.connect(self._populate_trajectory_ids)
        trajectory_options = QtWidgets.QHBoxLayout()
        trajectory_options.setContentsMargins(0, 0, 0, 0)
        trajectory_options.setSpacing(4)
        trajectory_options.addWidget(object_menu)
        trajectory_options.addWidget(range_menu)
        grid.addLayout(trajectory_options, 1, 4, Qt.AlignHCenter)

        self._highlight_toggle = QtWidgets.QPushButton("高亮显示", controls)
        self._highlight_toggle.setCheckable(True)
        self._highlight_toggle.setChecked(True)
        self._instant_delete_toggle = QtWidgets.QPushButton("立即删除", controls)
        self._instant_delete_toggle.setCheckable(True)
        self._instant_delete_toggle.setChecked(True)
        buttons = (
            (0, 1, self._frame_sync_toggle), (0, 2, self._auto_fill_toggle),
            (0, 4, trajectory), (0, 5, self._highlight_toggle), (0, 6, self._instant_delete_toggle),
        )
        button_width = max(button.fontMetrics().horizontalAdvance(button.text()) for _, _, button in buttons) + 17
        for row, column, button in buttons:
            button.setFixedSize(button_width, row_height)
            button.setStyleSheet("padding: 0 4px;")
            grid.addWidget(button, row, column, Qt.AlignHCenter)

        self._highlight_transparency = QtWidgets.QSlider(Qt.Horizontal, controls)
        self._highlight_transparency.setRange(0, 100)
        self._highlight_transparency.setValue(40)
        self._highlight_transparency.setFixedWidth(65)
        self._highlight_transparency.setAccessibleName("高亮透明度")
        level = QtWidgets.QLabel("40%", controls)
        level.setFixedWidth(level.fontMetrics().horizontalAdvance("100%") + 2)
        level.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        level.setToolTip("高亮透明度，越大越透明。")
        opacity_row = QtWidgets.QHBoxLayout()
        opacity_row.setContentsMargins(0, 0, 0, 0)
        opacity_row.setSpacing(4)
        opacity_row.addWidget(self._highlight_transparency)
        opacity_row.addWidget(level)
        grid.addLayout(opacity_row, 1, 5, Qt.AlignHCenter)

        delete_hint = QtWidgets.QLabel("Delete", controls)
        delete_hint.setMinimumWidth(delete_hint.fontMetrics().horizontalAdvance("Delete+Enter") + 2)
        delete_hint.setAlignment(Qt.AlignCenter)
        self._instant_delete_toggle.toggled.connect(
            lambda checked: delete_hint.setText("Delete" if checked else "Delete+Enter")
        )
        grid.addWidget(delete_hint, 1, 6, Qt.AlignCenter)

        label = QtWidgets.QLabel("随机颜色", controls)
        grid.addWidget(label, 0, 7, Qt.AlignCenter)
        refresh = QtWidgets.QToolButton(controls)
        refresh.setIcon(controls.style().standardIcon(QtWidgets.QStyle.SP_BrowserReload))
        refresh.setIconSize(QtCore.QSize(15, 15))
        refresh.setFixedSize(25, row_height)
        refresh.setAccessibleName("随机颜色")
        refresh.setToolTip("随机更换全部标注框的边框颜色")
        refresh.clicked.connect(self._randomize_annotation_colors)
        grid.addWidget(refresh, 1, 7, Qt.AlignCenter)
        self._pixel_display_toggle = QtWidgets.QPushButton("显示像素", controls)
        self._pixel_display_toggle.setCheckable(True)
        self._pixel_display_toggle.setFixedSize(button_width, row_height)
        self._pixel_display_toggle.setStyleSheet("padding: 0 4px;")
        self._pixel_display_toggle.setToolTip("放大图片时显示清晰的原始像素块。")
        self._pixel_display_toggle.toggled.connect(self._update_pixel_display)
        grid.addWidget(self._pixel_display_toggle, 0, 8, Qt.AlignHCenter)


        self._highlight_toggle.toggled.connect(self._highlight_transparency.setEnabled)
        self._highlight_toggle.toggled.connect(self._update_box_highlight)
        self._highlight_transparency.valueChanged.connect(self._update_box_highlight)
        self._highlight_transparency.valueChanged.connect(lambda value: level.setText(f"{value}%"))
        controls.setFixedSize(grid.sizeHint())
        action = QtWidgets.QWidgetAction(self)
        action.setDefaultWidget(controls)
        return action

    def _enable_sync_for_auto_fill(self, enabled: bool) -> None:
        if enabled:
            self._frame_sync_toggle.setChecked(True)

    def _update_pixel_display(self, enabled: bool) -> None:
        canvas = self._canvas_widgets.canvas
        canvas.show_pixels = enabled
        canvas.update()

    def _update_box_highlight(self) -> None:
        canvas = self._canvas_widgets.canvas
        canvas.show_box_highlight = self._highlight_toggle.isChecked()
        canvas.box_highlight_alpha = round(255 * (100 - self._highlight_transparency.value()) / 100)
        canvas.update()

    def _setup_toolbars(self) -> None:
        select_ai_model = QtWidgets.QWidgetAction(self)
        select_ai_model.setDefaultWidget(self._ai_annotation)

        ai_prompt_action = QtWidgets.QWidgetAction(self)
        ai_prompt_action.setDefaultWidget(self._ai_text)

        self.addToolBar(
            Qt.TopToolBarArea,
            ToolBar(
                title="Tools",
                actions=[
                    self._actions.open,
                    self._actions.open_dir,
                    self._actions.open_prev_img,
                    self._actions.open_next_img,
                    self._actions.save,
                    self._actions.delete_file,
                    None,
                    self._actions.edit_mode,
                    self._actions.duplicate,
                    self._actions.delete,
                    self._actions.undo,
                    self._actions.brightness_contrast,
                    None,
                    self._actions.fit_window,
                    self._actions.zoom_widget_action,
                    None,
                    select_ai_model,
                    None,
                    ai_prompt_action,
                    self._make_frame_sync_controls(),
                ],
                font_base=self.font(),
            ),
        )
        self.addToolBar(
            Qt.LeftToolBarArea,
            ToolBar(
                title="CreateShapeTools",
                actions=[
                    *[
                        a
                        for mode, a in self._actions.draw
                        if not mode.startswith("ai_")
                    ],
                    None,
                    *[a for mode, a in self._actions.draw if mode.startswith("ai_")],
                ],
                orientation=Qt.Vertical,
                button_style=Qt.ToolButtonTextUnderIcon,
                font_base=self.font(),
            ),
        )
        self._ai_annotation.hover_highlight_requested.connect(
            self._highlight_ai_buttons
        )

    def _setup_app_state(
        self,
        *,
        file_or_dir: str | None,
        output_dir: str | None,
    ) -> None:
        self._output_dir = Path(output_dir) if output_dir else None

        self._image = QtGui.QImage()
        self._label_file_path = None
        self._image_path = None
        self._prev_image_path = None
        self._selected_rectangle_keys: set[tuple[str, int]] = set()
        self._other_data = None
        self._has_image_zoom = False
        self._folder_labels = FolderLabelIndex()
        self._shown_label_directory: Path | None = None
        self._shown_label_counts: dict[str, dict[int | None, int]] = {}
        self._syncing_annotation_checks = False
        self._show_annotations_by_default = True
        self._annotation_visibility: dict[tuple[str, int | None], bool] = {}
        self._label_visibility: dict[str, bool] = {}
        self._brightness_contrast_values = {}
        self._pending_frame_sync = []
        self._frame_sync_undo = {}
        self._scroll_values = {
            Qt.Horizontal: {},
            Qt.Vertical: {},
        }

        if self._config["file_search"]:
            self._docks.file_search.setText(self._config["file_search"])

        self._default_state = self.saveState()
        #
        # XXX: Could be completely declarative.
        # Restore application settings.
        self.settings = QtCore.QSettings("labelme", "mylabelme")
        self._frame_copy_count = max(1, self.settings.value("controls/frameCopyCount", 1, type=int))
        self._frame_sync_count.setValue(self.settings.value("controls/frameSyncCount", 1, type=int))
        method = self.settings.value("controls/frameSyncMethod", "decay", type=str)
        self._frame_sync_method.setCurrentIndex(max(0, self._frame_sync_method.findData(method)))
        self._highlight_transparency.setValue(
            self.settings.value("controls/highlightTransparency", 40, type=int)
        )
        self._last_tone = (
            self.settings.value("view/brightness", 50, type=int),
            self.settings.value("view/contrast", 50, type=int),
        ) if self.settings.contains("view/brightness") else None
        self._restore_tone_on_first_image = self._last_tone is not None
        #
        # Bump this when dock/toolbar layout changes to reset window state
        # for users upgrading from an older version.
        SETTINGS_VERSION: int = 1
        if self.settings.value("settingsVersion", 0, type=int) != SETTINGS_VERSION:
            self._reset_layout()
            self.settings.setValue("settingsVersion", SETTINGS_VERSION)
        #
        geometry = self.settings.value("window/geometry", QtCore.QByteArray(), type=QtCore.QByteArray)
        if not self.restoreGeometry(geometry):
            self.resize(900, 500)
            self.setWindowState(self.windowState() | Qt.WindowMaximized)
        self.restoreState(self.settings.value("window/state", QtCore.QByteArray()))
        for key, action in self._remembered_menu_toggles():
            if self.settings.value(key, action.isChecked(), type=bool) != action.isChecked():
                action.trigger()
        for key, control in self._persistent_view_toggles():
            control.setChecked(self.settings.value(key, control.isChecked(), type=bool))
        self._show_annotations_by_default = self.settings.value(
            "controls/showAllAnnotations", True, type=bool
        )

        last_image = None
        if not file_or_dir:
            last_dir = self.settings.value("files/lastDirectory", "", type=str)
            last_image = self.settings.value("files/lastImage", "", type=str)
            if last_dir and Path(last_dir).is_dir():
                file_or_dir = last_dir
                self._restore_annotation_visibility(last_dir)
            elif last_image and Path(last_image).is_file():
                file_or_dir = str(Path(last_image).parent)
        if file_or_dir:
            self._load_from_file_or_dir(file_or_dir=file_or_dir, preferred_image=last_image)

    def _setup_status_bar(self) -> _StatusBarWidgets:
        message = QtWidgets.QLabel(self.tr("%s started.") % __appname__)
        stats = StatusStats()
        self.statusBar().addWidget(message, 1)
        self.statusBar().addWidget(stats, 0)
        self.statusBar().show()
        return _StatusBarWidgets(message=message, stats=stats)

    def _setup_canvas(self) -> _CanvasWidgets:
        zoom_widget = ZoomWidget()

        canvas = Canvas(
            epsilon=self._config["epsilon"],
            double_click=self._config["canvas"]["double_click"],
            num_backups=self._config["canvas"]["num_backups"],
            crosshair=self._config["canvas"]["crosshair"],
        )
        canvas.zoom_request.connect(self._zoom_requested)
        canvas.mouse_moved.connect(self._update_status_stats)
        canvas.status_updated.connect(
            lambda text: self._status_bar.message.setText(text)
        )

        scroll_area = QtWidgets.QScrollArea()
        scroll_area.setWidget(canvas)
        scroll_area.setWidgetResizable(True)
        scroll_bars = {
            Qt.Vertical: scroll_area.verticalScrollBar(),
            Qt.Horizontal: scroll_area.horizontalScrollBar(),
        }
        canvas.scroll_request.connect(self._on_scroll_request)
        canvas.pan_request.connect(self._on_pan_request)

        canvas.new_shape.connect(self._on_new_shape)
        canvas.inference_produced_no_shapes.connect(
            self._on_inference_produced_no_shapes
        )
        canvas.degenerate_shape_rejected.connect(
            lambda: self.show_status_message(
                self.tr("Shape had no area; nothing created."), 5000
            )
        )
        canvas.shape_moved.connect(self._on_canvas_shape_moved)
        canvas.label_dropped.connect(self._insert_dropped_label)
        canvas.rectangle_double_clicked.connect(self._edit_rectangle_at_canvas)
        canvas.selection_changed.connect(self._on_shape_selection_changed)
        canvas.drawing_polygon.connect(self._on_drawing_polygon_changed)

        self.setCentralWidget(scroll_area)

        return _CanvasWidgets(
            canvas=canvas,
            zoom_widget=zoom_widget,
            scroll_bars=scroll_bars,
        )

    def _setup_dock_widgets(self) -> _DockWidgets:
        flag_list = QtWidgets.QListWidget()
        flag = QtWidgets.QDockWidget(self.tr("Flags"), self)
        flag.setObjectName("Flags")
        if self._config["flags"]:
            self._load_flags(
                flags={k: False for k in self._config["flags"]},
                widget=flag_list,
            )
        flag.setWidget(flag_list)
        flag_list.itemChanged.connect(self.mark_dirty)

        label_list = LabelListWidget()
        label_list.item_selection_changed.connect(self._label_selection_changed)
        label_list.item_double_clicked.connect(self._edit_label)
        label_list.item_changed.connect(self._on_label_item_changed)
        shape = QtWidgets.QDockWidget(self.tr("Annotation List"), self)
        shape.setObjectName("Labels")
        shape.setWidget(label_list)
        self._setup_annotation_title(shape)

        unique_label_list = UniqueLabelQListWidget()
        unique_label_list.setToolTip(
            self.tr("Double-click a label or a Group ID to edit it in this folder.")
        )
        unique_label_list.batch_edit_requested.connect(self._edit_folder_labels)
        unique_label_list.visibility_state = self._label_visibility_state
        unique_label_list.visibility_requested.connect(self._toggle_label_visibility)
        unique_label_list.group_color = self._rgb_for_shape_label
        if self._config["labels"]:
            for lbl in self._config["labels"]:
                unique_label_list.add_label_item(
                    label=lbl,
                    color=self._get_rgb_by_label(
                        label=lbl, unique_label_list=unique_label_list
                    ),
                )
        label = QtWidgets.QDockWidget(self.tr("Label List"), self)
        label.setObjectName("Label List")
        label.setWidget(unique_label_list)
        self._setup_label_dock_title(label)

        file_search = QtWidgets.QLineEdit()
        file_search.setPlaceholderText(self.tr("Search Filename"))
        file_search.textChanged.connect(self._on_file_search_changed)
        file_list = QtWidgets.QListWidget()
        file_list.setItemDelegate(_FilePathDelegate(file_list))
        file_list.itemSelectionChanged.connect(self._file_list_item_selection_changed)
        file_list_layout = QtWidgets.QVBoxLayout()
        file_list_layout.setContentsMargins(0, 0, 0, 0)
        file_list_layout.setSpacing(0)
        file_count = QtWidgets.QLabel("0 个项目")
        file_count.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        search_row = QtWidgets.QHBoxLayout()
        search_row.setContentsMargins(0, 0, 8, 0)
        search_row.setSpacing(8)
        search_row.addWidget(file_search, 1)
        search_row.addWidget(file_count)
        file_list_layout.addLayout(search_row)
        file_list_layout.addWidget(file_list)
        count_timer = QtCore.QTimer(file_list)
        count_timer.setSingleShot(True)
        count_timer.timeout.connect(lambda: file_count.setText(f"{file_list.count()} 个项目"))
        model = file_list.model()
        for signal in (model.rowsInserted, model.rowsRemoved, model.modelReset):
            signal.connect(lambda *_: count_timer.start(0))
        file = QtWidgets.QDockWidget(self.tr("File List"), self)
        file.setObjectName("Files")
        file_list_container = QtWidgets.QWidget()
        file_list_container.setLayout(file_list_layout)
        file.setWidget(file_list_container)

        for config_key, dock_widget in [
            ("flag_dock", flag),
            ("label_dock", label),
            ("shape_dock", shape),
            ("file_dock", file),
        ]:
            features = QtWidgets.QDockWidget.DockWidgetFeatures()
            if self._config[config_key]["closable"]:
                features = features | QtWidgets.QDockWidget.DockWidgetClosable
            if self._config[config_key]["floatable"]:
                features = features | QtWidgets.QDockWidget.DockWidgetFloatable
            if self._config[config_key]["movable"]:
                features = features | QtWidgets.QDockWidget.DockWidgetMovable
            dock_widget.setFeatures(features)
            if self._config[config_key]["show"] is False:
                dock_widget.setVisible(False)
            self.addDockWidget(Qt.RightDockWidgetArea, dock_widget)

        return _DockWidgets(
            flag_dock=flag,
            flag_list=flag_list,
            shape_dock=shape,
            label_list=label_list,
            label_dock=label,
            unique_label_list=unique_label_list,
            file_dock=file,
            file_search=file_search,
            file_list=file_list,
        )

    def _load_config(
        self, config_file: Path | None, config_overrides: dict | None
    ) -> tuple[Path | None, dict]:
        try:
            config = load_config(
                config_file=config_file, config_overrides=config_overrides or {}
            )
        except ValueError as e:
            msg_box = QMessageBox(self)
            msg_box.setIcon(QMessageBox.Warning)
            msg_box.setWindowTitle(self.tr("Configuration Errors"))
            msg_box.setText(
                self.tr(
                    "Errors were found while loading the configuration. "
                    "Please review the errors below and reload your configuration or "
                    "ignore the erroneous lines."
                )
            )
            msg_box.setInformativeText(str(e))
            msg_box.setStandardButtons(QMessageBox.Ignore)
            msg_box.setModal(False)
            msg_box.show()

            config_file = None
            config_overrides = {}
            config = load_config(
                config_file=config_file, config_overrides=config_overrides
            )
        return config_file, config

    def menu(
        self,
        title: str,
        actions: tuple[QtWidgets.QAction | QtWidgets.QMenu | None, ...] | None = None,
    ) -> QtWidgets.QMenu:
        menu = self.menuBar().addMenu(title)
        if actions:
            utils.add_actions(menu, actions)
        return menu

    # Support Functions

    def has_no_shapes(self) -> bool:
        return not len(self._docks.label_list)

    def _populate_canvas_context_menu(self) -> None:
        menu = self._canvas_widgets.canvas.menus[0]
        menu.clear()
        row = _FrameCopyMenuRow(menu)
        layout = QtWidgets.QHBoxLayout(row)
        layout.setContentsMargins(8, 4, 8, 4)
        button = QtWidgets.QPushButton("复制到下一帧", row)
        layout.addWidget(button)
        layout.addWidget(QtWidgets.QLabel("帧数", row))
        count = QtWidgets.QSpinBox(row)
        count.setRange(1, 2147483647)
        count.setValue(self._frame_copy_count)
        count.setKeyboardTracking(False)
        count.setAccessibleName("复制帧数")
        layout.addWidget(count)

        def remember(value: int) -> None:
            self._frame_copy_count = value

        def copy() -> None:
            count.interpretText()
            menu.close()
            self._copy_to_next_frames(count.value())

        count.valueChanged.connect(remember)
        button.clicked.connect(copy)
        action = QtWidgets.QWidgetAction(menu)
        action.setDefaultWidget(row)
        menu.addAction(action)

        menu.addAction(self._actions.delete)
        swatch = QtGui.QPixmap(32, 32)
        swatch.fill(Qt.transparent)
        painter = QtGui.QPainter(swatch)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        painter.setPen(QtGui.QPen(QtGui.QColor("#087e89"), 3))
        painter.setBrush(QtGui.QColor("#22e7e7"))
        painter.drawRoundedRect(5, 5, 22, 22, 4, 4)
        painter.end()
        focus_color = menu.addAction(QtGui.QIcon(swatch), "高亮颜色")
        focus_color.setIconVisibleInMenu(True)
        focus_color.triggered.connect(self._highlight_active_group_color)

        def refresh_options() -> None:
            button.setEnabled(bool(self._canvas_widgets.canvas.selected_shapes and self._following_images()))
            focus_color.setEnabled(self._highlight_target_key() is not None)

        previous = getattr(self, "_refresh_frame_copy_options", None)
        if previous is not None:
            menu.aboutToShow.disconnect(previous)
        self._refresh_frame_copy_options = refresh_options
        menu.aboutToShow.connect(refresh_options)
        utils.add_actions(menu, tuple(
            action for action in self._actions.context_menu if action is not self._actions.delete
        ))

    def _following_images(self) -> list[str]:
        files = self.image_list
        if self._image_path in files:
            return files[files.index(self._image_path) + 1:]
        return []

    def _on_canvas_shape_moved(self) -> None:
        sync_count = self._frame_sync_count.value() if self._frame_sync_toggle.isChecked() else 0
        fill_count = self._frame_sync_count.value() if self._auto_fill_toggle.isChecked() else 0
        canvas = self._canvas_widgets.canvas
        if (sync_count or fill_count) and self._image_path and len(canvas.shape_backups) >= 2:
            current, previous = canvas.shape_backups[-1], canvas.shape_backups[-2]
            keys = Counter((shape.label, shape.group_id) for shape in current)
            deltas = {}
            copies = []
            for old, new in zip(previous, current):
                key = (new.label, new.group_id)
                if (
                    new.shape_type != "rectangle" or old.shape_type != "rectangle"
                    or type(new.group_id) is not int or not new.label or keys[key] != 1
                    or (old.label, old.group_id) != key or old.points == new.points
                    or len(old.points) != 2 or len(new.points) != 2
                ):
                    continue
                before = canvas._rectangle_bounds(old)
                after = canvas._rectangle_bounds(new)
                if before == after:
                    continue
                deltas[key] = tuple(b - a for a, b in zip(before, after))
                if fill_count:
                    copies.append(_shape_to_dict(new))
            if deltas:
                images = self._frame_tool_images()
                if self._image_path in images:
                    start = images.index(self._image_path) + 1
                    frames = images[start:start + max(sync_count, fill_count)]
                    if frames:
                        endpoint = None
                        if (
                            sync_count > 1 and start + sync_count < len(images)
                            and self._frame_sync_method.currentData() == "interpolation"
                        ):
                            endpoint = images[start + sync_count]
                        self._pending_frame_sync.append(
                            (current, frames, deltas, sync_count, copies, fill_count, endpoint)
                        )
        self.mark_dirty()

    def _frame_tool_images(self) -> list[str]:
        if not self._image_path:
            return []
        parent = Path(self._image_path).parent
        if self._docks.file_search.text() or self._image_path not in self.image_list:
            images = _scan_image_files(root_dir=str(parent))
        else:
            images = self.image_list
        return [image for image in images if Path(image).parent == parent]

    def _frame_target(self, image: str) -> Path:
        target = Path(_resolve_label_path(
            image_or_label_path=image, output_dir=self._output_dir, existing=True
        ))
        return target.parent.resolve() / target.name

    def _commit_frame_changes(self, changes: list) -> None:
        for directory in {path.parent for path, _, updated in changes if updated is not None}:
            directory.mkdir(parents=True, exist_ok=True)
        write_folder_label_changes(None, changes)
        self._folder_labels.mark_changed(*(path for path, _, _ in changes))
        changed_names = {
            path.name for path, original, updated in changes
            if original is None or updated is None
        }
        if changed_names:
            files = self._docks.file_list
            for row in range(files.count()):
                item = files.item(row)
                if item is not None and f"{Path(item.text()).stem}{LABEL_FILE_SUFFIX}" in changed_names:
                    item.setCheckState(Qt.Checked if self._frame_target(item.text()).is_file() else Qt.Unchecked)

    def _apply_pending_frame_sync(self) -> None:
        pending, self._pending_frame_sync = self._pending_frame_sync, []
        if not pending:
            return
        snapshots = {id(snapshot) for snapshot in self._canvas_widgets.canvas.shape_backups}
        self._frame_sync_undo = {
            key: entry for key, entry in self._frame_sync_undo.items() if key in snapshots
        }
        updated = set()
        for snapshot, frames, deltas, sync_count, copies, fill_count, endpoint in pending:
            changes = []
            try:
                targets = set()
                first_boxes = {
                    (shape.label, shape.group_id): _shape_to_dict(shape)
                    for shape in snapshot if (shape.label, shape.group_id) in deltas
                }
                end_boxes = {}
                if endpoint is not None:
                    end_boxes = frame_rectangle_anchors(Path(endpoint), self._frame_target(endpoint))
                for offset, image in enumerate(frames):
                    target = self._frame_target(image)
                    if target in targets:
                        raise ValueError(f"多张图片对应同一个标注文件：{target}")
                    targets.add(target)
                    change = None
                    if offset < sync_count:
                        weight = (sync_count - offset) / sync_count
                        existing = frame_rectangle_anchors(Path(image), target)
                        positions = {}
                        for key in first_boxes.keys() & existing.keys():
                            if offset == 0:
                                positions[key] = first_boxes[key]
                            elif key in end_boxes:
                                positions[key] = interpolate_rectangle(
                                    first_boxes[key], end_boxes[key], offset / sync_count
                                )
                            else:
                                positions[key] = interpolate_rectangle(
                                    existing[key], first_boxes[key], weight
                                )
                        change = plan_frame_sync(
                            Path(image), target, deltas, positions=positions, weight=weight,
                        )
                    if offset < fill_count:
                        change, _ = plan_frame_copy(
                            Path(image), target, copies, embed_image=self._config["with_image_data"],
                            prior=change, fit_rectangles=True,
                        )
                    if change is not None:
                        changes.append(change)
                if changes:
                    self._commit_frame_changes(changes)
            except (OSError, ValueError) as error:
                QtWidgets.QMessageBox.warning(
                    self, "后续帧标注", f"已更新 {len(updated)} 帧。\n{error}"
                )
                return
            if changes and id(snapshot) in snapshots:
                self._frame_sync_undo[id(snapshot)] = (snapshot, changes)
            updated.update(path for path, _, _ in changes)
        self.show_status_message(f"已更新 {len(updated)} 张后续帧。", delay=3500)

    def _make_trajectory_menu(
        self, parent: QtWidgets.QWidget, title: str, left: str, right: str,
        populate: Callable[[], None],
    ) -> tuple[QtWidgets.QToolButton, QtWidgets.QComboBox, QtWidgets.QComboBox]:
        button = QtWidgets.QToolButton(parent)
        button.setText(title)
        button.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        menu = QtWidgets.QMenu(button)
        button.setMenu(menu)
        row = QtWidgets.QWidget(menu)
        layout = QtWidgets.QGridLayout(row)
        inputs = []
        for column, name in enumerate((left, right)):
            combo = _TrajectoryComboBox(row)
            combo.setAccessibleName(name)
            combo.setPlaceholderText(name)
            combo.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(18 if title == "起止" else 10)
            combo.setMaxVisibleItems(10)
            combo.setStyleSheet("QComboBox { combobox-popup: 0; }")
            label = QtWidgets.QLabel(name, row)
            label.setBuddy(combo)
            layout.addWidget(label, 0, column)
            layout.addWidget(combo, 1, column)
            inputs.append(combo)
        action = QtWidgets.QWidgetAction(menu)
        action.setDefaultWidget(row)
        menu.addAction(action)
        menu.aboutToShow.connect(populate)
        return button, inputs[0], inputs[1]

    def _prepare_trajectory_controls(self) -> None:
        directory = Path(self._image_path).parent if self._image_path else None
        if directory != self._trajectory_directory:
            self._trajectory_directory = directory
            self._trajectory_ids.clear()
            self._trajectory_label.clear()
            self._trajectory_id.clear()
            self._trajectory_start.clear()
            self._trajectory_end.clear()

    def _populate_trajectory_objects(self) -> None:
        self._prepare_trajectory_controls()
        previous_label = self._trajectory_label.currentData()
        previous_id = self._trajectory_id.currentData()
        ids: dict[str, set[int]] = {}
        try:
            for image in self._frame_tool_images():
                for label, group_id in frame_rectangle_anchors(Path(image), self._frame_target(image)):
                    ids.setdefault(label, set()).add(group_id)
        except (OSError, ValueError) as error:
            self.show_status_message(f"无法读取轨迹对象：{error}", delay=3500)
            return
        self._trajectory_ids = {label: sorted(groups) for label, groups in ids.items()}
        with QtCore.QSignalBlocker(self._trajectory_label):
            self._trajectory_label.clear()
            for label in natsort.natsorted(ids):
                self._trajectory_label.addItem(label, label)
            self._trajectory_label.setCurrentIndex(self._trajectory_label.findData(previous_label))
        self._populate_trajectory_ids()
        self._trajectory_id.setCurrentIndex(self._trajectory_id.findData(previous_id))

    def _populate_trajectory_ids(self) -> None:
        self._trajectory_id.clear()
        for group_id in self._trajectory_ids.get(self._trajectory_label.currentData(), []):
            self._trajectory_id.addItem(str(group_id), group_id)
        self._trajectory_id.setCurrentIndex(-1)

    def _populate_trajectory_range(self) -> None:
        self._prepare_trajectory_controls()
        images = self._frame_tool_images()
        for combo in (self._trajectory_start, self._trajectory_end):
            previous = combo.currentData()
            combo.clear()
            for index, image in enumerate(images, 1):
                combo.addItem(f"{index}  {Path(image).name}", image)
            combo.setCurrentIndex(combo.findData(previous))

    def _fill_trajectory(self) -> None:
        self._prepare_trajectory_controls()
        images = self._frame_tool_images()
        label = self._trajectory_label.currentData()
        group_id = self._trajectory_id.currentData()
        first, last = self._trajectory_start.currentData(), self._trajectory_end.currentData()
        missing = []
        if label is None:
            missing.append("标签")
        if group_id is None:
            missing.append("ID")
        if first not in images:
            missing.append("起始帧")
        if last not in images:
            missing.append("结束帧")
        if missing:
            QtWidgets.QMessageBox.information(
                self, "轨迹预测", "请先选择" + "、".join(missing) + "。"
            )
            return
        start, end = images.index(first), images.index(last)
        if end - start < 2:
            QtWidgets.QMessageBox.information(
                self, "轨迹预测", "结束帧应在起始帧之后，且中间至少有一帧。"
            )
            return
        if not self._can_continue():
            return
        key = label, group_id
        title = "轨迹预测"
        targets = [self._frame_target(image) for image in images[start:end + 1]]
        if len(set(targets)) != len(targets):
            QtWidgets.QMessageBox.warning(self, title, "多张图片对应同一个标注文件。")
            return
        images = images[start:end + 1]
        start, end = 0, len(images) - 1
        try:
            changes = []
            previous = None
            for index in range(start, end + 1):
                anchor = frame_rectangle_anchors(Path(images[index]), targets[index]).get(key)
                if anchor is None:
                    continue
                if previous is not None:
                    left, left_box = previous
                    for gap in range(left + 1, index):
                        shape = interpolate_rectangle(left_box, anchor, (gap - left) / (index - left))
                        change, _ = plan_frame_copy(
                            Path(images[gap]), targets[gap], [shape],
                            embed_image=self._config["with_image_data"], fit_rectangles=True,
                        )
                        if change is not None:
                            changes.append(change)
                previous = index, anchor
            if changes:
                self._commit_frame_changes(changes)
                if self._image_path in images and any(
                    path == targets[images.index(self._image_path)] for path, _, _ in changes
                ):
                    self._load_file(self._image_path, keep_previous_shapes=False)
                else:
                    self._refresh_folder_label_list()
        except (OSError, ValueError) as error:
            QtWidgets.QMessageBox.warning(self, title, str(error))
            return
        message = (
            f"已成功补齐 {len(changes)} 帧。"
            if changes else "没有可补齐的缺框。请确认所选范围内至少有两个同标签、同 ID 的参考框。"
        )
        QtWidgets.QMessageBox.information(self, title, message)

    def _copy_to_next_frames(self, count: int) -> None:
        title = "复制到下一帧"
        selected = self._canvas_widgets.canvas.selected_shapes
        following = self._following_images()
        if not selected or not following:
            return
        targets = following[:count]
        copies = [_shape_to_dict(shape) for shape in selected]
        changes_by_directory = {}
        updates = []
        duplicates = 0
        written = 0
        try:
            pending_paths = set()
            for image in targets:
                target = Path(_resolve_label_path(
                    image_or_label_path=image, output_dir=self._output_dir, existing=True
                ))
                target = target.parent.resolve() / target.name
                if target in pending_paths:
                    raise ValueError(f"多张图片对应同一个标注文件：{target}")
                pending_paths.add(target)
                change, skipped = plan_frame_copy(
                    Path(image), target, copies, embed_image=self._config["with_image_data"]
                )
                duplicates += skipped
                if change is not None:
                    changes_by_directory.setdefault(target.parent, []).append(change)
                    updates.append((image, target))
            for directory, changes in changes_by_directory.items():
                directory.mkdir(parents=True, exist_ok=True)
                write_folder_label_changes(directory, changes)
                self._folder_labels.mark_changed(*(path for path, _, _ in changes))
                written += len(changes)
        except (OSError, ValueError) as error:
            QtWidgets.QMessageBox.warning(self, title, f"已写入 {written} 帧。\n{error}")
        if written:
            files = self._docks.file_list
            for image, target in updates:
                if target.is_file():
                    for item in files.findItems(image, Qt.MatchExactly):
                        item.setCheckState(Qt.Checked)
            self._refresh_folder_label_list()
        self.show_status_message(f"已复制到 {written} 帧，跳过 {duplicates} 个重复标注。", delay=3500)

    def populate_mode_actions(self) -> None:
        self._populate_canvas_context_menu()
        self._menus.edit.clear()
        actions = (
            *[draw_action for _, draw_action in self._actions.draw],
            self._actions.edit_mode,
            *self._actions.edit_menu,
        )
        utils.add_actions(self._menus.edit, actions)

    def _get_window_title(self, *, dirty: bool) -> str:
        file_list = self._docks.file_list
        file_index = file_list.currentRow() if file_list.currentItem() else None
        return _format_window_title(
            image_path=self._image_path,
            file_index=file_index,
            file_count=file_list.count(),
            dirty=dirty,
        )

    def mark_dirty(self) -> None:
        self._apply_annotation_visibility()
        self._refresh_duplicates()
        # Autosave does not clear the undo stack; keep the undo action available.
        self._actions.undo.setEnabled(self._canvas_widgets.canvas.can_restore_shape)

        if self._actions.save_auto.isChecked():
            assert self._image_path is not None
            if self.save_labels(
                label_path=self._label_file_path or _resolve_label_path(
                    image_or_label_path=self._image_path,
                    output_dir=self._output_dir,
                )
            ):
                if self._is_changed:
                    self.mark_clean()
                return
        self._is_changed = True
        self._actions.save.setEnabled(True)
        self.setWindowTitle(self._get_window_title(dirty=True))

    def mark_clean(self) -> None:
        self._refresh_duplicates()
        self._is_changed = False
        self._actions.save.setEnabled(False)
        for _, action in self._actions.draw:
            action.setEnabled(True)
        self.setWindowTitle(self._get_window_title(dirty=False))

        if self.has_label_file():
            self._actions.delete_file.setEnabled(True)
        else:
            self._actions.delete_file.setEnabled(False)

    def update_action_states(self, value: bool = True) -> None:
        for action in (*self._actions.zoom, *self._actions.on_load_active):
            action.setEnabled(value)

    def queue_event(self, function: Callable[[], None]) -> None:
        QtCore.QTimer.singleShot(0, function)

    def show_status_message(self, message: str, delay: int = 500) -> None:
        self.statusBar().showMessage(message, delay)

    def _submit_ai_prompt(self, _: bool) -> None:
        create_mode = self._canvas_widgets.canvas.create_mode
        shape_type = _resolve_text_annotation_shape_type(
            create_mode=create_mode,
            ai_output_format=self._ai_annotation.output_format,
        )
        if shape_type is None:
            logger.warning("Unsupported create_mode={!r}", create_mode)
            return

        texts = self._ai_text.get_text_prompt().split(",")

        model_name: str = self._ai_text.get_model_name()
        model_type = osam.apis.get_model_type_by_name(model_name)
        if not (_is_already_downloaded := model_type.get_size() is not None):
            if not download_ai_model(model_name=model_name, parent=self):
                return
        if (
            self._text_osam_session is None
            or self._text_osam_session.model_name != model_name
        ):
            self._text_osam_session = _automation.OsamSession(model_name=model_name)

        boxes, scores, labels, masks = _automation.get_bboxes_from_texts(
            session=self._text_osam_session,
            image=utils.img_qt_to_arr(self._image)[:, :, :3],
            image_id=str(hash(self._image_path)),
            texts=texts,
        )

        SCORE_FOR_EXISTING_SHAPE: Final[float] = 1.01
        for shape in self._canvas_widgets.canvas.shapes:
            if shape.shape_type != shape_type or shape.label not in texts:
                continue
            shape_bbox = _automation.shape_to_xyxy_bbox(shape=shape)
            if shape_bbox is None:
                continue
            boxes = np.r_[boxes, [shape_bbox]]
            scores = np.r_[scores, [SCORE_FOR_EXISTING_SHAPE]]
            labels = np.r_[labels, [texts.index(shape.label)]]

        boxes, scores, labels, indices = _automation.nms_bboxes(
            boxes=boxes,
            scores=scores,
            labels=labels,
            iou_threshold=self._ai_text.get_iou_threshold(),
            score_threshold=self._ai_text.get_score_threshold(),
            max_num_detections=100,
        )

        is_new = scores != SCORE_FOR_EXISTING_SHAPE
        boxes = boxes[is_new]
        scores = scores[is_new]
        labels = labels[is_new]
        indices = indices[is_new]

        if masks is None:
            masks = [None] * len(boxes)
        else:
            masks = [masks[i] for i in indices]
        del indices

        detections: list[_automation.Detection] = []
        for box, score, label, mask in zip(boxes, scores, labels, masks):
            text: str = texts[label]
            detections.append(
                _automation.Detection(
                    bbox=(
                        float(box[0]),
                        float(box[1]),
                        float(box[2]),
                        float(box[3]),
                    ),
                    mask=mask,
                    label=text,
                    description=json.dumps(dict(score=score.item(), text=text)),
                )
            )
        detections = _automation.suppress_detections_greedy(
            detections=detections,
            iou_threshold=self._ai_text.get_iou_threshold(),
        )
        shapes: list[Shape] = _automation.shapes_from_detections(
            detections=detections, shape_type=shape_type
        )

        self._canvas_widgets.canvas.backup_shapes()
        self._load_shapes(shapes, replace=False)
        self.mark_dirty()

    def reset_state(self) -> None:
        self._pending_frame_sync.clear()
        self._frame_sync_undo.clear()
        self._docks.label_list.clear()
        self._sync_all_annotations_checkbox()
        self._image_path = None
        self._image_data = None
        self._label_file_path = None
        self._other_data = None
        self._canvas_widgets.canvas.reset_state()

    # Callbacks

    def undo_shape_edit(self) -> None:
        canvas = self._canvas_widgets.canvas
        if not canvas.can_restore_shape:
            return
        current, previous = canvas.shape_backups[-1], canvas.shape_backups[-2]
        synchronized = self._frame_sync_undo.get(id(current))
        if synchronized:
            _, changes = synchronized
            try:
                self._commit_frame_changes(
                    [(path, updated, original) for path, original, updated in changes]
                )
            except OSError as error:
                QtWidgets.QMessageBox.warning(self, "同步修改", f"无法撤销后续帧的修改。\n{error}")
                return
            del self._frame_sync_undo[id(current)]
        self._pending_frame_sync = [
            request for request in self._pending_frame_sync if request[0] is not current
        ]
        canvas.restore_last_shape()
        self._docks.label_list.clear()
        self._load_shapes(canvas.shapes)
        replacement = canvas.shape_backups[-1]
        if entry := self._frame_sync_undo.pop(id(previous), None):
            self._frame_sync_undo[id(replacement)] = (replacement, entry[1])
        self._pending_frame_sync = [
            (replacement if snapshot is previous else snapshot, *payload)
            for snapshot, *payload in self._pending_frame_sync
        ]
        if synchronized:
            self.mark_dirty()
        else:
            self._refresh_duplicates()
            self._actions.undo.setEnabled(canvas.can_restore_shape)

    def tutorial(self) -> None:
        url = "https://github.com/labelmeai/labelme/tree/main/examples/tutorial"  # NOQA
        webbrowser.open(url)

    def _on_drawing_polygon_changed(self, drawing: bool = True) -> None:
        # In the middle of drawing, toggling between modes should be disabled.
        self._actions.edit_mode.setEnabled(not drawing)
        self._actions.undo_last_point.setEnabled(drawing)
        self._actions.undo.setEnabled(not drawing)
        self._actions.delete.setEnabled(not drawing)

    def _switch_canvas_mode(
        self, edit: bool = True, create_mode: str | None = None
    ) -> None:
        if create_mode == "ai_points_to_shape":
            model_name = self._canvas_widgets.canvas.get_ai_model_name()
            if model_name in _AI_MODELS_WITHOUT_POINT_SUPPORT:
                QtWidgets.QMessageBox.warning(
                    self,
                    self.tr("AI-Points Unavailable"),
                    self.tr(
                        "%s does not support point prompts.\n"
                        "Please select a different model or use AI-Box mode."
                    )
                    % model_name,
                )
                return
        self._canvas_widgets.canvas.set_editing(edit)
        if create_mode is not None:
            self._canvas_widgets.canvas.create_mode = create_mode
        if edit:
            for _, draw_action in self._actions.draw:
                draw_action.setEnabled(True)
        else:
            for draw_mode, draw_action in self._actions.draw:
                draw_action.setEnabled(create_mode != draw_mode)
        # Keep edit_mode disabled while a partial shape is alive so the user
        # can't abandon it mid-draw.
        self._actions.edit_mode.setEnabled(
            not edit and not self._canvas_widgets.canvas.is_drawing
        )
        self._ai_text.setEnabled(
            not edit
            and create_mode
            in (*get_args(_TextToAnnotationCreateMode), *_AI_CREATE_MODES)
        )
        self._ai_annotation.setEnabled(not edit and create_mode in _AI_CREATE_MODES)
        if create_mode == "ai_points_to_shape":
            self._ai_annotation.set_disabled_models(_AI_MODELS_WITHOUT_POINT_SUPPORT)
        else:
            self._ai_annotation.set_disabled_models(())

    def _highlight_ai_buttons(self, highlight: bool) -> None:
        HIGHLIGHT_COLOR: Final = "#FFFFCC"
        BORDER_COLOR: Final = "#E6E6A0"
        bg = HIGHLIGHT_COLOR if highlight else "transparent"
        border = BORDER_COLOR if highlight else "transparent"
        style = (
            "QToolButton:!checked:!pressed {"
            f" background-color: {bg}; border: 1px solid {border};"
            " }"
        )
        for mode, action in self._actions.draw:
            if mode in _AI_CREATE_MODES:
                for widget in action.associatedWidgets():
                    if isinstance(widget, QtWidgets.QToolButton):
                        widget.setStyleSheet(style)

    def show_label_list_menu(self, point: QtCore.QPoint) -> None:
        # PyQt5 stubs type QMenu.exec() argument too narrowly
        self._menus.label_list.exec(self._docks.label_list.mapToGlobal(point))  # ty: ignore[invalid-argument-type]

    def _dialog_group_counts(self) -> dict[str, dict[int | None, int]]:
        directory = self._active_labels_directory()
        return self._folder_labels.counts(directory) if directory is not None else {}

    def _active_labels_directory(self) -> Path | None:
        if self._image_path:
            return self._output_dir or _default_output_dir(Path(self._image_path).parent)
        if self._prev_opened_dir:
            return self._output_dir or _default_output_dir(Path(self._prev_opened_dir))
        return None

    def _edit_folder_labels(self, label: str, group_id: int | None, edit_id: bool) -> None:
        title = "批量编辑 ID" if edit_id else "批量编辑标签"
        directory = self._active_labels_directory()
        if directory is None:
            return
        if self._is_changed:
            QtWidgets.QMessageBox.information(self, title, "请先保存当前图片的标注。")
            return
        directory = directory.resolve()
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle(title)
        dialog.setMinimumWidth(300)
        form = QtWidgets.QFormLayout(dialog)
        label_edit = QtWidgets.QLineEdit(label, dialog)
        form.addRow("标签", label_edit)
        id_edit = None
        if edit_id:
            id_edit = QtWidgets.QLineEdit("" if group_id is None else str(group_id), dialog)
            pattern = QtCore.QRegularExpression(r"(?:[+-]?[0-9]+)?")
            id_edit.setValidator(QtGui.QRegularExpressionValidator(pattern, id_edit))
            form.addRow("ID", id_edit)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel, parent=dialog
        )
        submit = buttons.button(QtWidgets.QDialogButtonBox.Ok)
        submit.setText("确定")
        delete_result = QtWidgets.QDialog.Accepted + 1
        delete_all = buttons.addButton("全部删除", QtWidgets.QDialogButtonBox.ActionRole)
        delete_all.setAutoDefault(False)
        delete_all.clicked.connect(lambda: dialog.done(delete_result))
        buttons.button(QtWidgets.QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)

        def update_submit() -> None:
            submit.setEnabled(bool(label_edit.text().strip()) and (
                id_edit is None or id_edit.hasAcceptableInput()
            ))

        label_edit.textChanged.connect(update_submit)
        if id_edit is not None:
            id_edit.textChanged.connect(update_submit)
        update_submit()
        focused = id_edit if id_edit is not None else label_edit
        focused.selectAll()
        focused.setFocus()

        panel = self._docks.unique_label_list
        y = QtGui.QCursor.pos().y()
        position_left_of(dialog, panel, y)
        result = dialog.exec_()
        if not result:
            dialog.deleteLater()
            return
        deleting = result == delete_result
        new_label = None if deleting else label_edit.text().strip()
        new_id = int(id_edit.text()) if not deleting and id_edit is not None and id_edit.text() else None
        dialog.deleteLater()
        if new_label == label and (not edit_id or new_id == group_id):
            return
        try:
            changes, count, merge = plan_folder_label_edit(
                directory, label, group_id, new_label, new_id, match_id=edit_id
            )
            if not changes:
                QtWidgets.QMessageBox.information(self, title, "当前目录中没有匹配的已保存标注。")
                return
            if deleting or merge:
                confirm = QtWidgets.QMessageBox(self)
                confirm.setIcon(QtWidgets.QMessageBox.Warning if deleting else QtWidgets.QMessageBox.Question)
                confirm.setWindowTitle("确认删除" if deleting else "确认合并")
                confirm.setTextFormat(Qt.PlainText)
                target = label if deleting else new_label
                target_id = group_id if deleting else new_id
                if edit_id:
                    target += " / " + ("无 ID" if target_id is None else f"ID {target_id}")
                confirm.setText(f"删除「{target}」的全部匹配标注？" if deleting else f"{target} 已存在，是否合并？")
                verb = "删除" if deleting else "更新"
                confirm.setInformativeText(f"将{verb} {len(changes)} 个文件中的 {count} 条标注。\n{directory}")
                confirm.setStandardButtons(QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No)
                confirm.button(QtWidgets.QMessageBox.Yes).setText(verb if deleting else "合并")
                confirm.button(QtWidgets.QMessageBox.No).setText("取消")
                confirm.setDefaultButton(QtWidgets.QMessageBox.No)
                position_left_of(confirm, panel, y)
                answer = confirm.exec_()
                confirm.deleteLater()
                if answer != QtWidgets.QMessageBox.Yes:
                    return
            if (deleting or merge) and plan_folder_label_edit(
                directory, label, group_id, new_label, new_id, match_id=edit_id
            ) != (changes, count, merge):
                raise OSError(self.tr("Annotations changed. Please try again."))
            write_folder_label_changes(directory, changes)
            self._folder_labels.mark_changed(*(path for path, _, _ in changes))
        except (OSError, ValueError) as e:
            QtWidgets.QMessageBox.critical(self, "批量编辑失败", str(e))
            return
        if self._label_file_path and self._image_path and any(
            Path(self._label_file_path).resolve() == path for path, _, _ in changes
        ):
            self._load_file(self._image_path, keep_previous_shapes=False)
        else:
            self._refresh_folder_label_list()
        verb = "删除" if deleting else "更新"
        self.show_status_message(f"已{verb} {len(changes)} 个文件中的 {count} 条标注。")

    def _refresh_folder_label_list(self) -> None:
        widget = self._docks.unique_label_list
        directory = self._active_labels_directory()
        if directory is None:
            widget.clear()
            self._shown_label_directory = None
            self._shown_label_counts = {}
            return
        directory = directory.absolute()
        same_directory = directory == self._shown_label_directory
        counts = self._folder_labels.counts(directory)
        if (same_directory and counts == self._shown_label_counts
                and widget.label_names() == sorted(counts)):
            self._set_group_id_colors(self._color_by_group_id)
            widget.update_visibility_checks()
            return
        selected = widget.currentItem() if same_directory else None
        selected_label = selected.data(Qt.UserRole) if selected and selected.isSelected() else None
        expanded = widget.expanded_labels() if same_directory else set()
        widget.clear()
        self._shown_label_directory = directory
        self._shown_label_counts = counts
        for label, group_counts in sorted(counts.items()):
            color = self._get_rgb_by_label(label=label, unique_label_list=widget)
            widget.add_label_item(label, color, group_counts=group_counts, expanded=label in expanded)
        if selected_label and (item := widget.find_label_item(selected_label)):
            widget.setCurrentItem(item)
        self._set_group_id_colors(self._color_by_group_id)
        widget.update_visibility_checks()

    def validate_label(self, label: str) -> bool:
        policy = self._config["validate_label"]
        if policy is None:
            return True
        existing_labels = list(self._config["labels"] or [])
        existing_labels.extend(self._docks.unique_label_list.label_names())
        return _is_valid_label(
            label=label, existing_labels=existing_labels, policy=policy
        )

    def _edit_rectangle_at_canvas(self, anchor: QtCore.QRect) -> None:
        self._edit_label(next_to=anchor)

    def _edit_label(
        self, value: object | None = None, *, next_to: QtCore.QRect | None = None
    ) -> None:
        items = self._docks.label_list.selected_items()
        if not items:
            logger.warning("No label is selected, so cannot edit label.")
            return

        shapes = [cast(Shape, item.shape()) for item in items]
        first_shape = shapes[0]

        if len(items) == 1:
            edit_text = True
            edit_flags = True
            edit_group_id = True
            edit_description = True
        else:
            edit_text = all(shape.label == first_shape.label for shape in shapes[1:])
            edit_flags = all(shape.flags == first_shape.flags for shape in shapes[1:])
            edit_group_id = all(
                shape.group_id == first_shape.group_id for shape in shapes[1:]
            )
            edit_description = all(
                shape.description == first_shape.description for shape in shapes[1:]
            )

        if not edit_text:
            self._label_dialog.edit.setDisabled(True)
            self._label_dialog.label_list.setDisabled(True)
        if not edit_group_id:
            self._label_dialog.edit_group_id.setDisabled(True)
        if not edit_description:
            self._label_dialog.edit_description.setDisabled(True)

        text, flags, group_id, description = self._label_dialog.popup(
            text=first_shape.label if edit_text else "",
            flags=first_shape.flags if edit_flags else None,
            group_id=first_shape.group_id if edit_group_id else None,
            description=first_shape.description if edit_description else None,
            flags_disabled=not edit_flags,
            left_of=self._docks.shape_dock,
            next_to=next_to,
        )

        if not edit_text:
            self._label_dialog.edit.setDisabled(False)
            self._label_dialog.label_list.setDisabled(False)
        if not edit_group_id:
            self._label_dialog.edit_group_id.setDisabled(False)
        if not edit_description:
            self._label_dialog.edit_description.setDisabled(False)

        if text is None:
            assert flags is None
            assert group_id is None
            assert description is None
            return

        if not self.validate_label(text):
            self.show_error_message(
                self.tr("Invalid label"),
                self.tr("Invalid label '{}' with validation type '{}'").format(
                    text, self._config["validate_label"]
                ),
            )
            return

        self._canvas_widgets.canvas.backup_shapes()
        for item in items:
            shape = item.shape()
            assert shape is not None

            if edit_text:
                shape.label = text
            if edit_flags:
                shape.flags = flags
            if edit_group_id:
                shape.group_id = group_id
            if edit_description:
                shape.description = description

            assert shape.label is not None
            self._update_shape_color(shape)
            if self._docks.unique_label_list.find_label_item(shape.label) is None:
                self._docks.unique_label_list.add_label_item(
                    label=shape.label,
                    color=self._get_rgb_by_label(
                        label=shape.label,
                        unique_label_list=self._docks.unique_label_list,
                    ),
                )

        if edit_text or edit_group_id:
            self._remember_rectangle_selection(self._canvas_widgets.canvas.selected_shapes)
        self.mark_dirty()

    def _on_file_search_changed(self) -> None:
        if not self._prev_opened_dir:
            return
        files = self._docks.file_list
        with QtCore.QSignalBlocker(files):
            self._populate_file_list(self._prev_opened_dir, self._docks.file_search.text())
            if self._image_path in self.image_list:
                files.setCurrentRow(self.image_list.index(self._image_path))
        self.setWindowTitle(self._get_window_title(dirty=self._is_changed))

    def _file_list_item_selection_changed(self) -> None:
        if not self._can_continue():
            return
        if not (items := self._docks.file_list.selectedItems()):
            return
        self._load_file(image_or_label_path=items[0].text())

    # React to canvas signals.
    def _remember_rectangle_selection(self, shapes: list[Shape]) -> None:
        keys = {
            (shape.label, shape.group_id)
            for shape in shapes
            if shape.shape_type == "rectangle" and shape.label is not None
            and type(shape.group_id) is int
        }
        if keys:
            counts = Counter(
                (shape.label, shape.group_id)
                for shape in self._canvas_widgets.canvas.shapes
                if shape.shape_type == "rectangle"
            )
            keys = {key for key in keys if counts[key] == 1}
        self._selected_rectangle_keys = keys

    def _on_shape_selection_changed(
        self, selected_shapes: list[Shape], *, remember: bool = True
    ) -> None:
        if remember:
            self._remember_rectangle_selection(selected_shapes)
        self._docks.label_list.item_selection_changed.disconnect(
            self._label_selection_changed
        )
        for shape in self._canvas_widgets.canvas.selected_shapes:
            shape.selected = False
        self._docks.label_list.clearSelection()
        self._canvas_widgets.canvas.selected_shapes = selected_shapes
        for shape in self._canvas_widgets.canvas.selected_shapes:
            shape.selected = True
            item = self._docks.label_list.find_item_by_shape(shape)
            self._docks.label_list.select_item(item)
            self._docks.label_list.scroll_to_item(item)
        self._docks.label_list.item_selection_changed.connect(
            self._label_selection_changed
        )
        n_selected = len(selected_shapes) > 0
        self._actions.delete.setEnabled(n_selected)
        self._actions.duplicate.setEnabled(n_selected)
        self._actions.copy.setEnabled(n_selected)
        self._actions.edit.setEnabled(n_selected)

    def add_label(self, shape: Shape) -> None:
        assert shape.label is not None
        label_list_item = LabelListWidgetItem(shape=shape)
        self._docks.label_list.add_item(label_list_item)
        if self._docks.unique_label_list.find_label_item(shape.label) is None:
            self._docks.unique_label_list.add_label_item(
                label=shape.label,
                color=self._get_rgb_by_label(
                    label=shape.label,
                    unique_label_list=self._docks.unique_label_list,
                ),
            )
        self._label_dialog.add_label_history(shape.label)
        for action in self._actions.on_shapes_present:
            action.setEnabled(True)

        self._update_shape_color(shape)
        label_list_item.setText(format_shape_label(shape))

    def _setup_label_dock_title(self, dock: QtWidgets.QDockWidget) -> None:
        title = QtWidgets.QWidget(dock)
        layout = QtWidgets.QHBoxLayout(title)
        layout.setContentsMargins(4, 0, 2, 0)
        layout.setSpacing(3)
        layout.addWidget(QtWidgets.QLabel(dock.windowTitle(), title), 1)
        toggle = self._group_color_toggle = QtWidgets.QPushButton(title)
        toggle.setText("ID 配色")
        toggle.setToolTip("按标签和 Group ID 着色。取消选中可恢复按标签配色。")
        toggle.setCheckable(True)
        toggle.setChecked(self._color_by_group_id)
        toggle.toggled.connect(self._set_group_id_colors)
        layout.addWidget(toggle)
        captions = self._caption_toggle = QtWidgets.QPushButton("显示标签", title)
        captions.setCheckable(True)
        captions.setChecked(True)
        captions.setToolTip("显示标签首字符和 Group ID")
        captions.toggled.connect(self._set_shape_captions)
        layout.addWidget(captions)
        self._add_dock_title_controls(dock, title, layout, "label_dock")

    def _add_dock_title_controls(self, dock, title, layout, config_key: str) -> None:
        if self._config[config_key]["floatable"]:
            floating = QtWidgets.QToolButton(title)
            floating.setAutoRaise(True)
            floating.setIcon(dock.style().standardIcon(QtWidgets.QStyle.SP_TitleBarNormalButton))
            floating.setToolTip(self.tr("Float"))
            floating.clicked.connect(lambda: dock.setFloating(not dock.isFloating()))
            layout.addWidget(floating)
        if self._config[config_key]["closable"]:
            close = QtWidgets.QToolButton(title)
            close.setAutoRaise(True)
            close.setIcon(dock.style().standardIcon(QtWidgets.QStyle.SP_TitleBarCloseButton))
            close.setToolTip(self.tr("Close"))
            close.clicked.connect(dock.close)
            layout.addWidget(close)
        dock.setTitleBarWidget(title)

    def _setup_file_dock_title(self, dock: QtWidgets.QDockWidget) -> None:
        title = QtWidgets.QWidget(dock)
        layout = QtWidgets.QHBoxLayout(title)
        layout.setContentsMargins(4, 0, 2, 0)
        layout.setSpacing(3)
        layout.addWidget(QtWidgets.QLabel(dock.windowTitle(), title), 1)
        button = QtWidgets.QPushButton("保持缩放", title)
        button.setCheckable(True)
        action = self._actions.keep_prev_zoom
        button.setChecked(action.isChecked())
        button.setToolTip("按下时切图保持当前缩放比例，松开时恢复适合窗口。")
        button.clicked.connect(action.trigger)
        action.toggled.connect(button.setChecked)
        layout.addWidget(button)
        paths = self._file_path_toggle = QtWidgets.QPushButton("显示路径", title)
        paths.setCheckable(True)
        paths.setToolTip("按下时显示完整路径，松开时只显示文件名。")
        paths.toggled.connect(self._set_file_list_paths_visible)
        layout.addWidget(paths)
        self._add_dock_title_controls(dock, title, layout, "file_dock")

    def _set_file_list_paths_visible(self, enabled: bool) -> None:
        files = self._docks.file_list
        files.itemDelegate().show_full_path = enabled
        files.doItemsLayout()
        files.viewport().update()

    def _setup_annotation_title(self, dock: QtWidgets.QDockWidget) -> None:
        title = QtWidgets.QWidget(dock)
        layout = QtWidgets.QHBoxLayout(title)
        layout.setContentsMargins(4, 0, 2, 0)
        layout.setSpacing(3)
        layout.addWidget(QtWidgets.QLabel(dock.windowTitle(), title))
        self._all_annotations_visible = QtWidgets.QCheckBox(title)
        self._all_annotations_visible.setToolTip("显示或隐藏全部标注框")
        self._all_annotations_visible.setAccessibleName("显示或隐藏全部标注框")
        self._all_annotations_visible.setEnabled(False)
        self._all_annotations_visible.clicked.connect(self.toggle_shape_visibility)
        layout.addWidget(self._all_annotations_visible)
        layout.addStretch(1)
        self._duplicate_toggle = QtWidgets.QPushButton("重复检测", title)
        self._duplicate_toggle.setCheckable(True)
        self._duplicate_toggle.setChecked(True)
        self._duplicate_toggle.toggled.connect(self._refresh_duplicates)
        layout.addWidget(self._duplicate_toggle)
        advanced = self._rectangle_edit_toggle = QtWidgets.QPushButton("编辑模式", title)
        advanced.setCheckable(True)
        advanced.setChecked(True)
        advanced.setToolTip("开启后可拖动矩形的四条边和四个角。")
        advanced.toggled.connect(self._set_rectangle_edit_mode)
        layout.addWidget(advanced)
        self._add_dock_title_controls(dock, title, layout, "shape_dock")

    def _set_rectangle_edit_mode(self, enabled: bool) -> None:
        canvas = self._canvas_widgets.canvas
        canvas.set_rectangle_edge_editing(enabled)
        if enabled and not canvas.is_drawing:
            self._switch_canvas_mode(edit=True)

    def _refresh_duplicates(self, _checked: bool = False) -> None:
        rows = [(item, shape) for item in self._docks.label_list if (shape := item.shape()) is not None]
        groups = {}
        if self._duplicate_toggle.isChecked():
            for item, shape in rows:
                if shape.shape_type in {"rectangle", "oriented_rectangle"} and shape.group_id is not None:
                    groups.setdefault((shape.label, shape.group_id), []).append(item)
        colors = {}
        for index, items in enumerate(group for group in groups.values() if len(group) > 1):
            hue = (index * 137) % 360
            ink = QtGui.QColor.fromHsv(hue, 210, 125)
            tint = QtGui.QColor.fromHsv(hue, 55, 255)
            colors.update((item, (ink, tint)) for item in items)
        for item, shape in rows:
            text = format_shape_label(shape)
            if item in colors:
                ink, tint = colors[item]
                item.setText(f'<font color="{ink.name()}"><b>{text}</b></font>')
                item.setBackground(QtGui.QBrush(tint))
            else:
                item.setText(text)
                item.setBackground(QtGui.QBrush())
        self._sync_all_annotations_checkbox()

    def _sync_all_annotations_checkbox(self) -> None:
        states = {item.checkState() for item in self._docks.label_list}
        checkbox = self._all_annotations_visible
        checkbox.setEnabled(bool(states))
        partial = len(states) > 1
        checkbox.setTristate(partial)
        checkbox.setCheckState(Qt.PartiallyChecked if partial else next(iter(states), Qt.Unchecked))

    def _set_group_id_colors(self, enabled: bool) -> None:
        self._color_by_group_id = enabled
        for item in self._docks.label_list:
            if (shape := item.shape()) is not None:
                self._update_shape_color(shape)
        self._refresh_duplicates()
        self._docks.unique_label_list.viewport().update()
        self._canvas_widgets.canvas.update()

    def _set_shape_captions(self, enabled: bool) -> None:
        canvas = self._canvas_widgets.canvas
        canvas.show_rectangle_captions = enabled
        canvas.update()

    def _highlight_target_key(self) -> tuple[str, int] | None:
        canvas = self._canvas_widgets.canvas
        shape = canvas.hovered_shape
        if shape is None or shape not in canvas.selected_shapes:
            shape = canvas.selected_shapes[0] if len(canvas.selected_shapes) == 1 else None
        if shape is not None and shape.label is not None and shape.group_id is not None:
            return shape.label, shape.group_id
        return None

    def _highlight_active_group_color(self) -> None:
        target = self._highlight_target_key()
        if target is None or target in self._bright_targets:
            return
        bright = ((0, 255, 255), (105, 255, 40), (255, 65, 200), (255, 230, 40))
        highlight = bright[self._bright_color_index % len(bright)]
        self._bright_color_index += 1
        previous = self._rgb_for_target(target)
        displaced = next((other for other, color in self._target_colors.items()
                          if other != target and color == highlight), None)
        if displaced is not None:
            self._target_colors[displaced] = previous
            self._bright_targets.discard(displaced)
        self._target_colors[target] = highlight
        self._bright_targets.add(target)
        self._set_group_id_colors(self._color_by_group_id)

    def _randomize_annotation_colors(self) -> None:
        shapes = self._canvas_widgets.canvas.shapes
        targets = set(self._target_colors)
        targets.update((shape.label, shape.group_id) for shape in shapes if shape.label is not None)
        targets.update((label, group) for label, ids in self._shown_label_counts.items() for group in ids)
        widget = self._docks.unique_label_list
        labels = set(widget.label_names()) | set(self._random_label_colors)
        labels.update(shape.label for shape in shapes if shape.label is not None)
        used = {self._rgb_for_target(target) for target in targets}
        used.update(self._get_rgb_by_label(label, widget) for label in labels)

        def choose_color() -> tuple[int, int, int]:
            while True:
                color = QtGui.QColor.fromHsv(
                    random.randrange(360), random.randrange(145, 225), random.randrange(190, 250)
                ).getRgb()[:3]
                if color not in used:
                    used.add(color)
                    return color

        order = sorted(targets, key=lambda target: (target[0], target[1] is None, target[1] or 0))
        self._target_colors = {target: choose_color() for target in order}
        self._random_label_colors = {label: choose_color() for label in sorted(labels)}
        self._bright_targets.clear()
        self._bright_color_index = 0
        self._set_group_id_colors(self._color_by_group_id)

    def _rgb_for_target(self, target: tuple[str, int | None]) -> tuple[int, int, int]:
        if target not in self._target_colors:
            used = set(self._target_colors.values())
            while True:
                index = self._next_group_color
                self._next_group_color += 1
                if index < len(LABEL_COLORMAP):
                    rgb = _rgb_from_colormap_id(label_id=index)
                else:
                    color = (index * 0x9E3779) & 0xFFFFFF
                    rgb = (color >> 16, (color >> 8) & 255, color & 255)
                if rgb not in used:
                    self._target_colors[target] = rgb
                    break
        return self._target_colors[target]

    def _rgb_for_shape_label(self, label: str, group_id: int | None) -> tuple[int, int, int]:
        target = (label, group_id)
        if self._color_by_group_id or target in self._bright_targets:
            return self._rgb_for_target(target)
        color = self._get_rgb_by_label(label, unique_label_list=self._docks.unique_label_list)
        if any(color == self._target_colors[key] for key in self._bright_targets):
            return self._rgb_for_target(target)
        return color

    def _update_shape_color(self, shape: Shape) -> None:
        assert shape.label is not None
        use_color_when_selected = (
            self._color_by_group_id or (shape.label, shape.group_id) in self._bright_targets
            or shape.label in self._random_label_colors
        )
        r, g, b = self._rgb_for_shape_label(shape.label, shape.group_id)
        shape.line_color = QtGui.QColor(r, g, b)
        shape.vertex_fill_color = QtGui.QColor(r, g, b)
        shape.hvertex_fill_color = QtGui.QColor(255, 255, 255)
        shape.fill_color = QtGui.QColor(r, g, b, 128)
        shape.select_line_color = QtGui.QColor(r, g, b) if use_color_when_selected else QtGui.QColor(255, 255, 255)
        shape.select_fill_color = QtGui.QColor(r, g, b, 155)

    def _get_rgb_by_label(
        self,
        label: str,
        unique_label_list: UniqueLabelQListWidget,
    ) -> tuple[int, int, int]:
        if label in self._random_label_colors:
            return self._random_label_colors[label]
        if self._config["shape_color"] == "auto":
            item_index = unique_label_list.label_index(label)
            label_id: int = (
                1  # skip black color by default
                + item_index
                + self._config["shift_auto_shape_color"]
            )
            return _rgb_from_colormap_id(label_id=label_id)
        if self._config["shape_color"] == "manual":
            rgb = _rgb_from_label_colors(
                label=label, label_colors=self._config["label_colors"]
            )
            if rgb is not None:
                return rgb
        if self._config["default_shape_color"]:
            return self._config["default_shape_color"]
        return (0, 255, 0)

    def remove_labels(self, shapes: list[Shape]) -> None:
        for shape in shapes:
            item = self._docks.label_list.find_item_by_shape(shape)
            self._docks.label_list.remove_item(item)

    def _load_shapes(self, shapes: list[Shape], replace: bool = True) -> None:
        for shape in shapes:
            shape.visible = self._annotation_visibility.get(
                (shape.label, shape.group_id),
                self._label_visibility.get(shape.label, self._show_annotations_by_default),
            )
        self._docks.label_list.item_selection_changed.disconnect(
            self._label_selection_changed
        )
        shape: Shape
        for shape in shapes:
            self.add_label(shape)
        self._docks.label_list.clearSelection()
        self._docks.label_list.item_selection_changed.connect(
            self._label_selection_changed
        )
        self._canvas_widgets.canvas.load_shapes(shapes=shapes, replace=replace)

    def _load_flags(
        self,
        flags: dict[str, bool],
        widget: QtWidgets.QListWidget,
    ) -> None:
        widget.clear()
        key: str
        flag: bool
        for key, flag in flags.items():
            item: QtWidgets.QListWidgetItem = QtWidgets.QListWidgetItem(key)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if flag else Qt.Unchecked)
            widget.addItem(item)

    def save_labels(self, label_path: str) -> bool:
        shapes = [
            _shape_to_dict(s)
            for item in self._docks.label_list
            if (s := item.shape()) is not None
        ]
        flags: dict[str, bool] = {}
        for i in range(self._docks.flag_list.count()):
            item = self._docks.flag_list.item(i)
            assert item
            flags[item.text()] = item.checkState() == Qt.Checked
        try:
            assert self._image_path
            label_dir = Path(label_path).parent
            image_path = annotation_image_reference(Path(self._image_path), label_dir)
            image_data = self._image_data if self._config["with_image_data"] else None
            label_dir.mkdir(parents=True, exist_ok=True)
            write_label_file(
                filename=label_path,
                shapes=shapes,
                image_path=image_path,
                image_data=image_data,
                image_height=self._image.height(),
                image_width=self._image.width(),
                other_data=self._other_data,
                flags=flags,
            )
            self._folder_labels.mark_changed(Path(label_path))
            self._label_file_path = label_path
            items = self._docks.file_list.findItems(self._image_path, Qt.MatchExactly)
            if len(items) > 0:
                if len(items) != 1:
                    raise RuntimeError("There are duplicate files.")
                items[0].setCheckState(Qt.Checked)
            self._apply_pending_frame_sync()
            self._refresh_folder_label_list()
            return True
        except (LabelFileError, OSError) as e:
            self.show_error_message(
                self.tr("Error saving label data"), self.tr("<b>%s</b>") % e
            )
            return False

    def _insert_dropped_label(self, label: str, group_id: int | None, point: QtCore.QPointF) -> None:
        if not self._image_path or not self.validate_label(label):
            return
        canvas = self._canvas_widgets.canvas
        if group_id is not None and any(
            s.label == label and s.group_id == group_id for s in canvas.shapes
        ):
            answer = QtWidgets.QMessageBox.question(
                self, "拖入标注", f"{label} / ID {group_id} 目标已存在，是否继续？",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No,
            )
            if answer != QtWidgets.QMessageBox.Yes:
                return
        width, height = self._image.width() - 1, self._image.height() - 1
        if width < 1 or height < 1:
            return
        box_width, box_height = min(90, width), min(120, height)
        x = min(max(point.x() - box_width / 2, 0), width - box_width)
        y = min(max(point.y() - box_height / 2, 0), height - box_height)
        flags = {
            flag: False for pattern, names in (self._config["label_flags"] or {}).items()
            if re.match(pattern, label) for flag in names
        }
        shape = Shape(label=label, group_id=group_id, shape_type="rectangle", flags=flags)
        shape.add_point(QtCore.QPointF(x, y))
        shape.add_point(QtCore.QPointF(x + box_width, y + box_height))
        shape.close()
        self._switch_canvas_mode(edit=True)
        self._insert_shapes([shape])

    def _insert_shapes(self, shapes: list[Shape]) -> None:
        if not shapes:
            return
        self._load_shapes(shapes=shapes, replace=False)
        self._canvas_widgets.canvas.select_shapes(shapes)
        self.mark_dirty()

    def _label_selection_changed(self) -> None:
        selected_shapes: list[Shape] = []
        for item in self._docks.label_list.selected_items():
            shape = item.shape()
            assert shape is not None
            selected_shapes.append(shape)
        if selected_shapes:
            self._canvas_widgets.canvas.select_shapes(selected_shapes)
        else:
            if self._canvas_widgets.canvas.deselect_shape():
                self._canvas_widgets.canvas.update()

    def _label_visibility_state(self, label: str, group_id: int | None, match_id: bool):
        def visible(group):
            return self._annotation_visibility.get(
                (label, group), self._label_visibility.get(label, self._show_annotations_by_default)
            )
        if match_id:
            return Qt.Checked if visible(group_id) else Qt.Unchecked
        groups = set(self._shown_label_counts.get(label, {}))
        groups.update(shape.group_id for shape in self._canvas_widgets.canvas.shapes if shape.label == label)
        values = {visible(group) for group in groups} or {self._label_visibility.get(label, self._show_annotations_by_default)}
        if len(values) > 1:
            return Qt.PartiallyChecked
        return Qt.Checked if values.pop() else Qt.Unchecked

    def _toggle_label_visibility(self, label: str, group_id: int | None, match_id: bool, visible: bool) -> None:
        if match_id:
            self._annotation_visibility[(label, group_id)] = visible
        else:
            self._label_visibility[label] = visible
            self._annotation_visibility = {
                key: value for key, value in self._annotation_visibility.items() if key[0] != label
            }
        self._set_annotation_checks(
            (item, visible) for item in self._docks.label_list
            if item.shape().label == label and (not match_id or item.shape().group_id == group_id)
        )
        self._canvas_widgets.canvas.update()

    def _apply_annotation_visibility(self) -> None:
        canvas = self._canvas_widgets.canvas
        self._syncing_annotation_checks = True
        try:
            for item in self._docks.label_list:
                shape = item.shape()
                if shape is None:
                    continue
                visible = self._annotation_visibility.get(
                    (shape.label, shape.group_id),
                    self._label_visibility.get(shape.label, self._show_annotations_by_default),
                )
                canvas.set_shape_visible(shape, visible)
                item.setCheckState(Qt.Checked if visible else Qt.Unchecked)
        finally:
            self._syncing_annotation_checks = False
        self._sync_all_annotations_checkbox()
        self._docks.unique_label_list.update_visibility_checks()

    def _remember_annotation_visibility(self, shape: Shape, visible: bool) -> None:
        if shape.label is None:
            return
        key = (shape.label, shape.group_id)
        if visible == self._label_visibility.get(shape.label, self._show_annotations_by_default):
            self._annotation_visibility.pop(key, None)
        else:
            self._annotation_visibility[key] = visible

    def _on_label_item_changed(self, item: LabelListWidgetItem) -> None:
        shape = item.shape()
        is_visible_new = item.checkState() == Qt.Checked
        if self._syncing_annotation_checks or shape is None or shape.visible == is_visible_new:
            return

        selected_group = (
            self._docks.label_list.selection_at_press()
            or self._docks.label_list.selected_items()
        )
        items_to_toggle = (
            selected_group
            if item in selected_group and len(selected_group) > 1
            else [item]
        )
        self._set_annotation_checks((candidate, is_visible_new) for candidate in items_to_toggle)

    def _set_annotation_checks(
        self, updates: Iterable[tuple[LabelListWidgetItem, bool]]
    ) -> None:
        canvas = self._canvas_widgets.canvas
        changed = False
        self._syncing_annotation_checks = True
        try:
            for item, visible in updates:
                shape = item.shape()
                if shape is None or shape.visible == visible:
                    continue
                canvas.set_shape_visible(shape=shape, value=visible)
                self._remember_annotation_visibility(shape, visible)
                item.setCheckState(Qt.Checked if visible else Qt.Unchecked)
                changed = True
        finally:
            self._syncing_annotation_checks = False
        if changed:
            canvas.backup_shapes()
            self._actions.undo.setEnabled(canvas.can_restore_shape)
        self._sync_all_annotations_checkbox()
        self._docks.unique_label_list.update_visibility_checks()

    # Callback functions:

    def _on_new_shape(self) -> None:
        items = self._docks.unique_label_list.selectedItems()
        text = None
        if items:
            text = items[0].data(Qt.UserRole)
        flags = {}
        group_id = None
        description = ""
        if self._config["display_label_popup"] or not text:
            previous_text = self._label_dialog.edit.text()
            text, flags, group_id, description = self._label_dialog.popup(text)
            if not text:
                self._label_dialog.edit.setText(previous_text)

        if text and not self.validate_label(text):
            self.show_error_message(
                self.tr("Invalid label"),
                self.tr("Invalid label '{}' with validation type '{}'").format(
                    text, self._config["validate_label"]
                ),
            )
            text = ""
        if text:
            self._docks.label_list.clearSelection()
            assert isinstance(flags, dict)
            shapes = self._canvas_widgets.canvas.set_last_label(text, flags)
            for shape in shapes:
                shape.group_id = group_id
                shape.description = description
                self.add_label(shape)
            self._actions.edit_mode.setEnabled(True)
            self._actions.undo_last_point.setEnabled(False)
            self._actions.undo.setEnabled(True)
            self.mark_dirty()
        else:
            self._canvas_widgets.canvas.undo_last_line()
            self._canvas_widgets.canvas.shape_backups.pop()

    def _on_inference_produced_no_shapes(self) -> None:
        self.show_status_message(
            self.tr("AI inference produced no new annotation."), 5000
        )

    def _on_scroll_request(self, delta: int, orientation: Qt.Orientation) -> None:
        units = -delta * 0.1  # natural scroll
        bar = self._canvas_widgets.scroll_bars[orientation]
        value = bar.value() + bar.singleStep() * units
        self.set_scroll_value(orientation, value)

    def _on_pan_request(self, step: QtCore.QPoint) -> None:
        canvas = self._canvas_widgets.canvas
        if canvas.pixmap.isNull():
            return
        origin = canvas._compute_image_origin_offset() * canvas.scale
        bars = self._canvas_widgets.scroll_bars
        old = {orientation: bar.value() for orientation, bar in bars.items()}
        self.set_scroll_value(Qt.Horizontal, old[Qt.Horizontal] - step.x())
        self.set_scroll_value(Qt.Vertical, old[Qt.Vertical] - step.y())
        # Continue panning past a scrollbar limit by moving the image origin.
        canvas._zoom_image_origin = origin + QtCore.QPointF(
            step.x() + bars[Qt.Horizontal].value() - old[Qt.Horizontal],
            step.y() + bars[Qt.Vertical].value() - old[Qt.Vertical],
        )
        self._zoom_mode = _ZoomMode.MANUAL_ZOOM
        self._sync_zoom_mode_actions()
        canvas.update()

    def set_scroll_value(self, orientation: Qt.Orientation, value: float) -> None:
        bar = self._canvas_widgets.scroll_bars[orientation]
        bar.setValue(round(value))
        if self._image_path is not None:
            self._scroll_values[orientation][self._image_path] = bar.value()

    def _set_zoom(self, value: int, pos: QtCore.QPointF | None = None) -> None:
        if self._image_path is None:
            logger.warning("image_path is None, cannot set zoom")
            return

        if pos is None:
            pos = QtCore.QPointF(
                self._canvas_widgets.canvas.visibleRegion().boundingRect().center()
            )
        canvas = self._canvas_widgets.canvas
        image_point = canvas._transform_point_widget_to_image(pos)
        old_scroll = {
            orientation: bar.value()
            for orientation, bar in self._canvas_widgets.scroll_bars.items()
        }

        if self._zoom_mode == _ZoomMode.MANUAL_ZOOM:
            zoom = self._canvas_widgets.zoom_widget
            scale = min(max(value, zoom.minimum()), zoom.maximum()) / 100
            canvas._zoom_image_origin = QtCore.QPointF(
                pos.x() - old_scroll[Qt.Horizontal] - image_point.x() * scale,
                pos.y() - old_scroll[Qt.Vertical] - image_point.y() * scale,
            )
        else:
            canvas._zoom_image_origin = None
        self._sync_zoom_mode_actions()
        self._canvas_widgets.zoom_widget.setValue(value)  # triggers self._paint_canvas
        self._has_image_zoom = True

        if canvas._zoom_image_origin is not None:
            canvas._zoom_image_origin = QtCore.QPointF(
                min(max(canvas._zoom_image_origin.x(), 0.0), max(0, canvas.width() - canvas.pixmap.width() * canvas.scale)),
                min(max(canvas._zoom_image_origin.y(), 0.0), max(0, canvas.height() - canvas.pixmap.height() * canvas.scale)),
            )
        new_point = (image_point + canvas._compute_image_origin_offset()) * canvas.scale
        desired = QtCore.QPointF(
            old_scroll[Qt.Horizontal] + new_point.x() - pos.x(),
            old_scroll[Qt.Vertical] + new_point.y() - pos.y(),
        )
        self.set_scroll_value(Qt.Horizontal, desired.x())
        self.set_scroll_value(Qt.Vertical, desired.y())
        if self._zoom_mode == _ZoomMode.MANUAL_ZOOM:
            # Preserve the anchor even when the new scroll range is too small.
            origin = canvas._compute_image_origin_offset() * canvas.scale
            canvas._zoom_image_origin = origin + QtCore.QPointF(
                self._canvas_widgets.scroll_bars[Qt.Horizontal].value() - desired.x(),
                self._canvas_widgets.scroll_bars[Qt.Vertical].value() - desired.y(),
            )
            canvas.update()

    def _set_zoom_to_original(self) -> None:
        self._zoom_mode = _ZoomMode.MANUAL_ZOOM
        self._set_zoom(value=100)

    def _add_zoom(self, increment: float, pos: QtCore.QPointF | None = None) -> None:
        zoom_value: int
        if increment > 1:
            zoom_value = math.ceil(self._canvas_widgets.zoom_widget.value() * increment)
        else:
            zoom_value = math.floor(
                self._canvas_widgets.zoom_widget.value() * increment
            )
        self._zoom_mode = _ZoomMode.MANUAL_ZOOM
        self._set_zoom(value=zoom_value, pos=pos)

    def _zoom_requested(self, delta: int, pos: QtCore.QPointF) -> None:
        self._add_zoom(increment=1.1 if delta > 0 else 0.9, pos=pos)

    def set_fit_window_mode(self, value: bool = True) -> None:
        target = _ZoomMode.FIT_WINDOW if value else _ZoomMode.MANUAL_ZOOM
        self._switch_zoom_mode(target)

    def set_fit_width_mode(self, value: bool = True) -> None:
        target = _ZoomMode.FIT_WIDTH if value else _ZoomMode.MANUAL_ZOOM
        self._switch_zoom_mode(target)

    def _switch_zoom_mode(self, mode: _ZoomMode) -> None:
        self._zoom_mode = mode
        self._adjust_scale()

    def _sync_zoom_mode_actions(self) -> None:
        self._actions.fit_window.setChecked(self._zoom_mode == _ZoomMode.FIT_WINDOW)
        self._actions.fit_width.setChecked(self._zoom_mode == _ZoomMode.FIT_WIDTH)

    def _on_brightness_contrast_changed(self, qimage: QtGui.QImage) -> None:
        self._canvas_widgets.canvas.load_pixmap(
            QtGui.QPixmap.fromImage(qimage), clear_shapes=False
        )

    def open_brightness_contrast_dialog(
        self, value: bool, is_initial_load: bool = False
    ) -> None:
        if self._image_path is None:
            logger.warning("image_path is None, cannot set brightness/contrast")
            return

        brightness: int | None
        contrast: int | None
        brightness, contrast = self._brightness_contrast_values.get(
            self._image_path, (None, None)
        )
        if is_initial_load:
            if self._restore_tone_on_first_image:
                self._restore_tone_on_first_image = False
                brightness, contrast = self._last_tone
            elif self._config["keep_prev_brightness_contrast"] and self._prev_image_path:
                brightness, contrast = self._brightness_contrast_values.get(
                    self._prev_image_path, (brightness, contrast)
                )
            if brightness is None and contrast is None:
                return

        if brightness is None:
            brightness = NEUTRAL_TONE
        if contrast is None:
            contrast = NEUTRAL_TONE
        assert self._image_data is not None
        if is_initial_load:
            if (brightness, contrast) != (NEUTRAL_TONE, NEUTRAL_TONE):
                self._on_brightness_contrast_changed(
                    adjusted_image(utils.img_data_to_pil(self._image_data), brightness, contrast)
                )
        else:
            dialog = BrightnessContrastDialog(
                utils.img_data_to_pil(self._image_data),
                self._on_brightness_contrast_changed,
                parent=self,
                brightness=brightness,
                contrast=contrast,
            )
            try:
                dialog.exec_()
                brightness = dialog.slider_brightness.value()
                contrast = dialog.slider_contrast.value()
            finally:
                dialog.deleteLater()

        self._brightness_contrast_values[self._image_path] = (brightness, contrast)
        self._last_tone = (brightness, contrast)
        logger.debug(
            "Updated states for {}: brightness={}, contrast={}",
            self._image_path,
            brightness,
            contrast,
        )

    def toggle_shape_visibility(self, value: bool | None) -> None:
        if value is not None:
            self._show_annotations_by_default = value
            self._annotation_visibility.clear()
            self._label_visibility.clear()
        self._set_annotation_checks(
            (item, item.checkState() == Qt.Unchecked if value is None else value)
            for item in self._docks.label_list
        )

    def _open_label_file_into_state(self, label_path: str) -> LabelData | None:
        try:
            label_data = read_label_file(filename=label_path)
        except LabelFileError as e:
            self._show_file_open_error(path=label_path, file_kind="label", exc=e)
            return None
        self._label_file_path = label_path
        self._image_data = label_data.image_data
        self._image_path = os.path.normpath(Path(label_path).parent / label_data.image_path)
        self._other_data = label_data.other_data
        return label_data

    def _open_image_into_state(self, image_path: str) -> bool:
        try:
            image_data = read_image_file(filename=image_path)
        except OSError as e:
            self._show_file_open_error(path=image_path, file_kind="image", exc=e)
            return False
        self._image_data = image_data
        self._image_path = image_path
        self._label_file_path = None
        return True

    def _load_file(
        self, image_or_label_path: str, *, keep_previous_shapes: bool = True
    ) -> None:
        # changing fileListWidget loads file
        if image_or_label_path in self.image_list and (
            self._docks.file_list.currentRow()
            != self.image_list.index(image_or_label_path)
        ):
            self._docks.file_list.setCurrentRow(
                self.image_list.index(image_or_label_path)
            )
            self._docks.file_list.repaint()
            return

        canvas = self._canvas_widgets.canvas
        selected_rectangles = self._selected_rectangle_keys
        self._selected_rectangle_keys = set()

        prev_shapes: list[Shape] = (
            self._canvas_widgets.canvas.shapes
            if keep_previous_shapes and (
                self._config["keep_prev"]
                or QtWidgets.QApplication.keyboardModifiers()
                == (Qt.ControlModifier | Qt.ShiftModifier)
            )
            else []
        )
        previous_scroll = (
            {orientation: bar.value() for orientation, bar in self._canvas_widgets.scroll_bars.items()}
            if self._has_image_zoom and self._config["keep_prev_scale"]
            else None
        )
        self._prev_image_path = self._image_path
        self.reset_state()
        self._canvas_widgets.canvas.setEnabled(False)
        if not QtCore.QFile.exists(image_or_label_path):
            self.show_error_message(
                self.tr("Error opening file"),
                self.tr("No such file: <b>%s</b>") % image_or_label_path,
            )
            return
        # assumes same name, but json extension
        self.show_status_message(
            self.tr("Loading %s...") % Path(image_or_label_path).name
        )

        t0_load_file = time.time()
        label_path: str = _resolve_label_path(
            image_or_label_path=image_or_label_path,
            output_dir=self._output_dir,
            existing=True,
        )
        label_data: LabelData | None = None
        if QtCore.QFile.exists(label_path):
            label_data = self._open_label_file_into_state(label_path=label_path)
            if label_data is None:
                return
        else:
            if not self._open_image_into_state(image_path=image_or_label_path):
                return
        assert self._image_data is not None
        t0 = time.time()
        image = QtGui.QImage.fromData(self._image_data)
        logger.debug("Created QImage in {:.0f}ms", (time.time() - t0) * 1000)

        if image.isNull():
            formats = ", ".join(
                f"*.{fmt.data().decode()}"
                for fmt in QtGui.QImageReader.supportedImageFormats()
            )
            extra = self.tr("Allowed formats: {formats}").format(formats=formats)
            self._show_file_open_error(
                path=image_or_label_path,
                file_kind="image",
                extra=extra,
            )
            return
        self._image = image
        t0 = time.time()
        self._canvas_widgets.canvas.load_pixmap(QtGui.QPixmap.fromImage(image))
        logger.debug("Loaded pixmap in {:.0f}ms", (time.time() - t0) * 1000)
        flags = {k: False for k in self._config["flags"] or []}
        if label_data is not None:
            shapes = _shapes_from_dicts(
                shape_dicts=label_data.shapes,
                label_flags=self._config["label_flags"],
            )
            for shape in shapes:
                visible = self._show_annotations_by_default
                if shape.label is not None:
                    visible = self._annotation_visibility.get(
                        (shape.label, shape.group_id), self._label_visibility.get(shape.label, visible)
                    )
                shape.visible = visible
            self._load_shapes(shapes=shapes)
            flags.update(label_data.flags)
        self._load_flags(flags=flags, widget=self._docks.flag_list)
        if prev_shapes and self.has_no_shapes():
            self._load_shapes(shapes=prev_shapes, replace=False)
            self.mark_dirty()
        else:
            self.mark_clean()
        self._canvas_widgets.canvas.setEnabled(True)
        # set zoom values
        if not self._has_image_zoom or not self._config["keep_prev_scale"]:
            self._zoom_mode = _ZoomMode.FIT_WINDOW
            self._adjust_scale()
        else:
            self._zoom_mode = _ZoomMode.MANUAL_ZOOM
            self._paint_canvas()
        self.open_brightness_contrast_dialog(value=False, is_initial_load=True)
        self._paint_canvas()
        self.update_action_states(True)
        if (
            selected_rectangles
            and self._prev_image_path
            and self._image_path
            and Path(self._prev_image_path).parent == Path(self._image_path).parent
        ):
            self._selected_rectangle_keys = selected_rectangles
            matches = [
                shape for shape in canvas.shapes
                if shape.shape_type == "rectangle"
                and (shape.label, shape.group_id) in selected_rectangles
            ]
            counts = Counter((shape.label, shape.group_id) for shape in matches)
            self._on_shape_selection_changed(
                [
                    shape for shape in matches
                    if shape.visible and counts[(shape.label, shape.group_id)] == 1
                ],
                remember=False,
            )
            canvas.update()
        self._canvas_widgets.canvas.setFocus()
        if previous_scroll is not None:
            for orientation, value in previous_scroll.items():
                self.set_scroll_value(orientation, value)
        else:
            for orientation, values in self._scroll_values.items():
                if self._image_path in values:
                    self.set_scroll_value(orientation, values[self._image_path])
        self._refresh_folder_label_list()
        self.show_status_message(self.tr("Loaded %s") % Path(image_or_label_path).name)
        logger.info(
            "Loaded file: {!r} in {:.0f}ms",
            image_or_label_path,
            (time.time() - t0_load_file) * 1000,
        )

    def resizeEvent(self, a0: QtGui.QResizeEvent) -> None:
        if (
            self._canvas_widgets.canvas
            and not self._image.isNull()
            and self._zoom_mode != _ZoomMode.MANUAL_ZOOM
        ):
            self._adjust_scale()
        super().resizeEvent(a0)

    def _paint_canvas(self) -> None:
        if self._image.isNull():
            logger.warning("image is null, cannot paint canvas")
            return
        self._canvas_widgets.canvas.scale = (
            0.01 * self._canvas_widgets.zoom_widget.value()
        )
        self._canvas_widgets.canvas.adjustSize()
        self._canvas_widgets.canvas.update()

    def _adjust_scale(self) -> None:
        if self._zoom_mode == _ZoomMode.FIT_WINDOW:
            scale = self._fit_window_scale()
        elif self._zoom_mode == _ZoomMode.FIT_WIDTH:
            scale = self._fit_width_scale()
        else:
            scale = 1.0
        self._set_zoom(value=int(scale * 100))

    def _fit_window_scale(self) -> float:
        FIT_WINDOW_SCROLLBAR_MARGIN: Final[float] = 2.0
        viewport = self.centralWidget()
        pixmap = self._canvas_widgets.canvas.pixmap
        available_w = viewport.width() - FIT_WINDOW_SCROLLBAR_MARGIN
        available_h = viewport.height() - FIT_WINDOW_SCROLLBAR_MARGIN
        scale_by_width = available_w / pixmap.width()
        scale_by_height = available_h / pixmap.height()
        return min(scale_by_width, scale_by_height)

    def _fit_width_scale(self) -> float:
        FIT_WIDTH_SCROLLBAR_MARGIN: Final[float] = 15.0
        available_w = self.centralWidget().width() - FIT_WIDTH_SCROLLBAR_MARGIN
        return available_w / self._canvas_widgets.canvas.pixmap.width()

    def set_save_image_with_data(self, enabled: bool) -> None:
        self._config["with_image_data"] = enabled
        self._actions.save_with_image_data.setChecked(enabled)

    def _reset_layout(self) -> None:
        self.settings.remove("window/state")
        self.restoreState(self._default_state)

    def _remembered_menu_toggles(self) -> tuple[tuple[str, QtWidgets.QAction], ...]:
        return (
            ("controls/keepZoom", self._actions.keep_prev_zoom),
            ("controls/keepTone", self._actions.toggle_keep_prev_brightness_contrast),
        )

    def _persistent_view_toggles(self) -> tuple[tuple[str, QtWidgets.QPushButton], ...]:
        return (
            ("controls/showPath", self._file_path_toggle),
            ("controls/rectangleEdit", self._rectangle_edit_toggle),
            ("controls/groupColors", self._group_color_toggle),
            ("controls/showCaptions", self._caption_toggle),
            ("controls/detectDuplicates", self._duplicate_toggle),
            ("controls/autoFill", self._auto_fill_toggle),
            ("controls/syncFrames", self._frame_sync_toggle),
            ("controls/boxHighlight", self._highlight_toggle),
            ("controls/instantDelete", self._instant_delete_toggle),
            ("controls/showPixels", self._pixel_display_toggle),
        )

    def _restore_annotation_visibility(self, directory: str) -> None:
        register = self.settings.value("controls/annotationVisibility", "", type=str)
        if not register:
            return
        try:
            saved = json.loads(register)
        except ValueError:
            return
        if not isinstance(saved, dict) or saved.get("directory") != directory:
            return
        entries = saved.get("items")
        if not isinstance(entries, list):
            return
        self._prev_opened_dir = directory
        labels = saved.get("labels", {})
        if isinstance(labels, dict):
            self._label_visibility = {label: visible for label, visible in labels.items() if isinstance(label, str) and type(visible) is bool}
        for entry in entries:
            if not isinstance(entry, list) or len(entry) != 3:
                continue
            label, group_id, visible = entry
            if isinstance(label, str) and (group_id is None or type(group_id) is int) and type(visible) is bool:
                self._annotation_visibility[(label, group_id)] = visible

    def closeEvent(self, a0: QtGui.QCloseEvent) -> None:
        if not self._can_continue():
            a0.ignore()
            return
        if self._image_path:
            last_image = os.path.abspath(self._image_path)
            self.settings.setValue("files/lastImage", last_image)
            if not self._prev_opened_dir or not Path(last_image).is_relative_to(
                Path(self._prev_opened_dir)
            ):
                self.settings.setValue("files/lastDirectory", str(Path(last_image).parent))
        else:
            self.settings.remove("files/lastImage")
        for key, action in self._remembered_menu_toggles():
            self.settings.setValue(key, action.isChecked())
        self.settings.setValue("controls/frameCopyCount", self._frame_copy_count)
        self.settings.setValue("controls/frameSyncCount", self._frame_sync_count.value())
        self.settings.setValue("controls/frameSyncMethod", self._frame_sync_method.currentData())
        self.settings.setValue("controls/highlightTransparency", self._highlight_transparency.value())
        if self._last_tone is not None:
            brightness, contrast = self._last_tone
            self.settings.setValue("view/brightness", brightness)
            self.settings.setValue("view/contrast", contrast)
        for key, control in self._persistent_view_toggles():
            self.settings.setValue(key, control.isChecked())
        self.settings.setValue("controls/showAllAnnotations", self._show_annotations_by_default)
        self.settings.setValue(
            "controls/annotationVisibility",
            json.dumps({
                "directory": self._prev_opened_dir,
                "labels": self._label_visibility,
                "items": [(label, group_id, visible)
                          for (label, group_id), visible in self._annotation_visibility.items()],
            }),
        )
        self.settings.setValue("window/geometry", self.saveGeometry())
        self.settings.setValue("window/state", self.saveState())

    def dragEnterEvent(self, a0: QtGui.QDragEnterEvent) -> None:
        extensions = [
            f".{fmt.data().decode().lower()}"
            for fmt in QtGui.QImageReader.supportedImageFormats()
        ]
        if a0.mimeData().hasUrls():
            items = [i.toLocalFile() for i in a0.mimeData().urls()]
            if any([i.lower().endswith(tuple(extensions)) for i in items]):
                a0.accept()
        else:
            a0.ignore()

    def dropEvent(self, a0: QtGui.QDropEvent) -> None:
        if not self._can_continue():
            a0.ignore()
            return
        items = [i.toLocalFile() for i in a0.mimeData().urls()]
        self.import_dropped_image_files(items)

    # User Dialogs #

    def _open_prev_image(self, _value: bool = False) -> None:
        row_prev: int = self._docks.file_list.currentRow() - 1
        if row_prev < 0:
            logger.debug("there is no prev image")
            return

        logger.debug("setting current row to {:d}", row_prev)
        self._docks.file_list.setCurrentRow(row_prev)
        self._docks.file_list.repaint()

    def _open_next_image(self, _value: bool = False) -> None:
        row_next: int = self._docks.file_list.currentRow() + 1
        if row_next >= self._docks.file_list.count():
            logger.debug("there is no next image")
            return

        logger.debug("setting current row to {:d}", row_next)
        self._docks.file_list.setCurrentRow(row_next)
        self._docks.file_list.repaint()

    def _open_file_with_dialog(self, _value: bool = False) -> None:
        if not self._can_continue():
            return
        formats = [
            f"*.{fmt.data().decode()}"
            for fmt in QtGui.QImageReader.supportedImageFormats()
        ]
        filters = self.tr("Image & Label files (%s)") % " ".join(
            formats + [f"*{LABEL_FILE_SUFFIX}"]
        )
        image_or_label_path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            self.tr("%s - Choose Image or Label file") % __appname__,
            self.current_path(),
            filters,
        )
        if image_or_label_path:
            self._load_from_file_or_dir(file_or_dir=image_or_label_path)

    def prompt_output_dir(self, _value: bool = False) -> None:
        default_output_dir: str
        if self._output_dir is not None:
            default_output_dir = str(self._output_dir)
        elif self._image_path:
            default_output_dir = str(_default_output_dir(Path(self._image_path).parent))
        else:
            default_output_dir = self.current_path()

        output_dir = QtWidgets.QFileDialog.getExistingDirectory(
            self,
            self.tr("%s - Save/Load Annotations in Directory") % __appname__,
            default_output_dir,
            QtWidgets.QFileDialog.ShowDirsOnly
            | QtWidgets.QFileDialog.DontResolveSymlinks,
        )
        output_dir = str(output_dir)

        if not output_dir or not self._can_continue():
            return

        self._output_dir = Path(output_dir)

        self.statusBar().showMessage(
            self.tr("%s . Annotations will be saved/loaded in %s")
            % ("Change Annotations Dir", self._output_dir)
        )
        self.statusBar().show()

        if self._prev_opened_dir:
            self._on_file_search_changed()
        if self._image_path:
            self._load_file(self._image_path, keep_previous_shapes=False)
        else:
            self._refresh_folder_label_list()

    def _save_label_file(self, *, save_as: bool = False) -> None:
        assert not self._image.isNull(), "cannot save empty image"

        label_path: str | None = None
        if not save_as and self._label_file_path is not None:
            label_path = self._label_file_path
        if label_path is None:
            label_path = self.prompt_save_file_path()

        if not label_path:
            logger.warning("label_path=%r is empty, so cannot save", label_path)
            return

        if self.save_labels(label_path=label_path):
            self.mark_clean()

    def prompt_save_file_path(self) -> str:
        assert self._image_path is not None
        caption = self.tr("%s - Choose File") % __appname__
        filters = self.tr("Label files (*%s)") % LABEL_FILE_SUFFIX
        default_path = _resolve_label_path(
            image_or_label_path=self._image_path, output_dir=self._output_dir
        )
        try:
            Path(default_path).parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self.show_error_message(self.tr("Error saving label data"), str(e))
            return ""
        dlg = QtWidgets.QFileDialog(
            parent=self,
            caption=caption,
            directory=str(Path(default_path).parent),
            filter=filters,
        )
        dlg.setDefaultSuffix(LABEL_FILE_SUFFIX[1:])
        dlg.setAcceptMode(QtWidgets.QFileDialog.AcceptSave)
        dlg.setOption(QtWidgets.QFileDialog.DontConfirmOverwrite, False)
        dlg.setOption(QtWidgets.QFileDialog.DontUseNativeDialog, False)
        label_path, _ = dlg.getSaveFileName(
            parent=self,
            caption=self.tr("Choose File"),
            directory=default_path,
            filter=self.tr("Label files (*%s)") % LABEL_FILE_SUFFIX,
        )
        return label_path

    def close_file(self, _value: bool = False) -> None:
        if not self._can_continue():
            return
        self.reset_state()
        self.mark_clean()
        self.update_action_states(False)
        self._canvas_widgets.canvas.setEnabled(False)
        self._docks.file_list.setFocus()
        self._actions.save_as.setEnabled(False)
        self._docks.unique_label_list.clear()

    def current_label_file_path(self) -> str:
        assert self._image_path is not None
        return self._label_file_path or _resolve_label_path(
            image_or_label_path=self._image_path,
            output_dir=self._output_dir,
            existing=True,
        )

    def delete_file(self) -> None:
        msg = self.tr(
            "Permanently delete this label file? This action cannot be undone."
        )
        answer = QtWidgets.QMessageBox.warning(
            self,
            self.tr("Attention"),
            msg,
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
        )
        if answer != QtWidgets.QMessageBox.Yes:
            return

        annotation_path = Path(self.current_label_file_path())
        if not annotation_path.exists():
            return

        annotation_path.unlink()
        self._folder_labels.mark_changed(annotation_path)
        logger.info(f"Label file is removed: {annotation_path}")

        item = self._docks.file_list.currentItem()
        if item:
            item.setCheckState(Qt.Unchecked)

        self.reset_state()

    def _open_config_file(self) -> None:
        if self._config_file is None:
            QtWidgets.QMessageBox.information(
                self,
                self.tr("No Config File"),
                self.tr(
                    "Configuration was provided as a YAML expression via "
                    "command line.\n\n"
                    "To use the preferences editor, start Labelme with a config file:\n"
                    "  labelme --config ~/.labelmerc"
                ),
            )
            return
        config_file: Path = self._config_file

        system: str = platform.system()
        if system == "Darwin":
            subprocess.Popen(["open", "-t", config_file])
        elif system == "Windows":
            os.startfile(config_file)  # ty: ignore[unresolved-attribute]  # Windows-only
        else:
            subprocess.Popen(["xdg-open", config_file])

    # Message Dialogs. #
    def has_labels(self) -> bool:
        if self.has_no_shapes():
            self.show_error_message(
                "No objects labeled",
                "You must label at least one object to save the file.",
            )
            return False
        return True

    def has_label_file(self) -> bool:
        if self._image_path is None:
            return False

        label_file = self.current_label_file_path()
        return Path(label_file).exists()

    def _can_continue(self) -> bool:
        if not self._is_changed:
            return True
        prompt_text = self.tr('Save annotations to "{}" before closing?').format(
            self._image_path
        )
        user_choice = QtWidgets.QMessageBox.question(
            self,
            self.tr("Save annotations?"),
            prompt_text,
            QtWidgets.QMessageBox.Save
            | QtWidgets.QMessageBox.Discard
            | QtWidgets.QMessageBox.Cancel,
            QtWidgets.QMessageBox.Save,
        )
        if user_choice == QtWidgets.QMessageBox.Save:
            self._save_label_file()
            return not self._is_changed
        return user_choice == QtWidgets.QMessageBox.Discard

    def show_error_message(self, title: str, message: str) -> int:
        return QtWidgets.QMessageBox.critical(
            self, title, f"<p><b>{title}</b></p>{message}"
        )

    def _show_file_open_error(
        self,
        *,
        path: str,
        file_kind: Literal["label", "image"],
        exc: BaseException | None = None,
        extra: str | None = None,
    ) -> None:
        if file_kind == "label":
            message = self.tr(
                "The selected label file could not be opened: {path}"
            ).format(path=path)
        else:
            message = self.tr(
                "The selected image file could not be opened: {path}"
            ).format(path=path)
        if exc is not None:
            message = f"{message}\n\n{exc}"
        if extra:
            message = f"{message}\n\n{extra}"
        QtWidgets.QMessageBox.critical(self, self.tr("Error opening file"), message)
        self.show_status_message(self.tr("Failed to load: {path}").format(path=path))

    def current_path(self) -> str:
        return str(Path(self._image_path).parent) if self._image_path else "."

    def remove_selected_point(self) -> None:
        self._canvas_widgets.canvas.remove_selected_point()
        self._canvas_widgets.canvas.update()
        if (
            self._canvas_widgets.canvas.hovered_shape
            and not self._canvas_widgets.canvas.hovered_shape.points
        ):
            self._canvas_widgets.canvas.delete_shape(
                self._canvas_widgets.canvas.hovered_shape
            )
            self.remove_labels([self._canvas_widgets.canvas.hovered_shape])
            if self.has_no_shapes():
                for action in self._actions.on_shapes_present:
                    action.setEnabled(False)
        self.mark_dirty()

    def delete_selected_shapes(self) -> None:
        canvas = self._canvas_widgets.canvas
        if not canvas.selected_shapes:
            return
        if not self._instant_delete_toggle.isChecked():
            yes, no = QtWidgets.QMessageBox.Yes, QtWidgets.QMessageBox.No
            msg = self.tr(
                "Permanently delete {} shapes? This action cannot be undone."
            ).format(len(canvas.selected_shapes))
            if QtWidgets.QMessageBox.warning(
                self, self.tr("Attention"), msg, yes | no, yes
            ) != yes:
                return
        with QtCore.QSignalBlocker(self._docks.label_list):
            self.remove_labels(canvas.delete_selected())
        self._on_shape_selection_changed([], remember=False)
        self.mark_dirty()
        if self.has_no_shapes():
            for action in self._actions.on_shapes_present:
                action.setEnabled(False)

    def copy_shape(self) -> None:
        self._canvas_widgets.canvas.end_move(copy=True)
        for shape in self._canvas_widgets.canvas.selected_shapes:
            self.add_label(shape)
        self._docks.label_list.clearSelection()
        self.mark_dirty()

    def move_shape(self) -> None:
        self._canvas_widgets.canvas.end_move(copy=False)
        self._on_canvas_shape_moved()

    def _load_from_file_or_dir(
        self, file_or_dir: str, *, preferred_image: str | None = None
    ) -> None:
        if not file_or_dir:
            raise ValueError("file_or_dir cannot be empty")

        if is_label_file_path(filename=file_or_dir):
            self._docks.file_list.clear()
            self._docks.file_dock.setEnabled(False)
            self._docks.file_dock.setToolTip(
                self.tr("File list is disabled when a label file is opened")
            )
            self._load_file(image_or_label_path=file_or_dir)
        elif Path(file_or_dir).is_dir():
            self._import_images_from_dir(
                root_dir=file_or_dir, pattern=self._docks.file_search.text()
            )
            if preferred_image and preferred_image in self.image_list:
                self._load_file(image_or_label_path=preferred_image)
            else:
                self._open_next_image()
            if not self._image_path:
                self._refresh_folder_label_list()
        else:
            self._import_images_from_dir(
                root_dir=str(Path(file_or_dir).parent),
                pattern=self._docks.file_search.text(),
            )
            self._load_file(image_or_label_path=file_or_dir)

    def _open_dir_with_dialog(self, _value: bool = False) -> None:
        if not self._can_continue():
            return

        default_open_dir_path: str
        if self._prev_opened_dir and Path(self._prev_opened_dir).exists():
            default_open_dir_path = self._prev_opened_dir
        else:
            default_open_dir_path = (
                str(Path(self._image_path).parent) if self._image_path else "."
            )

        dir_path = str(
            QtWidgets.QFileDialog.getExistingDirectory(
                self,
                self.tr("%s - Open Directory") % __appname__,
                default_open_dir_path,
                QtWidgets.QFileDialog.ShowDirsOnly
                | QtWidgets.QFileDialog.DontResolveSymlinks,
            )
        )
        if dir_path:
            self._load_from_file_or_dir(file_or_dir=dir_path)

    @property
    def image_list(self) -> list[str]:
        lst = []
        for i in range(self._docks.file_list.count()):
            item = self._docks.file_list.item(i)
            assert item
            lst.append(item.text())
        return lst

    def import_dropped_image_files(self, image_files: list[str]) -> None:
        extensions = tuple(
            f".{fmt.data().decode().lower()}"
            for fmt in QtGui.QImageReader.supportedImageFormats()
        )
        already_loaded = set(self.image_list)
        new_files = [
            path
            for path in image_files
            if path not in already_loaded and path.lower().endswith(extensions)
        ]

        self._image_path = None
        for path in new_files:
            self._docks.file_list.addItem(
                _make_image_list_item(image_path=path, output_dir=self._output_dir)
            )

        if len(self.image_list) > 1:
            self._actions.open_next_img.setEnabled(True)
            self._actions.open_prev_img.setEnabled(True)

        self._open_next_image()

    def _import_images_from_dir(
        self, root_dir: str | None, pattern: str | None = None
    ) -> None:
        self._actions.open_next_img.setEnabled(True)
        self._actions.open_prev_img.setEnabled(True)

        if not self._can_continue() or not root_dir:
            return

        self._docks.file_dock.setEnabled(True)
        self._docks.file_dock.setToolTip("")

        root_dir = os.path.abspath(root_dir)
        if self._prev_opened_dir != root_dir:
            self._annotation_visibility.clear()
            self._label_visibility.clear()
        self._prev_opened_dir = root_dir
        self.settings.setValue("files/lastDirectory", root_dir)
        self._image_path = None
        self._populate_file_list(root_dir, pattern)

    def _populate_file_list(self, root_dir: str, pattern: str | None) -> None:
        files = self._docks.file_list
        files.clear()
        image_paths = _scan_image_files(root_dir=root_dir)
        if pattern:
            try:
                image_paths = [x for x in image_paths if re.search(pattern, Path(x).name)]
            except re.error:
                pass
        for image_path in image_paths:
            files.addItem(_make_image_list_item(image_path=image_path, output_dir=self._output_dir))

    def _update_status_stats(self, mouse_pos: QtCore.QPointF) -> None:
        stats: list[str] = []
        stats.append(f"mode={self._canvas_widgets.canvas.mode.name}")
        stats.append(f"x={mouse_pos.x():6.1f}, y={mouse_pos.y():6.1f}")
        self._status_bar.stats.setText(" | ".join(stats))


def _shapes_from_dicts(
    *,
    shape_dicts: list[ShapeDict],
    label_flags: dict[str, list[str]] | None,
) -> list[Shape]:
    shapes: list[Shape] = []
    for shape_dict in shape_dicts:
        shape = Shape(
            label=shape_dict["label"],
            shape_type=shape_dict["shape_type"],
            group_id=shape_dict["group_id"],
            description=shape_dict["description"],
            mask=shape_dict["mask"],
        )
        for x, y in shape_dict["points"]:
            shape.add_point(QtCore.QPointF(x, y))
        shape.close()

        default_flags: dict[str, bool] = {}
        if label_flags:
            for pattern, keys in label_flags.items():
                if not isinstance(shape.label, str):
                    logger.warning("shape.label is not str: {}", shape.label)
                    continue
                if re.match(pattern, shape.label):
                    for key in keys:
                        default_flags[key] = False
        shape.flags = default_flags
        shape.flags.update(shape_dict["flags"])
        shape.other_data = shape_dict["other_data"]

        shapes.append(shape)
    return shapes


def _resolve_text_annotation_shape_type(
    *, create_mode: str, ai_output_format: _automation.AiOutputFormat
) -> _automation.AiOutputFormat | None:
    if create_mode in _AI_CREATE_MODES:
        return ai_output_format
    if create_mode in get_args(_TextToAnnotationCreateMode):
        return cast(_TextToAnnotationCreateMode, create_mode)
    return None


def _rgb_from_colormap_id(*, label_id: int) -> tuple[int, int, int]:
    r, g, b = LABEL_COLORMAP[label_id % len(LABEL_COLORMAP)].tolist()
    return r, g, b


def _rgb_from_label_colors(
    *, label: str, label_colors: dict[str, list[int]] | None
) -> tuple[int, int, int] | None:
    if not label_colors or label not in label_colors:
        return None
    rgb = label_colors[label]
    if len(rgb) != 3 or not all(0 <= c <= 255 for c in rgb):
        raise ValueError(f"Color for label must be 0-255 RGB tuple, but got: {rgb}")
    r, g, b = rgb
    return r, g, b


def _is_valid_label(
    *, label: str, existing_labels: list[str], policy: str | None
) -> bool:
    if policy is None:
        return True
    if policy == "exact":
        return label in existing_labels
    return False


def _format_window_title(
    *,
    image_path: str | None,
    file_index: int | None,
    file_count: int,
    dirty: bool,
) -> str:
    title = __appname__
    if image_path:
        title = f"{title} - {image_path}"
        if file_count and file_index is not None:
            title = f"{title} [{file_index + 1}/{file_count}]"
    if dirty:
        title = f"{title}*"
    return title


def _default_output_dir(image_dir: Path) -> Path:
    image_dir = Path(os.path.abspath(image_dir))
    return image_dir / f"{image_dir.name}_json"


def _resolve_label_path(
    *, image_or_label_path: str, output_dir: Path | None, existing: bool = False
) -> str:
    if is_label_file_path(filename=image_or_label_path):
        return image_or_label_path
    image_path = Path(os.path.abspath(image_or_label_path))
    image_dir = image_path.parent
    label_name = f"{image_path.stem}{LABEL_FILE_SUFFIX}"
    if output_dir is not None:
        return str(output_dir / label_name)
    default_path = _default_output_dir(image_dir) / label_name
    if existing and not default_path.is_file():
        for legacy_path in (image_dir / image_dir.name / label_name, image_dir / label_name):
            if legacy_path.is_file():
                return str(legacy_path)
    return str(default_path)


def _make_image_list_item(
    *, image_path: str, output_dir: Path | None
) -> QtWidgets.QListWidgetItem:
    item = QtWidgets.QListWidgetItem(image_path)
    item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
    label_path = _resolve_label_path(
        image_or_label_path=image_path, output_dir=output_dir, existing=True
    )
    has_label = QtCore.QFile.exists(label_path)
    item.setCheckState(Qt.Checked if has_label else Qt.Unchecked)
    return item


def _shape_to_dict(shape: Shape) -> dict[str, Any]:
    data = shape.other_data.copy()
    data.update(
        label=shape.label,
        points=[(p.x(), p.y()) for p in shape.points],
        group_id=shape.group_id,
        description=shape.description,
        shape_type=shape.shape_type,
        flags=shape.flags,
        mask=None
        if shape.mask is None
        else utils.img_arr_to_b64(shape.mask.astype(np.uint8)),
    )
    return data


def _scan_image_files(root_dir: str) -> list[str]:
    extensions: list[str] = [
        f".{fmt.data().decode().lower()}"
        for fmt in QtGui.QImageReader.supportedImageFormats()
    ]

    images: list[str] = []
    for root, dirs, files in os.walk(root_dir):
        for file in files:
            if file.lower().endswith(tuple(extensions)):
                relative_path = os.path.normpath(os.path.join(root, file))
                images.append(relative_path)

    logger.debug("found {:d} images in {!r}", len(images), root_dir)
    try:
        return natsort.os_sorted(images)
    except OSError:
        logger.warning(
            "natsort.os_sorted failed (known macOS strxfrm bug), "
            "falling back to locale-unaware natural sort"
        )
        return natsort.natsorted(images)
