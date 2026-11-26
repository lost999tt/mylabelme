from __future__ import annotations

import json

from PyQt5 import QtCore
from PyQt5 import QtGui
from PyQt5 import QtWidgets
from PyQt5.QtCore import Qt

from .label_list_widget import HTMLDelegate
from .label_list_widget import format_label_with_color_dot


LABEL_RECTANGLE_MIME = "application/x-mylabelme-rectangle"

_GROUPS_ROLE = Qt.UserRole + 1
_GROUP_KEY_ROLE = Qt.UserRole + 2
_IMAGE_COUNT_ROLE = Qt.UserRole + 3


class _EscapableQListWidget(QtWidgets.QListWidget):
    def keyPressEvent(self, keyEvent: QtGui.QKeyEvent) -> None:  # ty: ignore[invalid-method-override]
        super().keyPressEvent(keyEvent)
        if keyEvent.key() == Qt.Key_Escape:
            self.clearSelection()


def _visibility_icon_rect(rect, metrics, is_group: bool) -> QtCore.QRect:
    indent = metrics.horizontalAdvance("\u25be n") if is_group else 0
    return QtCore.QRect(rect.left() + 4 + indent, rect.center().y() - 7, 18, 14)


class _GroupListDelegate(HTMLDelegate):
    def initStyleOption(self, option, index) -> None:
        super().initStyleOption(option, index)
        option.features &= ~QtWidgets.QStyleOptionViewItem.HasCheckIndicator

    def paint(self, painter, option, index) -> None:
        group = index.data(_GROUP_KEY_ROLE)
        parent = index.data(_GROUPS_ROLE)
        if group is not None:
            self._paint_group(painter, option, index, group)
            return
        text_option = QtWidgets.QStyleOptionViewItem(option)
        text_option.rect.adjust(24, 0, 0, 0)
        super().paint(painter, text_option, index)
        self._paint_eye(painter, option, index, False)
        if parent is not None:
            painter.save()
            color = option.palette.highlightedText() if option.state & QtWidgets.QStyle.State_Selected else option.palette.text()
            painter.setPen(color.color())
            painter.drawText(option.rect.adjusted(0, 0, -16, 0), Qt.AlignRight | Qt.AlignVCenter,
                             str(sum(group_id is not None for group_id in parent[0])))
            painter.restore()

    def _paint_group(self, painter, option, index, group) -> None:
        opt = QtWidgets.QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        text = opt.text
        opt.text = ""
        style = opt.widget.style()
        style.drawControl(QtWidgets.QStyle.CE_ItemViewItem, opt, painter, opt.widget)
        text_rect = style.subElementRect(QtWidgets.QStyle.SE_ItemViewItemText, opt, opt.widget)
        metrics = opt.fontMetrics
        x = _visibility_icon_rect(opt.rect, metrics, True).right() + 5
        baseline = opt.rect.center().y() + (metrics.ascent() - metrics.descent()) // 2
        painter.save()
        painter.setFont(opt.font)
        painter.setPen(opt.palette.text().color())
        painter.drawText(x, baseline, text)
        if self.parent().group_color is not None:
            color = QtGui.QColor(*self.parent().group_color(*group))
            rect = QtCore.QRect(x + metrics.horizontalAdvance(text) + 5, opt.rect.center().y() - 5, 10, 10)
            painter.fillRect(rect, color)
            painter.setPen(opt.palette.mid().color())
            painter.drawRect(rect)
        count = index.data(_IMAGE_COUNT_ROLE)
        if count is not None:
            painter.setPen(opt.palette.text().color())
            painter.drawText(
                opt.rect.adjusted(0, 0, -16, 0), Qt.AlignRight | Qt.AlignVCenter, str(count)
            )
        painter.restore()
        self._paint_eye(painter, option, index, True)

    def _paint_eye(self, painter, option, index, is_group: bool) -> None:
        rect = _visibility_icon_rect(option.rect, option.fontMetrics, is_group)
        state = index.data(Qt.CheckStateRole)
        selected = bool(option.state & QtWidgets.QStyle.State_Selected)
        color = option.palette.highlightedText().color() if selected else option.palette.text().color()
        painter.save()
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        x, y = rect.left() + 3, rect.center().y()
        if state == Qt.Unchecked:
            # A subtle inset slot, matching Photoshop's hidden-layer indicator.
            groove = QtCore.QRectF(x + 1, y - 4.5, 9, 9)
            shadow = QtGui.QColor(color)
            shadow.setAlpha(35)
            light = QtGui.QColor(option.palette.base().color())
            light.setAlpha(180)
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QtGui.QPen(shadow, 0.8))
            painter.drawLine(groove.topLeft(), groove.bottomLeft())
            painter.drawLine(groove.bottomLeft(), groove.bottomRight())
            painter.setPen(QtGui.QPen(light, 0.8))
            painter.drawLine(groove.topRight(), groove.bottomRight())
            painter.drawLine(groove.topLeft(), groove.topRight())
        else:
            color.setAlpha(155 if state == Qt.Checked else 85)
            painter.setPen(Qt.NoPen)
            painter.setBrush(color)
            eye = QtGui.QPainterPath()
            eye.moveTo(x, y)
            eye.cubicTo(x + 3, y - 4.5, x + 8, y - 4.5, x + 11, y)
            eye.cubicTo(x + 8, y + 4.5, x + 3, y + 4.5, x, y)
            pupil = QtGui.QPainterPath()
            pupil.addEllipse(QtCore.QPointF(x + 5.5, y), 2.1, 2.1)
            painter.drawPath(eye.subtracted(pupil))
            painter.drawEllipse(QtCore.QPointF(x + 5.5, y), 0.9, 0.9)
        painter.restore()


class UniqueLabelQListWidget(_EscapableQListWidget):
    batch_edit_requested = QtCore.pyqtSignal(str, object, bool)
    visibility_requested = QtCore.pyqtSignal(str, object, bool, bool)

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent=parent)
        self.group_color = None
        self.visibility_state = None
        self.itemChanged.connect(self._visibility_changed)
        self._drag_label: tuple[QtCore.QPoint, str, int | None] | None = None
        self.setItemDelegate(_GroupListDelegate(parent=self))

    def _eye_rect(self, item) -> QtCore.QRect:
        return _visibility_icon_rect(
            self.visualItemRect(item), self.fontMetrics(), item.data(_GROUP_KEY_ROLE) is not None
        )

    def _visibility_changed(self, item) -> None:
        state = item.checkState()
        if item.data(Qt.UserRole + 4) == state:
            return
        with QtCore.QSignalBlocker(self):
            item.setData(Qt.UserRole + 4, state)
        group = item.data(_GROUP_KEY_ROLE)
        label = item.data(Qt.UserRole)
        if group is not None:
            self.visibility_requested.emit(*group, True, item.checkState() == Qt.Checked)
        elif isinstance(label, str):
            self.visibility_requested.emit(label, None, False, item.checkState() == Qt.Checked)

    def update_visibility_checks(self) -> None:
        if self.visibility_state is None:
            return
        with QtCore.QSignalBlocker(self):
            for row in range(self.count()):
                item = self.item(row)
                group = item.data(_GROUP_KEY_ROLE)
                label = item.data(Qt.UserRole)
                if group is not None:
                    state = self.visibility_state(*group, True)
                elif isinstance(label, str):
                    state = self.visibility_state(label, None, False)
                else:
                    continue
                item.setData(Qt.UserRole + 4, state)
                item.setCheckState(state)

    def mousePressEvent(self, mouseEvent: QtGui.QMouseEvent) -> None:  # ty: ignore[invalid-method-override]
        self._drag_label = None
        item = self.itemAt(mouseEvent.pos())
        if item and mouseEvent.button() == Qt.LeftButton:
            check = self._eye_rect(item)
            if check.contains(mouseEvent.pos()):
                mouseEvent.accept()
                return
            groups = item.data(_GROUPS_ROLE)
            if groups and mouseEvent.pos().x() < check.right() + 22:
                self._toggle_groups(item)
                mouseEvent.accept()
                return
            label, group_id = item.data(_GROUP_KEY_ROLE) or (item.data(Qt.UserRole), None)
            if isinstance(label, str):
                self._drag_label = mouseEvent.pos(), label, group_id
        super().mousePressEvent(mouseEvent)
        if not self.indexAt(mouseEvent.pos()).isValid():
            self.clearSelection()

    def mouseMoveEvent(self, event: QtGui.QMouseEvent) -> None:
        if self._drag_label is not None and event.buttons() & Qt.LeftButton:
            origin, label, group_id = self._drag_label
            if (event.pos() - origin).manhattanLength() >= QtWidgets.QApplication.startDragDistance():
                self._drag_label = None
                mime = QtCore.QMimeData()
                mime.setData(LABEL_RECTANGLE_MIME, json.dumps((label, group_id)).encode("utf-8"))
                drag = QtGui.QDrag(self)
                drag.setMimeData(mime)
                drag.exec_(Qt.CopyAction)
                event.accept()
                return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QtGui.QMouseEvent) -> None:
        self._drag_label = None
        item = self.itemAt(event.pos())
        if item and event.button() == Qt.LeftButton and self._eye_rect(item).contains(event.pos()):
            item.setCheckState(Qt.Unchecked if item.checkState() == Qt.Checked else Qt.Checked)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: QtGui.QMouseEvent) -> None:
        item = self.itemAt(event.pos())
        if item and event.button() == Qt.LeftButton:
            if self._eye_rect(item).contains(event.pos()):
                event.accept()
                return
            group = item.data(_GROUP_KEY_ROLE)
            label = item.data(Qt.UserRole)
            if group is not None:
                self.batch_edit_requested.emit(*group, True)
                return
            if label is not None:
                if not item.data(_GROUPS_ROLE) or event.pos().x() >= self._eye_rect(item).right() + 22:
                    self.batch_edit_requested.emit(label, None, False)
                return
        super().mouseDoubleClickEvent(event)

    def label_names(self) -> list[str]:
        return [
            name for row in range(self.count())
            if isinstance(name := self.item(row).data(Qt.UserRole), str)
        ]

    def expanded_labels(self) -> set[str]:
        return {
            item.data(Qt.UserRole)
            for row in range(self.count())
            if (groups := (item := self.item(row)).data(_GROUPS_ROLE)) and groups[2]
        }

    def find_label_item(self, label: str) -> QtWidgets.QListWidgetItem | None:
        for row in range(self.count()):
            item = self.item(row)
            if item and item.data(Qt.UserRole) == label:
                return item
        return None

    def _toggle_groups(self, item: QtWidgets.QListWidgetItem) -> None:
        ids, title, expanded, counts = item.data(_GROUPS_ROLE)
        row = self.row(item) + 1
        if expanded:
            for _ in ids:
                self.takeItem(row)
        else:
            for offset, group_id in enumerate(ids):
                text = "无 ID" if group_id is None else str(group_id)
                child = QtWidgets.QListWidgetItem(text)
                child.setData(_GROUP_KEY_ROLE, (item.data(Qt.UserRole), group_id))
                child.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
                with QtCore.QSignalBlocker(self):
                    child.setData(Qt.UserRole + 4, Qt.Checked)
                    child.setCheckState(Qt.Checked)
                child.setData(_IMAGE_COUNT_ROLE, counts[group_id])
                self.insertItem(row + offset, child)
        item.setData(_GROUPS_ROLE, (ids, title, not expanded, counts))
        item.setText(("\u25b8" if expanded else "\u25be") + " " + title)

        self.update_visibility_checks()

    def label_index(self, label: str) -> int:
        index = 0
        for row in range(self.count()):
            value = self.item(row).data(Qt.UserRole)
            if value == label:
                break
            if value is not None:
                index += 1
        return index

    def add_label_item(
        self, label: str, color: tuple[int, int, int], *,
        group_counts: dict[int | None, int] | None = None, expanded: bool = False,
    ) -> None:
        if self.find_label_item(label):
            raise ValueError(f"Item for label '{label}' already exists")

        item = QtWidgets.QListWidgetItem()
        item.setData(Qt.UserRole, label)  # for find_label_item
        item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
        item.setData(Qt.UserRole + 4, Qt.Checked)
        item.setCheckState(Qt.Checked)
        title = format_label_with_color_dot(text=label, color=color)
        if group_counts:
            numeric_count = sum(group_id is not None for group_id in group_counts)
            item.setToolTip(f"{numeric_count} Group ID")
            ids: list[int | None] = sorted(group_id for group_id in group_counts if group_id is not None)
            if None in group_counts:
                ids.append(None)
            item.setData(_GROUPS_ROLE, (ids, title, False, group_counts))
            title = "\u25b8 " + title
        item.setText(title)
        self.addItem(item)
        if expanded and group_counts:
            self._toggle_groups(item)
