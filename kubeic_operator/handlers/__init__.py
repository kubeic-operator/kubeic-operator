"""Kopf handler modules, imported here so that their decorators actually run.

`kopf run <file>` builds its watch set once, from whatever `@kopf.on.*`
decorators have executed by the time the entrypoint module finishes loading:
observation.py's `resource_observer` scans the registry a single time with
`group=None`, before anything of ours beyond module scope has run. Nothing
registered later is picked up — `revise_resources` afterwards only ever
re-scans the group of a CRD that itself changed.

So a handler module reached only by a function-level import is never watched.
That was this package's state from the initial commit: it was empty, main.py
imported handlers.namespace lazily from `_bootstrap_checkers` on the audit
thread, and so `kopf run` saw zero selectors and no handler ever fired in
production.

New handler modules belong in the import below, not in a function-level one.
"""

from kubeic_operator.handlers import namespace  # noqa: F401
