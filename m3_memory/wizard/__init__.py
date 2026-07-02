"""Internal submodules backing m3_memory.setup_wizard.

Split out of the former monolithic setup_wizard.py (1710 LOC). setup_wizard.py
remains the facade every importer and test uses (`m3_memory.setup_wizard`) —
these submodules hold only pieces that are never monkeypatched by tests and
never call anything that is. See setup_wizard.py's module docstring / the
comments at each import site for the constraint that kept the rest in place.
"""
