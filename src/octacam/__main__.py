"""Enable ``python -m octacam`` — the entry point for detached processing jobs.

``process_jobs.spawn_detached`` re-execs octacam with the current interpreter as
``[sys.executable, "-m", "octacam", ...]`` so a background job runs in the same
virtualenv as its parent (rather than relying on an ``octacam`` console script
being on ``PATH``). This module makes that invocation resolve to :func:`main`.

The ``__name__`` guard matters here: ``python -m octacam`` runs this file *as*
``__main__``, so the CLI still starts, but merely importing ``octacam.__main__``
(a ``pkgutil`` walk, a docs/API scraper, an editor's symbol index) no longer runs
the CLI and exits the host process.
"""

from octacam.cli import main

if __name__ == "__main__":
    main()
