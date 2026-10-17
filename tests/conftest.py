import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("JARVIS_DATA_DIR", tempfile.mkdtemp(prefix="jarvis-test-"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
