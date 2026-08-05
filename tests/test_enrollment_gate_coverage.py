"""Every enrollment writer asks the do-not-contact question, checked mechanically.

The behavioural half is ``tests/test_enrollment_do_not_contact_gate.py``. This is
the half that catches the *seventh* writer, and it exists for the same reason
``test_send_gate_coverage`` does: there is nothing to see at the call site. An
``OutreachEnrollment(...)`` that consults the suppression list and one that does
not are character-for-character identical; what separates them is a check
somewhere above, in the same function, that a reviewer has to go looking for.

Six writers existed and only one of them asked all three questions. The other
five did not look wrong — each looked like a slightly shorter version of a
neighbour, and the clause they dropped (the org do-not-contact LIST) is the one
that cannot be inferred from the contact row, because ``suppression.add_entry``
deliberately does not touch ``Contact.suppressed``.

**Function-local, deliberately.** "Some caller checks first" is precisely the
assumption that was wrong to begin with: ``social_listening_agent.auto_enroll_
lead`` documented itself as mirroring the prospector's checks and mirrored none
of them, and no reviewer reading either function alone would have caught it.

A writer that genuinely should not ask — a re-enrollment of a row that was
already proved mailable, a test fixture factory — belongs in ``REVIEWED`` with
the reason, not in a silently-widened matcher.
"""

import ast
import pathlib

APP = pathlib.Path(__file__).resolve().parent.parent / "app"

# Names that constitute asking the question. ``blocking_reason`` /
# ``blocking_flag`` / ``is_mailable`` are the shared predicate;
# ``suppressed_addresses`` and ``is_suppressed`` are the list query the batch
# paths use directly; ``Contact.suppressed`` is the prospector's SQL-level
# filter, which asks it in the WHERE clause instead.
_GATE_NAMES = frozenset({
    "blocking_reason",
    "blocking_flag",
    "is_mailable",
    "is_suppressed",
    "suppressed_addresses",
    "_emailable",
    "_emailable_subset",
})

# Enrollment writes that legitimately ask nothing, each with the reason.
REVIEWED: dict[tuple[str, str], str] = {}


def _iter_functions(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def _constructs_enrollment(func: ast.AST) -> bool:
    for node in ast.walk(func):
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id == "OutreachEnrollment":
                return True
            if isinstance(fn, ast.Attribute) and fn.attr == "OutreachEnrollment":
                return True
    return False


def _asks_the_question(func: ast.AST) -> bool:
    """Whether this function consults any do-not-contact source before writing."""
    for node in ast.walk(func):
        # A call to one of the gate helpers, however it is spelled.
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", None)
            if name in _GATE_NAMES:
                return True
        # The prospector's shape: the filter is in the SELECT, as
        # ``Contact.suppressed.is_(False)`` / ``Contact.unsubscribed...``.
        if isinstance(node, ast.Attribute) and node.attr in ("suppressed", "unsubscribed"):
            value = node.value
            if isinstance(value, ast.Name) and value.id == "Contact":
                return True
    return False


def _writers() -> list[tuple[str, str, ast.AST]]:
    found = []
    for path in sorted(APP.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        rel = str(path.relative_to(APP.parent))
        for func in _iter_functions(tree):
            if _constructs_enrollment(func):
                found.append((rel, func.name, func))
    return found


def test_every_enrollment_writer_asks_the_do_not_contact_question():
    unguarded = [
        f"{rel}::{name}"
        for rel, name, func in _writers()
        if (rel, name) not in REVIEWED and not _asks_the_question(func)
    ]
    assert not unguarded, (
        "these functions create an OutreachEnrollment without consulting any "
        "do-not-contact source. Call suppression.blocking_reason (or filter in "
        "SQL, as autonomous_prospector does), or add an entry to REVIEWED with "
        "the reason: " + ", ".join(unguarded)
    )


def test_the_scan_actually_finds_the_writers():
    """A checker matching nothing passes forever and reads as coverage."""
    found = {f"{rel}::{name}" for rel, name, _ in _writers()}
    for expected in (
        "app/routers/outreach.py::enroll_contact",
        "app/routers/public_api.py::create_enrollment",
        "app/services/social_listening_agent.py::auto_enroll_lead",
        "app/services/autonomous_prospector.py::_auto_enroll",
    ):
        assert expected in found, f"{expected} not seen by the scan (found: {sorted(found)})"


# --- Self-tests: plant each shape and assert the scan catches it -------------


def _scan_source(src: str) -> bool:
    """``True`` if the planted function would pass the check."""
    tree = ast.parse(src)
    func = next(_iter_functions(tree))
    assert _constructs_enrollment(func), "the plant must actually create one"
    return _asks_the_question(func)


def test_self_an_unguarded_write_is_caught():
    assert not _scan_source(
        "async def enrol(db, contact, seq):\n"
        "    db.add(OutreachEnrollment(contact_id=contact.id, sequence_id=seq.id))\n"
    )


def test_self_a_flags_only_write_is_still_caught_if_it_uses_no_helper():
    """Reading the two flags by hand is what five of the six did.

    ``getattr(contact, "suppressed", False)`` is not ``Contact.suppressed`` —
    it is the contact row, which cannot answer the list clause. The scan does
    not accept it, which is the point: the fix is to call the shared predicate.
    """
    assert not _scan_source(
        "async def enrol(db, contact, seq):\n"
        "    if getattr(contact, 'suppressed', False):\n"
        "        return\n"
        "    db.add(OutreachEnrollment(contact_id=contact.id, sequence_id=seq.id))\n"
    )


def test_self_the_shared_predicate_passes():
    assert _scan_source(
        "async def enrol(db, contact, seq):\n"
        "    if await _suppression.blocking_reason(db, contact) is not None:\n"
        "        return\n"
        "    db.add(OutreachEnrollment(contact_id=contact.id, sequence_id=seq.id))\n"
    )


def test_self_the_sql_filter_shape_passes():
    assert _scan_source(
        "async def enrol(db, seq):\n"
        "    rows = await db.execute(\n"
        "        select(Contact).where(Contact.suppressed.is_(False))\n"
        "    )\n"
        "    for c in rows.scalars():\n"
        "        db.add(OutreachEnrollment(contact_id=c.id, sequence_id=seq.id))\n"
    )


def test_self_the_batch_list_query_passes():
    assert _scan_source(
        "async def enrol(db, contacts, seq):\n"
        "    listed = await _suppression.suppressed_addresses(db, [c.email for c in contacts])\n"
        "    for c in contacts:\n"
        "        db.add(OutreachEnrollment(contact_id=c.id, sequence_id=seq.id))\n"
    )
