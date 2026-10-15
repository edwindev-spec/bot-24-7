"""Write complete files beside their destination before replacing them."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".moonveil-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def write_text(path: Path, content: str, *, encoding: str = "utf-8") -> None:
    write_bytes(path, content.encode(encoding))
