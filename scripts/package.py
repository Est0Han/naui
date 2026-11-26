#!/usr/bin/env python3
"""Bootstrap the packaging CLI from either the checkout or installed resources."""
from pathlib import Path
import sys

sys.dont_write_bytecode = True
root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "src"))
from naui.installation import main

raise SystemExit(main(root))
