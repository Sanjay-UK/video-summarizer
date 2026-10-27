"""Re-export of the fake S3 client. The implementation lives in tests/fake_s3_client.py, the folder CI runs."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))

from fake_s3_client import *  # noqa: E402,F401,F403
