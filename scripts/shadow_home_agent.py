#!/usr/bin/env python3
"""Run the private Shadow home-PC companion service."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from companion.home_agent import main


if __name__ == "__main__":
    main()

