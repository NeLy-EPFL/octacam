"""A TYPE_CHECKING-only name must never appear in an annotation Python evaluates.

Why this file exists: octacam declares ``requires-python >= 3.10`` but the dev rig
runs 3.14, where PEP 649 makes annotations lazy — so a bug in this class is
*invisible* there and fatal everywhere else. It shipped once: ``cli.py`` annotated
``reporter: JobReporter | None`` with ``JobReporter`` imported only under
``if TYPE_CHECKING:``, which on 3.10–3.13 is evaluated when the ``def`` executes
and raised ``NameError: name 'JobReporter' is not defined`` at import — the whole
``octacam`` CLI was unusable on every supported Python below 3.14.

A plain "does it import" test cannot catch this: on 3.14 the broken code imports
fine. So this walks the AST instead, and is therefore version-independent.

The rule, per module without ``from __future__ import annotations``: any name the
module imports only under ``TYPE_CHECKING`` may appear in an annotation only
inside a string (``"JobReporter | None"``), which is the convention the code
already follows for ``RecordConfig``/``ProgressCallback``/``Callable``.

Only annotation positions Python actually evaluates are checked:

* function/method parameter and return annotations — evaluated when the ``def``
  runs (module import for a top-level function, call time for a nested one),
* module-level and class-body variable annotations — evaluated at import.

Function-*local* variable annotations are deliberately not checked: PEP 526
leaves them unevaluated, which is why ``self._task: TaskID | None = None`` inside
a method is safe and appears throughout ``cli.py``.
"""

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "octacam"
PY_FILES = sorted(SRC.rglob("*.py"))
assert PY_FILES, f"no modules found under {SRC}"


def _has_future_annotations(tree: ast.Module) -> bool:
    return any(
        isinstance(node, ast.ImportFrom)
        and node.module == "__future__"
        and any(a.name == "annotations" for a in node.names)
        for node in tree.body
    )


def _type_checking_names(tree: ast.Module) -> set[str]:
    """Names bound only inside an ``if TYPE_CHECKING:`` block.

    Matches the bare ``TYPE_CHECKING`` and the qualified ``typing.TYPE_CHECKING``
    spellings, and takes the aliased name when the import uses ``as``.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        guard = (
            isinstance(test, ast.Name)
            and test.id == "TYPE_CHECKING"
            or isinstance(test, ast.Attribute)
            and test.attr == "TYPE_CHECKING"
        )
        if not guard:
            continue
        for sub in ast.walk(node):
            if isinstance(sub, (ast.Import, ast.ImportFrom)):
                for alias in sub.names:
                    names.add(alias.asname or alias.name.split(".")[0])
    return names


def _referenced_names(annotation: ast.expr) -> set[str]:
    """Names the interpreter would have to resolve to evaluate this annotation.

    Subtrees inside a string constant are skipped — a quoted annotation (whole or
    partial, as in ``dict[str, "JobReporter"]``) is never resolved eagerly.
    """
    if isinstance(annotation, ast.Constant):
        return set()
    found: set[str] = set()
    for child in ast.iter_child_nodes(annotation):
        found |= _referenced_names(child)
    if isinstance(annotation, ast.Name):
        found.add(annotation.id)
    elif isinstance(annotation, ast.Attribute):
        # `octacam.config.RecordConfig` — only the root name is looked up, and
        # iter_child_nodes above already collected it from the Name node.
        pass
    return found


def _eager_annotations(tree: ast.Module):
    """Yield (lineno, annotation) for every eagerly evaluated annotation."""
    # Function signatures, at any nesting depth: the annotations run when the def
    # statement itself executes.
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            for arg in (
                *args.posonlyargs,
                *args.args,
                *args.kwonlyargs,
                args.vararg,
                args.kwarg,
            ):
                if arg is not None and arg.annotation is not None:
                    yield arg.lineno, arg.annotation
            if node.returns is not None:
                yield node.lineno, node.returns

    # Variable annotations at module level and in class bodies (both evaluated at
    # import). Walked explicitly rather than via ast.walk so function-local
    # AnnAssigns — which PEP 526 never evaluates — stay excluded.
    def _bodies(body):
        for stmt in body:
            if isinstance(stmt, ast.AnnAssign) and stmt.annotation is not None:
                yield stmt.lineno, stmt.annotation
            elif isinstance(stmt, ast.ClassDef):
                yield from _bodies(stmt.body)
            elif isinstance(stmt, ast.If):  # e.g. a version-gated class attribute
                yield from _bodies(stmt.body)
                yield from _bodies(stmt.orelse)

    yield from _bodies(tree.body)


@pytest.mark.parametrize("path", PY_FILES, ids=lambda p: p.name)
def test_type_checking_names_are_quoted_in_evaluated_annotations(path):
    tree = ast.parse(path.read_text(), filename=str(path))
    if _has_future_annotations(tree):
        return  # every annotation is lazy on every version; nothing to enforce
    deferred = _type_checking_names(tree)
    if not deferred:
        return

    offenders = [
        f"{path.name}:{lineno}: {sorted(hit)} imported only under TYPE_CHECKING"
        for lineno, annotation in _eager_annotations(tree)
        if (hit := _referenced_names(annotation) & deferred)
    ]
    assert not offenders, (
        "These annotations are evaluated at runtime but name a TYPE_CHECKING-only "
        "import, which raises NameError on Python < 3.14 (PEP 649 hides it on "
        "3.14+). Quote the annotation, or add `from __future__ import "
        "annotations`:\n  " + "\n  ".join(offenders)
    )
