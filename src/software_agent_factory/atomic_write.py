"""One atomic text write for the factory's JSON files: temp file, then ``os.replace``."""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

_DEFAULT_MODE = 0o666


def write_text_atomic(destination: Path, content: str, *, mode: int = _DEFAULT_MODE) -> None:
    """Write ``content`` to a temp file beside ``destination``, then ``os.replace`` it in.

    Readers see the old file or the whole new one. The temp file is created with
    ``mode`` (the umask can only clear bits), so a private file is never
    readable by others, not even for a moment. A failure removes the temp file
    and leaves ``destination`` as it was.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_path = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
    try:
        descriptor = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(temp_path, destination)
    except OSError:
        temp_path.unlink(missing_ok=True)
        raise
