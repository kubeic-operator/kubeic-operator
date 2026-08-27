"""Tests for the kopf persistence stubs.

These verify cause detection end-to-end through kopf's own pipeline, not just
our two classes in isolation. That isolation is precisely what hid the original
bug: both stubs looked individually reasonable, and the breakage only existed in
how kopf composes them (#65).
"""
import logging

from kubeic_operator.main import (
    DIFFBASE_STORAGE,
    _InMemoryDiffBaseStorage,
    _NoWriteProgressStorage,
)


def _body(uid="u-1", name="ns1", labels=None):
    return {
        "metadata": {"name": name, "uid": uid, "labels": labels or {"a": "b"}},
        "spec": {},
    }


class TestNoWriteProgressStorage:
    def test_clear_returns_the_essence_rather_than_none(self):
        # The root cause. kopf pipes both sides of its diff through clear()
        # and expects the essence back; returning None nulled `new`, so
        # diffbase_storage.store() — guarded by `if cause.new is not None` —
        # was never reached, defeating diff-base tracking entirely.
        essence = {"metadata": {"labels": {"a": "b"}}}
        assert _NoWriteProgressStorage().clear(essence=essence) == essence

    def test_clear_deep_copies_so_callers_cannot_mutate_stored_state(self):
        essence = {"metadata": {"labels": {"a": "b"}}}
        result = _NoWriteProgressStorage().clear(essence=essence)
        result["metadata"]["labels"]["a"] = "mutated"
        assert essence["metadata"]["labels"]["a"] == "b"

    def test_write_methods_stay_inert(self):
        # These must remain no-ops: the operator holds no namespace patch
        # permission, which is why the stubs exist at all.
        storage = _NoWriteProgressStorage()
        assert storage.fetch(body=_body()) is None
        assert storage.store(body=_body(), patch={}, key="k", record=None) is None
        assert storage.purge(body=_body(), patch={}, key="k") is None


class TestInMemoryDiffBaseStorage:
    def test_round_trips_an_essence_by_uid(self):
        storage = _InMemoryDiffBaseStorage()
        essence = {"metadata": {"labels": {"a": "b"}}}
        storage.store(body=_body(uid="u-1"), patch={}, essence=essence)
        assert storage.fetch(body=_body(uid="u-1")) == essence

    def test_unknown_uid_fetches_none(self):
        storage = _InMemoryDiffBaseStorage()
        assert storage.fetch(body=_body(uid="never-seen")) is None

    def test_separate_uids_do_not_collide(self):
        storage = _InMemoryDiffBaseStorage()
        storage.store(body=_body(uid="u-1"), patch={}, essence={"n": 1})
        storage.store(body=_body(uid="u-2"), patch={}, essence={"n": 2})
        assert storage.fetch(body=_body(uid="u-1")) == {"n": 1}
        assert storage.fetch(body=_body(uid="u-2")) == {"n": 2}

    def test_body_without_uid_is_handled_safely(self):
        storage = _InMemoryDiffBaseStorage()
        bodiless = {"metadata": {}}
        storage.store(body=bodiless, patch={}, essence={"n": 1})
        assert storage.fetch(body=bodiless) is None
        assert len(storage) == 0

    def test_retain_evicts_only_the_dead(self):
        # Without eviction the dict grows for the operator's whole lifetime on
        # clusters that churn namespaces.
        storage = _InMemoryDiffBaseStorage()
        for uid in ("u-1", "u-2", "u-3"):
            storage.store(body=_body(uid=uid), patch={}, essence={"uid": uid})

        evicted = storage.retain({"u-1", "u-3"})

        assert evicted == 1
        assert len(storage) == 2
        assert storage.fetch(body=_body(uid="u-2")) is None
        assert storage.fetch(body=_body(uid="u-1")) == {"uid": "u-1"}

    def test_retain_with_nothing_live_clears_everything(self):
        storage = _InMemoryDiffBaseStorage()
        storage.store(body=_body(), patch={}, essence={"n": 1})
        assert storage.retain(set()) == 1
        assert len(storage) == 0

    def test_module_singleton_is_the_right_type(self):
        # on_startup hands this exact instance to kopf, and _reconcile_checkers
        # prunes it, so they must be the same object.
        assert isinstance(DIFFBASE_STORAGE, _InMemoryDiffBaseStorage)


class TestCauseDetectionEndToEnd:
    """Drives kopf's real cause detection with our storages.

    Depends on kopf internals (`kopf._core.intents.causes`) by necessity: the
    bug was in how kopf composes the two storages, so nothing short of running
    its pipeline can prove the fix. If a kopf upgrade moves these, this failing
    is the correct outcome — the fix rests on that behaviour.
    """

    @staticmethod
    def _run_cycle(prog, diffbase, raw, initial):
        from kopf._cogs.structs import bodies, diffs, patches
        from kopf._core.intents.causes import detect_changing_cause

        body = bodies.Body(raw)
        old = diffbase.fetch(body=body)
        new = diffbase.build(body=body, extra_fields=frozenset())
        old = prog.clear(essence=old) if old is not None else None
        new = prog.clear(essence=new) if new is not None else None

        cause = detect_changing_cause(
            raw_event={"type": "MODIFIED", "object": raw},
            body=body, old=old, new=new, diff=diffs.diff(old, new),
            initial=initial, resource=None, indices=None,
            logger=logging.getLogger("test"), patch=patches.Patch(),
            memo=None, finalizer="x",
        )
        # Mirrors processing.py once handlers are done or skipped.
        if cause.new is not None and cause.old != cause.new:
            diffbase.store(body=body, patch=patches.Patch(), essence=cause.new)
        return str(cause.reason)

    def test_create_fires_once_then_repeats_are_noop(self):
        prog, diffbase = _NoWriteProgressStorage(), _InMemoryDiffBaseStorage()
        raw = _body()

        first = self._run_cycle(prog, diffbase, raw, initial=True)
        second = self._run_cycle(prog, diffbase, raw, initial=False)
        third = self._run_cycle(prog, diffbase, raw, initial=False)

        # Previously every one of these was "create", so the handler re-ran
        # deploy_checker on every namespace event.
        assert first == "create"
        assert second == "noop"
        assert third == "noop"

    def test_a_real_edit_is_an_update_not_another_create(self):
        prog, diffbase = _NoWriteProgressStorage(), _InMemoryDiffBaseStorage()
        self._run_cycle(prog, diffbase, _body(labels={"a": "b"}), initial=True)

        edited = self._run_cycle(
            prog, diffbase, _body(labels={"a": "changed"}), initial=False,
        )
        settled = self._run_cycle(
            prog, diffbase, _body(labels={"a": "changed"}), initial=False,
        )

        # No on.update handler is registered, so "update" means nothing runs.
        assert edited == "update"
        assert settled == "noop"

    def test_a_null_returning_clear_reintroduces_the_bug(self):
        # Pins why clear() must return the essence: with the old stub every
        # cycle is a creation again, however good the diff-base storage is.
        import kopf

        class _NullClearProgress(_NoWriteProgressStorage):
            def clear(self, *, essence):
                return None

        prog, diffbase = _NullClearProgress(), _InMemoryDiffBaseStorage()
        raw = _body()
        reasons = [
            self._run_cycle(prog, diffbase, raw, initial=True),
            self._run_cycle(prog, diffbase, raw, initial=False),
            self._run_cycle(prog, diffbase, raw, initial=False),
        ]

        assert reasons == ["create", "create", "create"]
        assert isinstance(prog, kopf.ProgressStorage)
