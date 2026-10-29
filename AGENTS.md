Always work on a feature branch, never push directly to main. Use software engineering best practices like TDD.

Do not commit parquet files. They bloat the git history permanently; keep them out of the repo (write them outside it or leave them untracked).
