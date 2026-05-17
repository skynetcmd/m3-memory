import os
from setuptools import setup

try:
    from mypyc.build import mypycify
    # Only compile these stateless, high-frequency modules
    ext_modules = mypycify([
        "bin/memory/util.py",
        "bin/memory/fts.py"
    ])
except ImportError:
    ext_modules = []

setup(
    ext_modules=ext_modules,
)
