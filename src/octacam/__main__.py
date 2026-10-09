"""`python -m octacam`: how a detached processing job re-execs octacam in its
parent's interpreter (`process_jobs.spawn_detached`). The `__name__` guard
keeps a mere import (a `pkgutil` walk) from running the CLI.
"""

from octacam.cli import main

if __name__ == "__main__":
    main()
