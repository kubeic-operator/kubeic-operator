"""The entrypoint must register the handlers by the time it finishes loading.

`kopf run --standalone -A /app/kubeic_operator/main.py` (the Dockerfile
ENTRYPOINT) loads main.py, builds its watch set from whatever `@kopf.on.*`
decorators have executed, and only then starts watching. From the initial
commit main.py imported the handler modules solely from inside functions, so at
that moment nothing was registered: kopf watched nothing and all four handlers
were dead code for five months of production.

The check runs in a subprocess for two reasons. Only a fresh interpreter can
answer "was this imported by loading main.py", since the rest of this suite
imports the handler modules directly. And loading main.py through kopf's own
`loaders.preload` is what `kopf run` literally does (kopf/cli.py), including the
pseudo-module name that keeps it out of sys.modules as `kubeic_operator.main` —
so this exercises the real entrypoint path rather than an approximation of it.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MAIN_PY = REPO_ROOT / "kubeic_operator" / "main.py"

# Runs in the child interpreter. Mirrors kopf/cli.py: `loaders.preload(paths=...)`
# is the entirety of how `kopf run <file>` gets handlers into the registry.
_PROBE = """
import json, sys

from kopf._cogs.helpers import loaders

loaders.preload(paths=[sys.argv[1]], modules=[])

result = {
    "handler_modules": sorted(
        name for name in sys.modules if name.startswith("kubeic_operator.handlers.")
    ),
}

# Secondary, and deliberately tolerant: the registry internals kopf exposes here
# are private and have moved between versions. A failure to read them must not
# be reported as a failure to register.
try:
    from kopf._core.intents import registries

    changing = registries.get_default_registry()._changing
    result["selectors"] = sorted(str(s) for s in changing.get_all_selectors())
    result["handler_count"] = len(changing.get_all_handlers())
except Exception as exc:
    result["selector_error"] = "%s: %s" % (type(exc).__name__, exc)

print(json.dumps(result))
"""


@pytest.fixture(scope="module")
def preloaded():
    """Load main.py the way `kopf run` does, in a clean interpreter."""
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE, str(MAIN_PY)],
        capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=120,
    )
    if proc.returncode != 0:
        pytest.fail(
            f"Loading {MAIN_PY} the way `kopf run` does failed "
            f"(exit {proc.returncode}):\n{proc.stderr}"
        )
    # main.py configures logging at import, so take only the JSON line.
    return json.loads(proc.stdout.strip().splitlines()[-1])


class TestKopfRunRegistersHandlers:
    @pytest.mark.parametrize("module", [
        "kubeic_operator.handlers.namespace",
    ])
    def test_handler_module_is_imported_by_loading_main(self, preloaded, module):
        # The decorators live at module scope in these files, so "imported" and
        # "registered" are the same event. Asserting on sys.modules rather than
        # on kopf's registry keeps the regression pinned to something public.
        assert module in preloaded["handler_modules"], (
            f"{module} was not imported by loading main.py, so its @kopf.on.* "
            f"decorators never ran and kopf will not watch its resources. "
            f"Imported: {preloaded['handler_modules']}"
        )

    def test_kopf_watches_namespaces(self, preloaded):
        # Secondary check: tolerated if this kopf keeps its registry elsewhere.
        if "selector_error" in preloaded:
            pytest.skip(f"kopf registry not introspectable: {preloaded['selector_error']}")

        selectors = " ".join(preloaded["selectors"])
        assert "namespaces" in selectors, f"namespaces not watched; selectors: {selectors}"
        assert preloaded["handler_count"] >= 1, (
            f"expected on_namespace_create to be registered, got {preloaded['handler_count']} handlers"
        )

    def test_no_policy_watch(self, preloaded):
        # imageauditpolicies is deliberately NOT watched. on_policy_change ran a
        # full cluster reconcile per event with no debounce, for a change the
        # reconcile loop already applies within scanIntervalSeconds. If a policy
        # handler is reintroduced it needs a debounce, and this assertion is the
        # reminder to think about that rather than a rule against it.
        if "selector_error" in preloaded:
            pytest.skip(f"kopf registry not introspectable: {preloaded['selector_error']}")

        selectors = " ".join(preloaded["selectors"])
        assert "imageauditpolicies" not in selectors, (
            f"a policy watch reappeared without a debounce; selectors: {selectors}"
        )
