"""Spintax — manual copy variation for outreach templates.

Spintax lets a human author bake several phrasings into one template and have a
*different* one picked for each send, so a batch of otherwise-identical emails
doesn't look machine-stamped to a spam filter (or a recipient). It's the manual
counterpart to AI generation: no model call, full author control.

Syntax::

    Hi {first_name}, {I wanted to reach out|I'd love to connect|Just reaching out}

A **spintax group** is a ``{...}`` block whose alternatives are separated by an
unescaped, top-level ``|``. One alternative is chosen at random per render.

Rules the parser honours:

* **Nesting** — ``{a|{b|c}|d}`` resolves inside-out; a nested group is picked
  only when its containing alternative is picked.
* **Escaping** — ``\\{``, ``\\}``, ``\\|`` and ``\\\\`` render as the literal
  character, so real braces/pipes survive.
* **Empty options** — ``{a||b}`` has three alternatives, the middle one empty,
  so it can render to nothing.
* **Coexistence with merge fields** — a ``{...}`` block with *no* top-level
  ``|`` is **not** a spintax group: its braces are left verbatim. That keeps
  single-brace merge tokens (``{first_name}``) and Jinja (``{{ }}`` / ``{% %}``)
  untouched, while still resolving any spintax nested *inside* such a block
  (e.g. ``{{ x }}`` is preserved, ``{note: {a|b}}`` becomes ``{note: a}``).

The parser is deliberately lenient — an unbalanced ``{`` is treated as a literal
character rather than raising, because malformed copy must never blow up a send.

Leniency is not the same as cheapness
-------------------------------------
That fallback used to cost exponential time. ``_parse_group`` discovered an
unterminated ``{`` by scanning to the end of the string, returned ``None``, and
the caller then advanced one character and parsed the remainder again — including
every nested ``{`` inside it, each of which repeated the same doomed scan. So
``k`` unmatched braces cost ``2**k``: 20 of them took 0.6s, 24 took 10s, and 30
would have wedged a Celery worker for the better part of an hour.

Nothing exotic is needed to reach that. This runs on the send path, on copy that
has *already been through Jinja*, so the braces are a mix of what an author typed
and what merge fields interpolated — a company name, a research snippet, an
LLM-drafted line that opened a JSON-ish brace and never closed it.
:func:`_brace_matches` now answers "does this ``{`` close?" once, in a single
linear scan, so an unmatched brace is literal text at no cost.

The other half is depth. Both parsing and rendering recurse per nesting level, so
a few hundred *balanced* nested groups raised ``RecursionError`` — which is an
exception, so "malformed copy never blows up a send" was untrue in that direction
too. :data:`MAX_NESTING_DEPTH` bounds it: past that depth a ``{`` is literal, the
same answer the parser already gives for a brace it cannot use.

:func:`resolve_tracked` returns both the rendered string and the sequence of
choices made, so a send path can persist *which* variant went out and attribute
downstream engagement to it.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Optional, Union

# Characters that carry meaning and can be backslash-escaped to a literal.
_ESCAPABLE = "{}|\\"

# How deep ``{`` blocks may nest before a ``{`` is read as a literal character.
# Parsing and rendering both recurse once per level, so this is what keeps a
# pathological template from raising ``RecursionError`` out of a send. Real
# spintax nests two or three deep; ``{a|{b|c}|d}`` in the module docstring is the
# realistic maximum, and 32 leaves an order of magnitude of headroom over it.
MAX_NESTING_DEPTH = 32


def _brace_matches(s: str) -> set[int]:
    """Indices of every ``{`` in *s* that has a matching unescaped ``}``.

    One linear pass with a stack, honouring the same backslash escapes the parser
    does, so ``\\{`` opens nothing and ``\\}`` closes nothing.

    This exists to make the unterminated-brace case free. The parser's fallback
    for a ``{`` that never closes is to treat it as literal text, and it used to
    *discover* that by parsing the rest of the string — recursively, once per
    nested brace, so the cost doubled with every extra unmatched ``{``. Asking
    the question once, up front, turns that into a lookup.
    """
    stack: list[int] = []
    matched: set[int] = set()
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c == "\\" and i + 1 < n and s[i + 1] in _ESCAPABLE:
            i += 2
            continue
        if c == "{":
            stack.append(i)
        elif c == "}" and stack:
            matched.add(stack.pop())
        i += 1
    return matched


class _Group:
    """A parsed ``{...}`` block: a list of alternatives, each a segment list."""

    __slots__ = ("alternatives",)

    def __init__(self, alternatives: list[list]) -> None:
        self.alternatives = alternatives

    @property
    def is_spintax(self) -> bool:
        # A block is only a spintax choice if it actually offered alternatives
        # (i.e. contained a top-level ``|``). One alternative = literal braces.
        return len(self.alternatives) > 1


# A segment is either literal text (str) or a nested _Group.
_Segment = Union[str, _Group]


def _parse_sequence(
    s: str, i: int, stops: str, matches: set[int], depth: int = 0
) -> tuple[list[_Segment], int]:
    """Parse text into segments until an unescaped char in *stops* (or EOS).

    Returns ``(segments, index)`` where *index* points at the stop char (or the
    string end). Nested groups are parsed recursively.

    *matches* is :func:`_brace_matches` for the whole of *s*; *depth* is the
    current nesting level. Together they are what bound the work: a ``{`` that is
    unmatched, or already :data:`MAX_NESTING_DEPTH` levels down, becomes a
    literal without recursing.
    """
    segments: list[_Segment] = []
    buf: list[str] = []
    n = len(s)
    while i < n:
        c = s[i]
        if c == "\\" and i + 1 < n and s[i + 1] in _ESCAPABLE:
            buf.append(s[i + 1])
            i += 2
            continue
        if c in stops:
            break
        if c == "{":
            group, ni = (
                _parse_group(s, i, matches, depth)
                if i in matches and depth < MAX_NESTING_DEPTH
                # Unbalanced, or nested past what we're willing to recurse
                # through. Either way there is no group here.
                else (None, i)
            )
            if group is None:
                # Unbalanced brace — treat it as a literal character.
                buf.append("{")
                i += 1
                continue
            if buf:
                segments.append("".join(buf))
                buf = []
            segments.append(group)
            i = ni
            continue
        buf.append(c)
        i += 1
    if buf:
        segments.append("".join(buf))
    return segments, i


def _parse_group(
    s: str, i: int, matches: set[int], depth: int = 0
) -> tuple[Optional[_Group], int]:
    """Parse a ``{...}`` block starting at ``s[i] == '{'``.

    Returns ``(group, next_index)``, or ``(None, i)`` when the brace is never
    closed (so the caller can fall back to treating ``{`` as literal text).

    Only called for an ``i`` known to be in *matches*, so the unterminated return
    is now unreachable in practice; it is kept because it is the contract the
    caller's fallback is written against, not an assertion about the caller.
    """
    j = i + 1
    alternatives: list[list[_Segment]] = []
    n = len(s)
    while True:
        seg, j = _parse_sequence(s, j, "|}", matches, depth + 1)
        alternatives.append(seg)
        if j >= n:
            return None, i  # unterminated group
        if s[j] == "|":
            j += 1
            continue
        # s[j] == '}'
        return _Group(alternatives), j + 1


def _parse(template: str) -> list[_Segment]:
    """Parse a whole template into segments. The one entry point to the parser."""
    segments, _ = _parse_sequence(template, 0, "", _brace_matches(template))
    return segments


def _render_segments(segments: list[_Segment], rng, choices: list[dict]) -> str:
    out: list[str] = []
    for seg in segments:
        if isinstance(seg, str):
            out.append(seg)
        else:
            out.append(_render_group(seg, rng, choices))
    return "".join(out)


def _render_group(group: _Group, rng, choices: list[dict]) -> str:
    if not group.is_spintax:
        # Not a choice — keep the literal braces, but resolve any spintax nested
        # inside (the single alternative). Preserves merge/Jinja tokens.
        inner = _render_segments(group.alternatives[0], rng, choices) if group.alternatives else ""
        return "{" + inner + "}"
    idx = rng.randrange(len(group.alternatives))
    # Record the choice top-down (before rendering the chosen branch) so nested
    # choices append after their parent — a stable, readable order.
    choices.append({"options": len(group.alternatives), "index": idx})
    return _render_segments(group.alternatives[idx], rng, choices)


def _count_segments(segments: list[_Segment]) -> int:
    total = 1
    for seg in segments:
        if isinstance(seg, str):
            continue
        total *= _count_group(seg)
    return total


def _count_group(group: _Group) -> int:
    if not group.is_spintax:
        return _count_segments(group.alternatives[0]) if group.alternatives else 1
    return sum(_count_segments(alt) for alt in group.alternatives)


@dataclass
class SpintaxResult:
    """The rendered text plus the choices made to produce it."""

    text: str
    choices: list[dict] = field(default_factory=list)

    @property
    def has_choices(self) -> bool:
        return bool(self.choices)

    @property
    def signature(self) -> str:
        """Compact, stable key of the chosen indices (e.g. ``"0-2-1"``).

        Empty when no spintax group was present. Two renders that took the same
        branches share a signature, which makes it a natural analytics bucket.
        """
        return "-".join(str(c["index"]) for c in self.choices)


def resolve_tracked(template: Optional[str], *, rng: Optional[random.Random] = None) -> SpintaxResult:
    """Resolve every spintax group in *template*, returning text + choices.

    Pass an explicit *rng* (a seeded :class:`random.Random`) for deterministic
    output; otherwise the module-global RNG is used.
    """
    if not template:
        return SpintaxResult(text=template or "", choices=[])
    generator = rng if rng is not None else random
    segments = _parse(template)
    choices: list[dict] = []
    text = _render_segments(segments, generator, choices)
    return SpintaxResult(text=text, choices=choices)


def resolve(template: Optional[str], *, rng: Optional[random.Random] = None) -> str:
    """Resolve spintax and return just the rendered string."""
    return resolve_tracked(template, rng=rng).text


def has_spintax(template: Optional[str]) -> bool:
    """True if *template* contains at least one resolvable spintax group."""
    if not template:
        return False
    segments = _parse(template)
    return _has_spintax_segments(segments)


def _has_spintax_segments(segments: list[_Segment]) -> bool:
    for seg in segments:
        if isinstance(seg, _Group):
            if seg.is_spintax:
                return True
            if seg.alternatives and _has_spintax_segments(seg.alternatives[0]):
                return True
    return False


def count_variations(template: Optional[str]) -> int:
    """Number of distinct strings *template* can render to.

    ``1`` for a template with no spintax. Multiplies independent groups and
    sums a group's alternatives, so nesting is counted exactly.
    """
    if not template:
        return 1
    segments = _parse(template)
    return _count_segments(segments)
