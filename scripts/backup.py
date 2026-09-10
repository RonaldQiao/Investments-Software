from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.main import backup_database

if __name__ == "__main__":
    print(backup_database())
