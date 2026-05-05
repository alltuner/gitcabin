# ABOUTME: Prints the pygments-token CSS rules gitcabin's blob view depends on.
# ABOUTME: Called by web-src/build.ts to bundle the rules into the main CSS.

from __future__ import annotations

import sys

from gitcabin.web.code import pygments_stylesheet


def main() -> None:
    sys.stdout.write(pygments_stylesheet())


if __name__ == "__main__":
    main()
