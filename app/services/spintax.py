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


def _parse_sequence(s: str, i: int, stops: str) -> tuple[list[_Segment], int]:
    """Parse text into segments until an unescaped char in *stops* (or EOS).

    Returns ``(segments, index)`` where *index* points at the stop char (or the
    string end). Nested groups are parsed recursively.
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
            group, ni = _parse_group(s, i)
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


def _parse_group(s: str, i: int) -> tuple[Optional[_Group], int]:
    """Parse a ``{...}`` block starting at ``s[i] == '{'``.

    Returns ``(group, next_index)``, or ``(None, i)`` when the brace is never
    closed (so the caller can fall back to treating ``{`` as literal text).
    """
    j = i + 1
    alternatives: list[list[_Segment]] = []
    n = len(s)
    while True:
        seg, j = _parse_sequence(s, j, "|}")
        alternatives.append(seg)
        if j >= n:
            return None, i  # unterminated group
        if s[j] == "|":
            j += 1
            continue
        # s[j] == '}'
        return _Group(alternatives), j + 1


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
    segments, _ = _parse_sequence(template, 0, "")
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
    segments, _ = _parse_sequence(template, 0, "")
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
    segments, _ = _parse_sequence(template, 0, "")
    return _count_segments(segments)
