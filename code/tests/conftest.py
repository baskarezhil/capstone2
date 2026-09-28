import os
import sys
import tempfile
import warnings
from pathlib import Path

# Isolate audit log / checkpoints per test session (must happen before autoheal is imported).
os.environ["AUTOHEAL_DATA_DIR"] = tempfile.mkdtemp(prefix="autoheal-test-")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
warnings.filterwarnings("ignore", category=DeprecationWarning)
