"""Enable ``python -m octacam`` — the entry point for detached processing jobs.

``process_jobs.spawn_detached`` re-execs octacam with the current interpreter as
``[sys.executable, "-m", "octacam", ...]`` so a background job runs in the same
virtualenv as its parent (rather than relying on an ``octacam`` console script
being on ``PATH``). This module makes that invocation resolve to :func:`main`.
"""

from octacam.cli import main

main()
