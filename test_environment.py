"""Checks that every library Command depends on can be imported.

Run with:  python test_environment.py
"""

import importlib
import sys

# (pip package name, Python import name)
LIBRARIES = [
    ("pypsa", "pypsa"),
    ("networkx", "networkx"),
    ("plotly", "plotly"),
    ("scikit-learn", "sklearn"),
    ("pandas", "pandas"),
    ("numpy", "numpy"),
    ("streamlit", "streamlit"),
    ("scipy", "scipy"),
    ("joblib", "joblib"),
]


def main() -> int:
    print(f"Python {sys.version.split()[0]}")
    failures = []

    for package, module_name in LIBRARIES:
        try:
            module = importlib.import_module(module_name)
            version = getattr(module, "__version__", "unknown")
            print(f"  OK    {package:<14} {version}")
        except Exception as exc:
            print(f"  FAIL  {package:<14} {exc}")
            failures.append(package)

    if failures:
        print(f"\n{len(failures)} library(s) failed to import: {', '.join(failures)}")
        print("Run: bash setup_env.sh")
        return 1

    print("\nCommand environment ready")
    return 0


if __name__ == "__main__":
    sys.exit(main())