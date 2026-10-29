"""Rebuild the index page and nav list for the Wise-Sayings pages in the website's demos folder.

    python tools/build_demos_index.py [demos folder]

Writes sayings-index.html and sayings-nav.js (see compendium/pages.py). Only this project's
pages (sayings-*.html) are listed; other projects' files and the folder's own index.html are
left alone. Run it after every publish.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from compendium import pages  # noqa: E402

DEFAULT_DIR = Path(r"D:\AWS3\websites\toughguycomputing_net\www\demos")

if __name__ == "__main__":
    folder = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DIR
    n = pages.build(folder, prefix="sayings-")
    print(f"Wrote {folder / pages.INDEX} and {folder / pages.NAV} ({n} pages)")
