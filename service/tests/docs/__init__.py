"""Tests that hold the top-level prose deliverables to the code they cite.

``test_doc_citations`` is both the suite and the repair tool. It enforces the **symbol**
half of every ``path:line`` citation in ``ARCHITECTURE.md`` and ``AI_USAGE.md`` on every
run, and re-derives the **integer** half from the working tree on demand::

    cd service && uv run python -m tests.docs.test_doc_citations --check   # report, exit 1
    cd service && uv run python -m tests.docs.test_doc_citations --write   # rewrite in place

The split is deliberate. A doc gate that reddens whenever an unrelated commit moves a
function four lines down is a gate people learn to ignore, and this one did exactly that.
"""
