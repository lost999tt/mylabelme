from __future__ import annotations

from collections.abc import Callable

import PIL.Image
import PIL.ImageEnhance
from PyQt5 import QtWidgets
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QImage


NEUTRAL_TONE = 50


def adjusted_image(img: PIL.Image.Image, brightness: int, contrast: int) -> QImage:
    alpha = img.getchannel("A") if "A" in img.getbands() else None
    if img.mode != "RGB":
        img = img.convert("RGB")
    if brightness != NEUTRAL_TONE:
        img = PIL.ImageEnhance.Brightness(img).enhance(brightness / NEUTRAL_TONE)
    if contrast != NEUTRAL_TONE:
        img = PIL.ImageEnhance.Contrast(img).enhance(contrast / NEUTRAL_TONE)

    fmt = QImage.Format_RGB888
    if alpha is not None:
        img = img.convert("RGBA")
        img.putalpha(alpha)
        fmt = QImage.Format_RGBA8888
    return QImage(img.tobytes(), img.width, img.height, img.width * len(img.getbands()), fmt)


class BrightnessContrastDialog(QtWidgets.QDialog):
    def __init__(
        self,
        img: PIL.Image.Image,
        callback: Callable[[QImage], None],
        parent: QtWidgets.QWidget | None = None,
        *,
        brightness: int = NEUTRAL_TONE,
        contrast: int = NEUTRAL_TONE,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(self.tr("Brightness/Contrast"))
        self.setModal(True)

        self._on_image_changed = callback

        sliders = {}
        layouts = {}
        for title, initial in (("Brightness:", brightness), ("Contrast:", contrast)):
            layout = QtWidgets.QHBoxLayout()
            title_label = QtWidgets.QLabel(self.tr(title))
            title_label.setFixedWidth(75)
            layout.addWidget(title_label)
            #
            slider = QtWidgets.QSlider(Qt.Horizontal)
            slider.setRange(0, 3 * NEUTRAL_TONE)
            slider.setValue(initial)
            layout.addWidget(slider)
            #
            value_label = QtWidgets.QLabel(f"{slider.value() / NEUTRAL_TONE:.2f}")
            value_label.setAlignment(Qt.AlignRight)
            layout.addWidget(value_label)
            #
            slider.valueChanged.connect(lambda _: self.apply())
            slider.valueChanged.connect(
                lambda _,
                value_label_=value_label,
                slider_=slider: value_label_.setText(
                    f"{slider_.value() / NEUTRAL_TONE:.2f}"
                )
            )
            layouts[title] = layout
            sliders[title] = slider

        self.slider_brightness = sliders["Brightness:"]
        self.slider_contrast = sliders["Contrast:"]
        del sliders

        v_layout = QtWidgets.QVBoxLayout()
        v_layout.addLayout(layouts["Brightness:"])
        v_layout.addLayout(layouts["Contrast:"])
        del layouts
        reset = QtWidgets.QPushButton("复原", self)
        reset.setAutoDefault(False)
        reset.clicked.connect(self._reset)
        v_layout.addWidget(reset, alignment=Qt.AlignRight)
        self.setLayout(v_layout)

        self.img = img

    def _reset(self) -> None:
        self.slider_brightness.setValue(NEUTRAL_TONE)
        self.slider_contrast.setValue(NEUTRAL_TONE)

    def apply(self) -> None:
        self._on_image_changed(
            adjusted_image(self.img, self.slider_brightness.value(), self.slider_contrast.value())
        )
