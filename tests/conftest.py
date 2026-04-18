"""pytest configuration and fixtures for chatlog tests."""

import sys
import os

# Add bin/ directory to Python path so tests can import bin modules
bin_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "bin")
if bin_dir not in sys.path:
    sys.path.insert(0, bin_dir)
