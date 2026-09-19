"""Application services shared by the CLI and the HTTP adapter.

Everything in this package is framework-free: no ``typer``, no ``rich``, no
``fastapi``. Adapters parse their own input, call in here, and render whatever
comes back. That constraint is what lets the same logic back a terminal command
and an HTTP endpoint without either one owning the behaviour.

See ``docs/frontend-implementation-plan.zh-CN.md`` §4 for the extraction plan.
"""
