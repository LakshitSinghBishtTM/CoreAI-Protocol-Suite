"""Locate repo-level data directories (config/, migrations/).

Order: ``$COREAI_HOME``, then the nearest parent of this file that contains
the directory (source checkout or editable install), then the working
directory (Docker image, where config/ sits next to the app).
"""

import os
from pathlib import Path


def data_dir(name: str) -> Path:
    home = os.environ.get("COREAI_HOME")
    if home and (Path(home) / name).is_dir():
        return Path(home) / name
    for parent in Path(__file__).resolve().parents:
        if (parent / name).is_dir():
            return parent / name
    return Path.cwd() / name
