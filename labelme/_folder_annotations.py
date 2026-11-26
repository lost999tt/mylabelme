from __future__ import annotations

import base64
import io
import json
import math
import os
import stat as stat_module
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, NamedTuple

from loguru import logger
from PIL import Image

from labelme import __version__
from labelme._label_file import LABEL_FILE_SUFFIX, read_image_file


def _annotation_paths(directory: Path) -> Iterator[Path]:
    for path in directory.iterdir():
        if path.suffix.lower() == LABEL_FILE_SUFFIX and not path.is_symlink() and path.is_file():
            yield path


class _IndexedAnnotation(NamedTuple):
    stamp: tuple[int, int, int]
    image: str
    groups: set[tuple[str, int | None]]


class FolderLabelIndex:
    """Cache compact per-file summaries for the current output directory."""

    def __init__(self) -> None:
        self._directory: Path | None = None
        self._files: dict[Path, _IndexedAnnotation] = {}
        self._counts: dict[str, dict[int | None, int]] = {}
        self._dirty_paths: set[Path] = set()
        self._next_scan = 0.0

    def mark_changed(self, *paths: Path) -> None:
        directory = self._directory
        if directory is None:
            return
        for path in paths:
            path = path.absolute()
            if path.parent == directory or path.parent.resolve() == directory.resolve():
                self._dirty_paths.add(directory / path.name)

    def counts(self, directory: Path, *, refresh: bool = False) -> dict[str, dict[int | None, int]]:
        directory = directory.absolute()
        same_directory = self._directory == directory
        scan = refresh or not same_directory or time.monotonic() >= self._next_scan
        if not scan and not self._dirty_paths:
            return self._counts

        previous = self._files if same_directory else {}
        if scan:
            current: dict[Path, _IndexedAnnotation] = {}
            try:
                for path in _annotation_paths(directory):
                    if annotation := self._load_entry(path, None if path in self._dirty_paths else previous.get(path)):
                        current[path] = annotation
            except FileNotFoundError:
                pass
            except OSError as error:
                logger.warning("Cannot list annotation directory {}: {}", directory, error)
            self._next_scan = time.monotonic() + 10
        else:
            current = previous.copy()
            for path in self._dirty_paths:
                if annotation := self._load_entry(path, None):
                    current[path] = annotation
                else:
                    current.pop(path, None)
        self._dirty_paths.clear()
        if same_directory and current == previous:
            return self._counts
        self._directory, self._files = directory, current
        groups: dict[str, dict[int | None, set[str]]] = {}
        for annotation in current.values():
            for label, group_id in annotation.groups:
                groups.setdefault(label, {}).setdefault(group_id, set()).add(annotation.image)
        self._counts = {
            label: {group_id: len(images) for group_id, images in ids.items()}
            for label, ids in groups.items()
        }
        return self._counts

    def _load_entry(
        self, path: Path, cached: _IndexedAnnotation | None
    ) -> _IndexedAnnotation | None:
        try:
            stat = path.lstat()
            if not stat_module.S_ISREG(stat.st_mode):
                return None
            stamp = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
            return cached if cached and cached.stamp == stamp else self._read(path, stamp)
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as error:
            logger.warning("Cannot read annotations in {}: {}", path, error)
            return None

    @staticmethod
    def _read(path: Path, stamp: tuple[int, int, int]) -> _IndexedAnnotation:
        data = json.loads(path.read_bytes())
        shapes = data.get("shapes") if isinstance(data, dict) else None
        if not isinstance(shapes, list):
            return _IndexedAnnotation(stamp, str(path), set())
        relative = data.get("imagePath")
        image = (
            os.path.normcase(os.path.abspath(path.parent / relative))
            if isinstance(relative, str) and relative else str(path)
        )
        groups = set()
        for shape in shapes:
            if not isinstance(shape, dict) or not isinstance(label := shape.get("label"), str):
                continue
            group_id = shape.get("group_id")
            if group_id is None or type(group_id) is int:
                groups.add((label, group_id))
        return _IndexedAnnotation(stamp, image, groups)


def _folder_label_documents(directory: Path) -> Iterator[tuple[Path, bytes, dict[str, Any]]]:
    if not directory.is_dir():
        return
    for path in sorted(_annotation_paths(directory)):
        if path.resolve().parent != directory:
            continue
        original = path.read_bytes()
        try:
            data = json.loads(original.decode("utf-8-sig"))
            if not isinstance(data, dict) or "imagePath" not in data or "shapes" not in data:
                continue
            shapes = data["shapes"]
            if not isinstance(shapes, list) or any(
                not isinstance(shape, dict) or not isinstance(shape.get("label"), str)
                for shape in shapes
            ):
                raise ValueError("Invalid Labelme shapes")
        except ValueError as e:
            raise ValueError(f"{path}: {e}") from e
        yield path, original, data


def annotation_image_reference(image: Path, directory: Path) -> str:
    try:
        return os.path.relpath(image, directory)
    except ValueError:
        # Windows cannot produce relative paths across different drive letters.
        return str(image.absolute())


def _read_frame_labels(image: Path, target: Path) -> tuple[bytes, dict[str, Any]]:
    original = target.read_bytes()
    payload = json.loads(original.decode("utf-8-sig"))
    if not isinstance(payload, dict) or not isinstance(payload.get("shapes"), list):
        raise ValueError(f"Invalid annotations: {target}")
    reference = payload.get("imagePath")
    if not isinstance(reference, str) or (target.parent / reference).resolve() != image.resolve():
        raise ValueError(f"Annotation belongs to another image: {target}")
    return original, payload


def plan_frame_copy(
    image: Path, target: Path, shapes: list[dict[str, Any]], *, embed_image: bool,
    prior: tuple[Path, bytes, bytes] | None = None, fit_rectangles: bool = False,
) -> tuple[tuple[Path, bytes | None, bytes] | None, int]:
    if target.is_symlink():
        raise OSError(f"Cannot write through a link: {target}")
    if not image.is_file():
        raise FileNotFoundError(image)
    if prior is not None:
        _, original, updated = prior
        payload = json.loads(updated)
    elif target.is_file():
        original, payload = _read_frame_labels(image, target)
    else:
        original = None
        data = read_image_file(str(image))
        with Image.open(io.BytesIO(data)) as source:
            width, height = source.size
        payload = {
            "version": __version__, "flags": {}, "shapes": [],
            "imagePath": annotation_image_reference(image, target.parent),
            "imageData": base64.b64encode(data).decode("ascii") if embed_image else None,
            "imageWidth": width, "imageHeight": height,
        }

    def identity(shape: dict[str, Any]) -> tuple:
        if not isinstance(shape, dict) or not isinstance(shape.get("label"), str):
            raise ValueError(f"Invalid shapes: {target}")
        label = shape["label"]
        group = shape.get("group_id")
        if type(group) is int:
            return label, group
        kind = shape.get("shape_type")
        points = shape.get("points")
        if group is not None or not isinstance(kind, (str, type(None))) or not isinstance(points, list) or any(
            not isinstance(point, (list, tuple)) or len(point) != 2
            or any(type(value) not in (int, float) for value in point)
            for point in points
        ):
            raise ValueError(f"Invalid shape identity: {target}")
        return label, None, kind, tuple(tuple(point) for point in points)

    existing = payload["shapes"]
    identities = {identity(shape) for shape in existing}
    skipped = 0
    for shape in shapes:
        key = identity(shape)
        if key in identities:
            skipped += 1
            continue
        if fit_rectangles:
            points = _fit_box(_rectangle_box(shape), payload.get("imageWidth"), payload.get("imageHeight"))
            if points is None:
                skipped += 1
                continue
            shape = dict(shape, points=points)
        existing.append(shape)
        identities.add(key)
    if skipped == len(shapes):
        return prior, skipped
    updated = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    return (target, original, updated), skipped


def _rectangle_box(shape: dict[str, Any]) -> tuple[float, float, float, float] | None:
    points = shape.get("points")
    if shape.get("shape_type") != "rectangle" or not isinstance(points, (list, tuple)) or len(points) != 2 or any(
        not isinstance(point, (list, tuple)) or len(point) != 2
        or any(type(value) not in (int, float) or not math.isfinite(value) for value in point)
        for point in points
    ):
        return None
    (x0, y0), (x1, y1) = points
    return min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)


def _fit_box(box: tuple[float, float, float, float] | None, width: Any, height: Any) -> list[list[float]] | None:
    if box is None:
        return None
    left, top, right, bottom = box
    if type(width) is int and width > 1:
        left, right = max(0, left), min(width - 1, right)
    if type(height) is int and height > 1:
        top, bottom = max(0, top), min(height - 1, bottom)
    if right - left < 1 or bottom - top < 1:
        return None
    return [[left, top], [right, bottom]]


def frame_rectangle_anchors(
    image: Path, target: Path, *, updated: bytes | None = None
) -> dict[tuple[str, int], dict[str, Any]]:
    if updated is not None:
        payload = json.loads(updated)
    elif target.is_symlink() or not target.is_file():
        return {}
    else:
        _, payload = _read_frame_labels(image, target)
    candidates: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for shape in payload["shapes"]:
        if not isinstance(shape, dict):
            continue
        label, group = shape.get("label"), shape.get("group_id")
        if isinstance(label, str) and type(group) is int:
            candidates.setdefault((label, group), []).append(shape)
    return {
        key: shapes[0] for key, shapes in candidates.items()
        if len(shapes) == 1 and _rectangle_box(shapes[0]) is not None
    }


def interpolate_rectangle(
    left: dict[str, Any], right: dict[str, Any], fraction: float
) -> dict[str, Any]:
    start, end = _rectangle_box(left), _rectangle_box(right)
    assert start is not None and end is not None
    x0, y0, x1, y1 = [a + (b - a) * fraction for a, b in zip(start, end)]
    return dict(left, points=[[x0, y0], [x1, y1]])


def plan_frame_sync(
    image: Path, target: Path,
    deltas: dict[tuple[str, int], tuple[float, float, float, float]],
    *, weight: float = 1.0,
    positions: dict[tuple[str, int], dict[str, Any]] | None = None,
) -> tuple[Path, bytes, bytes] | None:
    if target.is_symlink() or not target.is_file():
        return None
    original, payload = _read_frame_labels(image, target)
    candidates: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for shape in payload["shapes"]:
        if not isinstance(shape, dict):
            continue
        label, group = shape.get("label"), shape.get("group_id")
        if isinstance(label, str) and type(group) is int and (label, group) in deltas:
            candidates.setdefault((label, group), []).append(shape)
    changed = False
    for key, shapes in candidates.items():
        if len(shapes) != 1 or shapes[0].get("shape_type") != "rectangle":
            continue
        shape = shapes[0]
        box = _rectangle_box(shape)
        if box is None:
            continue
        if positions is not None and key in positions:
            adjusted = _rectangle_box(positions[key])
        else:
            adjusted = tuple(value + delta * weight for value, delta in zip(box, deltas[key]))
        fitted = _fit_box(adjusted, payload.get("imageWidth"), payload.get("imageHeight"))
        if fitted is None:
            continue
        points = shape["points"]
        (x0, y0), (x1, y1) = points
        (left, top), (right, bottom) = fitted
        first_x, second_x = (left, right) if x0 <= x1 else (right, left)
        first_y, second_y = (top, bottom) if y0 <= y1 else (bottom, top)
        replacement = [[first_x, first_y], [second_x, second_y]]
        if replacement != points:
            shape["points"] = replacement
            changed = True
    if not changed:
        return None
    return target, original, json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


def write_folder_label_changes(
    directory: Path | None, changes: list[tuple[Path, bytes | None, bytes | None]]
) -> None:
    # Sync can target multiple annotation directories. Stage beside each target.
    staging_by_directory: dict[Path, Path] = {}
    committed = []
    try:
        for index, (path, original, updated) in enumerate(changes):
            parent = directory if directory is not None else path.parent
            if path.is_symlink() or path.resolve().parent != parent:
                raise OSError(f"Unexpected annotation path: {path}")
            if parent not in staging_by_directory:
                staging_by_directory[parent] = Path(
                    tempfile.mkdtemp(prefix=".labelme-rename-", dir=parent)
                )
            staging = staging_by_directory[parent]
            if original is not None:
                (staging / f"{index}.original").write_bytes(original)
            if updated is not None:
                (staging / f"{index}.updated").write_bytes(updated)
        for index, (path, original, updated) in enumerate(changes):
            parent = directory if directory is not None else path.parent
            if path.is_symlink() or path.resolve().parent != parent:
                raise OSError(f"Unexpected annotation path: {path}")
            staging = staging_by_directory[parent]
            staged = staging / f"{index}.updated"
            if updated is None:
                if original is None or path.read_bytes() != original:
                    raise OSError(f"File changed before removal: {path}")
                path.unlink()
            elif original is None:
                if os.name == "nt":
                    os.rename(staged, path)  # fails if the destination already exists
                else:
                    os.link(staged, path)
            else:
                if path.read_bytes() != original:
                    raise OSError(f"File changed before rename: {path}")
                os.replace(staged, path)
            committed.append((index, path, parent, staging, original is not None, updated))
    except OSError as error:
        rollback_errors = []
        for index, path, parent, staging, existed, updated in reversed(committed):
            try:
                if path.is_symlink() or path.resolve().parent != parent:
                    raise OSError(f"File changed during rename: {path}")
                if updated is None:
                    if os.name == "nt":
                        os.rename(staging / f"{index}.original", path)
                    else:
                        os.link(staging / f"{index}.original", path)
                    continue
                if path.read_bytes() != updated:
                    raise OSError(f"File changed during rename: {path}")
                if existed:
                    os.replace(staging / f"{index}.original", path)
                else:
                    path.unlink()
            except OSError as rollback_error:
                rollback_errors.append(f"{rollback_error} (backup: {staging})")
        if rollback_errors:
            raise OSError(
                f"{error}\nRollback incomplete:\n" + "\n".join(rollback_errors)
            ) from error
        for staging in staging_by_directory.values():
            _remove_label_rename_staging(staging)
        raise
    for staging in staging_by_directory.values():
        _remove_label_rename_staging(staging)


def _remove_label_rename_staging(staging: Path) -> None:
    try:
        for path in staging.iterdir():
            path.unlink()
        staging.rmdir()
    except OSError as e:
        logger.warning("Could not remove temporary rename directory {}: {}", staging, e)


def plan_folder_label_edit(
    directory: Path, label: str, group_id: int | None, new_label: str | None,
    new_id: int | None, *, match_id: bool,
) -> tuple[list[tuple[Path, bytes, bytes]], int, bool]:
    changes = []
    count = 0
    merge = False
    for path, original, data in _folder_label_documents(directory):
        matches = 0
        remaining = []
        for shape in data["shapes"]:
            saved_label = shape["label"]
            saved_id = shape.get("group_id")
            valid_id = not match_id or saved_id is None or type(saved_id) is int
            if valid_id and saved_label == label and (not match_id or saved_id == group_id):
                matches += 1
                if new_label is None:
                    continue
                shape["label"] = new_label
                if match_id and saved_id != new_id:
                    shape["group_id"] = new_id
            elif valid_id and new_label is not None and saved_label == new_label and (not match_id or saved_id == new_id):
                merge = True
            remaining.append(shape)
        if matches:
            data["shapes"] = remaining
            updated = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            changes.append((path, original, updated))
            count += matches
    return changes, count, merge
