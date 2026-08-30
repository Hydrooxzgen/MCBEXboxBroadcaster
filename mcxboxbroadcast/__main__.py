"""Allow running with ``python -m mcxboxbroadcast``.

The standalone entry point lives in ``main.py`` next to the package (mirroring
the repository layout), so bootstrap the path before importing it.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import main

if __name__ == "__main__":
    main()
