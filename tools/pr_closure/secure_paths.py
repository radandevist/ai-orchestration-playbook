from __future__ import annotations

import hashlib
import os
import secrets
import stat
from typing import Tuple


class SecurePathError(OSError):
    """Raised when a contained path cannot be opened without following links."""


def _absolute_components(value, label: str) -> Tuple[str, Tuple[str, ...]]:
    if not isinstance(value, (str, os.PathLike)):
        raise SecurePathError("{0} path must be absolute".format(label))
    raw = os.path.expanduser(os.fspath(value))
    if not os.path.isabs(raw):
        raise SecurePathError("{0} path must be absolute".format(label))
    absolute = os.path.abspath(raw)
    components = tuple(component for component in absolute.split(os.sep) if component)
    if any(component in (".", "..") for component in components):
        raise SecurePathError("{0} path contains an unsafe component".format(label))
    return absolute, components


def _open_directory_chain(components: Tuple[str, ...], label: str) -> int:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise SecurePathError("platform does not provide O_NOFOLLOW for {0}".format(label))
    flags = os.O_RDONLY | os.O_DIRECTORY | nofollow
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    fd = os.open(os.sep, flags)
    try:
        for component in components:
            next_fd = os.open(component, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def _directory_fd(path, label: str) -> int:
    _absolute, components = _absolute_components(path, label)
    return _open_directory_chain(components, label)


def ensure_directory(path) -> None:
    """Create a directory chain without following a mutable ancestor link."""
    _absolute, components = _absolute_components(path, "directory")
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise SecurePathError("platform does not provide O_NOFOLLOW for directory creation")
    flags = os.O_RDONLY | os.O_DIRECTORY | nofollow
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    fd = os.open(os.sep, flags)
    try:
        for component in components:
            try:
                next_fd = os.open(component, flags, dir_fd=fd)
            except FileNotFoundError:
                try:
                    os.mkdir(component, 0o700, dir_fd=fd)
                except FileExistsError:
                    # Another writer created this component.  Re-open it
                    # below with O_NOFOLLOW so a racing symlink still fails.
                    pass
                else:
                    os.fsync(fd)
                next_fd = os.open(component, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
    finally:
        os.close(fd)


def _fsync_directory_fd(fd: int, label: str) -> None:
    try:
        os.fsync(fd)
    except OSError as error:
        raise SecurePathError("cannot fsync {0} parent directory".format(label)) from error


def atomic_create(path, raw: bytes) -> bool:
    """Create one file with no replacement and durable file/parent fsyncs."""
    absolute, components = _absolute_components(path, "record")
    if not components:
        raise SecurePathError("record path must name a file")
    parent = os.path.dirname(absolute)
    ensure_directory(parent)
    parent_fd = _directory_fd(parent, "record parent")
    temp_name = ".tmp-" + secrets.token_hex(16)
    temp_fd = None
    try:
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        temp_fd = os.open(temp_name, flags, 0o600, dir_fd=parent_fd)
        view = memoryview(raw)
        while view:
            written = os.write(temp_fd, view)
            view = view[written:]
        os.fsync(temp_fd)
        os.close(temp_fd)
        temp_fd = None
        try:
            os.link(
                temp_name,
                components[-1],
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileExistsError:
            return False
        os.unlink(temp_name, dir_fd=parent_fd)
        temp_name = None
        _fsync_directory_fd(parent_fd, "record")
        return True
    finally:
        if temp_fd is not None:
            os.close(temp_fd)
        try:
            if temp_name is not None:
                try:
                    os.unlink(temp_name, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass
        finally:
            os.close(parent_fd)


def rename_no_replace(source, target) -> None:
    """Atomically rename ``source`` to an absent ``target`` and fsync parents."""
    source_absolute, source_components = _absolute_components(source, "source")
    target_absolute, target_components = _absolute_components(target, "target")
    source_parent = os.path.dirname(source_absolute)
    target_parent = os.path.dirname(target_absolute)
    source_fd = _directory_fd(source_parent, "source")
    target_fd = source_fd if source_parent == target_parent else _directory_fd(target_parent, "target")
    source_leaf_fd = None
    try:
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        source_leaf_fd = os.open(
            source_components[-1], os.O_RDONLY | nofollow, dir_fd=source_fd
        )
        opened = os.fstat(source_leaf_fd)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise SecurePathError("source must be a single-link regular file")
        try:
            import ctypes

            libc = ctypes.CDLL(None, use_errno=True)
            renameat2 = libc.renameat2
        except (AttributeError, OSError) as error:
            raise SecurePathError("platform cannot provide atomic no-replace rename") from error
        renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        renameat2.restype = ctypes.c_int
        result = renameat2(
            source_fd,
            source_components[-1].encode("utf-8"),
            target_fd,
            target_components[-1].encode("utf-8"),
            1,
        )
        if result != 0:
            error_number = ctypes.get_errno()
            if error_number == getattr(os, "EEXIST", 17):
                raise FileExistsError(error_number, os.strerror(error_number), target_absolute)
            raise OSError(error_number, os.strerror(error_number), source_absolute)
        _fsync_directory_fd(source_fd, "retirement source")
        if target_fd != source_fd:
            _fsync_directory_fd(target_fd, "retirement target")
    finally:
        if source_leaf_fd is not None:
            os.close(source_leaf_fd)
        os.close(source_fd)
        if target_fd != source_fd:
            os.close(target_fd)


def read_contained_file(path, root, label: str) -> Tuple[bytes, str]:
    """Read one regular file under ``root`` through a no-follow fd chain.

    The root and target are checked lexically, then every root and target
    ancestor is opened with ``O_NOFOLLOW`` relative to a pinned directory fd.
    No post-hoc ``realpath`` check is used as a substitute for the safe open.
    """
    root_absolute, root_components = _absolute_components(root, label + " root")
    path_absolute, _path_components = _absolute_components(path, label)
    try:
        if os.path.commonpath((root_absolute, path_absolute)) != root_absolute:
            raise SecurePathError("{0} path escapes the closure root".format(label))
    except ValueError as error:
        raise SecurePathError("{0} path is not contained by the closure root".format(label)) from error
    relative = os.path.relpath(path_absolute, root_absolute)
    relative_components = tuple(component for component in relative.split(os.sep) if component)
    if not relative_components:
        raise SecurePathError("{0} path must name a file below the closure root".format(label))

    root_fd = _open_directory_chain(root_components, label + " root")
    directory_fd = root_fd
    file_fd = None
    try:
        nofollow = getattr(os, "O_NOFOLLOW", None)
        flags = os.O_RDONLY | nofollow
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        for component in relative_components[:-1]:
            next_fd = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | nofollow,
                dir_fd=directory_fd,
            )
            os.close(directory_fd)
            directory_fd = next_fd
        file_fd = os.open(relative_components[-1], flags, dir_fd=directory_fd)
        opened = os.fstat(file_fd)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise SecurePathError("{0} must be a single-link regular file".format(label))
        chunks = []
        while True:
            chunk = os.read(file_fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
        raw = b"".join(chunks)
        try:
            entry = os.lstat(path_absolute)
        except OSError as error:
            raise SecurePathError("{0} changed identity during read".format(label)) from error
        if (
            not stat.S_ISREG(entry.st_mode)
            or entry.st_nlink != 1
            or (entry.st_dev, entry.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise SecurePathError("{0} changed identity during read".format(label))
        return raw, hashlib.sha256(raw).hexdigest()
    finally:
        if file_fd is not None:
            os.close(file_fd)
        os.close(directory_fd)
