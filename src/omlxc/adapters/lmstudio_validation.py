"""LM Studio 适配器的 lms/SSH 参数安全校验(从 lmstudio.py 拆出, 控制 God Module 体量)。"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

_SAFE_TARGET_PART = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,252}[A-Za-z0-9])?$")
_SAFE_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/+@-]{0,511}$")


def _validate_model_token(value: str, *, label: str = "model") -> None:
    if not _SAFE_MODEL.fullmatch(value):
        raise ValueError(f"{label} is not a safe lms argument")
    if any(segment == ".." for segment in value.split("/")):
        raise ValueError(f"{label} must not contain path traversal")


def _validate_target(value: str) -> None:
    if value.startswith("-") or value.count("@") > 1:
        raise ValueError("SSH target is invalid")
    user, separator, host = value.rpartition("@")
    if not separator:
        host = value
        user = ""
    if not host or not _SAFE_TARGET_PART.fullmatch(host):
        raise ValueError("SSH target is invalid")
    if ".." in host:
        raise ValueError("SSH target is invalid")
    if user and not _SAFE_TARGET_PART.fullmatch(user):
        raise ValueError("SSH target is invalid")


def _validate_known_hosts(path: Path) -> None:
    if not path.is_absolute():
        raise ValueError("known_hosts file must use an absolute path")
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ValueError("known_hosts file must exist") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("known_hosts path must be a regular file")
    if metadata.st_uid != os.geteuid():
        raise ValueError("known_hosts file owner must match the current effective user")
    if metadata.st_mode & 0o022:
        raise ValueError("known_hosts file permissions must reject group/world writes")
    for ancestor in path.parents:
        try:
            ancestor_metadata = ancestor.lstat()
        except OSError as exc:
            raise ValueError("known_hosts path ancestor must exist") from exc
        if stat.S_ISLNK(ancestor_metadata.st_mode):
            raise ValueError("known_hosts path must not traverse a symlink")
    parent_metadata = path.parent.lstat()
    if parent_metadata.st_mode & 0o022:
        raise ValueError("known_hosts parent must reject group/world writes")
