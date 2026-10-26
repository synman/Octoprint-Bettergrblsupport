# coding=utf-8
"""Normalised copies of stored G-code for the built-in viewer (served by the plugin's /viewer routes).

The stored file is never changed. A normalised copy and its position map are built once per
(storage, file, mtime, size, normaliser version) and kept in the plugin data folder.
"""
import hashlib
import json
import logging
import os
import threading

from . import gcode_normalizer

_logger = logging.getLogger("octoprint.plugins.bettergrblsupport.viewer")

KEEP_ENTRIES = 20
KEEP_BYTES = 200 * 1024 * 1024
# one normalised copy may not pass this: an arc-heavy file expands about 570x (20,000 arcs of 21 bytes made
# 240 MB), and the build blocks the request that asked for it. Past it the viewer serves the original.
MAX_COPY_BYTES = 64 * 1024 * 1024

_locks = {}
_locks_guard = threading.Lock()
_failed = set()


def source_path(file_manager, storage, filename):
    """Path on disk of a stored file, or None when the storage has no local path or the file is missing.

    OctoPrint 2.0 has ``capabilities()``; 1.11 has none and raises NotImplementedError instead."""
    try:
        if storage not in getattr(file_manager, "registered_storages", [storage]):
            return None
        if hasattr(file_manager, "capabilities") and not file_manager.capabilities(storage).path_on_disk:
            return None
        path = file_manager.path_on_disk(storage, filename)
    except (NotImplementedError, KeyError, ValueError):
        return None
    if not path or not os.path.isfile(path):
        return None
    return path


def origin_shift(origin, width, length):
    """X/Y shift that puts a job on the viewer's bed, from Bettergrbl's own frame metadata
    (``bgs_origin``/``bgs_width``/``bgs_length``, written by ``_bgs.defer_generate_metadata_for_file``).

    The origin names where the work origin sits on the material: a job whose origin is its top-right corner
    runs in negative X and Y, so it is moved right by its width and up by its length."""
    try:
        width, length = float(width or 0), float(length or 0)
    except (TypeError, ValueError):
        return 0.0, 0.0
    origin = origin or ""
    if not origin.startswith("grbl"):
        return 0.0, 0.0
    dx = 0.0 if origin.endswith("Left") else width if origin.endswith("Right") else width / 2
    dy = 0.0 if origin.startswith("grblBottom") else length if origin.startswith("grblTop") else length / 2
    return dx, dy


def _lock_for(key):
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = _locks[key] = threading.Lock()
        return lock


def _evict(cache_dir):
    try:
        entries = []
        for name in os.listdir(cache_dir):
            if name.endswith(".gcode"):
                p = os.path.join(cache_dir, name)
                st = os.stat(p)
                entries.append((st.st_mtime, st.st_size, name[:-6]))
        entries.sort(reverse=True)
        total = 0
        for i, (_, size, key) in enumerate(entries):
            total += size
            if i > 0 and (i >= KEEP_ENTRIES or total > KEEP_BYTES):
                for ext in (".gcode", ".json"):
                    try:
                        os.remove(os.path.join(cache_dir, key + ext))
                    except OSError:
                        pass
    except OSError as e:
        _logger.warning("viewer cache eviction failed: %s", e)


def cached(cache_dir, storage, filename, src, shift=(0.0, 0.0)):
    """Return ``(gcode_path, map_path)`` for the normalised copy of ``src``, building it if needed.
    Returns None when normalising fails (the caller then serves the original file)."""
    st = os.stat(src)
    ident = "\0".join((storage, filename, str(st.st_mtime_ns), str(st.st_size), str(gcode_normalizer.VERSION),
                       "%.3f,%.3f" % tuple(shift)))
    key = hashlib.sha1(ident.encode("utf-8")).hexdigest()
    gpath = os.path.join(cache_dir, key + ".gcode")
    mpath = os.path.join(cache_dir, key + ".json")
    if key in _failed:
        return None
    with _lock_for(key):
        if os.path.isfile(gpath) and os.path.isfile(mpath):
            return gpath, mpath
        os.makedirs(cache_dir, exist_ok=True)
        tmp_g, tmp_m = gpath + ".tmp", mpath + ".tmp"
        try:
            with open(tmp_g, "wb") as out:
                result = gcode_normalizer.normalize_file(src, out, shift=shift, max_out_bytes=MAX_COPY_BYTES)
            with open(tmp_m, "w") as out:
                json.dump(result["map"], out, separators=(",", ":"), allow_nan=False)
            os.replace(tmp_m, mpath)
            os.replace(tmp_g, gpath)
        except Exception as e:
            _failed.add(key)
            if isinstance(e, gcode_normalizer.OutputTooLarge):
                _logger.warning("not normalising %s:%s for the viewer, %s; serving the original", storage, filename, e)
            else:
                _logger.exception("could not normalise %s:%s for the viewer; serving the original", storage, filename)
            for p in (tmp_g, tmp_m):
                try:
                    os.remove(p)
                except OSError:
                    pass
            return None
    _evict(cache_dir)
    return gpath, mpath
