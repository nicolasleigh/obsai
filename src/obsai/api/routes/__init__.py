"""HTTP routes.

One module per resource, all mounted under ``/api/v1`` by
:func:`obsai.api.app.create_app`. The modules are thin by construction: they
resolve their dependencies, call one application service, and hand the result back.
Anything that decides *what* happens belongs in ``obsai.application``.
"""
