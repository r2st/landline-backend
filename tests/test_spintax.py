"""Tests for the spintax parser (manual copy variation).

Covers the documented edge cases: flat groups, nesting, escaping, empty
options, coexistence with merge/Jinja tokens, malformed input leniency,
determinism under a seeded RNG, variation counting, and per-send tracking.
"""

import random
import time

from app.services import spintax


def _seeded(seed: int) -> random.Random:
    return random.Random(seed)


# --- Basic resolution -------------------------------------------------------

def test_no_spintax_is_returned_verbatim():
    assert spintax.resolve("Hello there, friend.") == "Hello there, friend."


def test_empty_and_none_are_safe():
    assert spintax.resolve("") == ""
    assert spintax.resolve(None) == ""
    r = spintax.resolve_tracked(None)
    assert r.text == "" and r.choices == []


def test_flat_group_picks_one_alternative():
    template = "{alpha|beta|gamma}"
    for seed in range(20):
        assert spintax.resolve(template, rng=_seeded(seed)) in {"alpha", "beta", "gamma"}


def test_group_embedded_in_text():
    out = spintax.resolve("Hi {there|hello}, welcome", rng=_seeded(1))
    assert out.startswith("Hi ") and out.endswith(", welcome")
    assert "there" in out or "hello" in out


def test_all_alternatives_are_reachable():
    seen = set()
    for seed in range(50):
        seen.add(spintax.resolve("{a|b|c}", rng=_seeded(seed)))
    assert seen == {"a", "b", "c"}


# --- Nesting ----------------------------------------------------------------

def test_nested_groups_resolve_inside_out():
    template = "{a|{b|c}|d}"
    results = {spintax.resolve(template, rng=_seeded(s)) for s in range(60)}
    assert results <= {"a", "b", "c", "d"}
    # Over enough seeds the nested branch surfaces both inner options.
    assert "b" in results and "c" in results


def test_deeply_nested():
    template = "{1{x|y}|2{z|w}}"
    for s in range(40):
        out = spintax.resolve(template, rng=_seeded(s))
        assert out in {"1x", "1y", "2z", "2w"}


# --- Escaping ---------------------------------------------------------------

def test_escaped_braces_and_pipe_are_literal():
    # \{ \} \| render as literal characters, NOT spintax syntax.
    assert spintax.resolve(r"price is \{100\|200\}") == "price is {100|200}"


def test_escaped_backslash():
    assert spintax.resolve(r"back\\slash") == r"back\slash"


def test_escape_inside_a_group():
    # The pipe separates two alternatives; the escaped pipe is literal.
    out = spintax.resolve(r"{a\|b|c}", rng=_seeded(0))
    assert out in {"a|b", "c"}
    both = {spintax.resolve(r"{a\|b|c}", rng=_seeded(s)) for s in range(30)}
    assert both == {"a|b", "c"}


# --- Empty options ----------------------------------------------------------

def test_empty_option_can_render_nothing():
    both = {spintax.resolve("x{a||b}y", rng=_seeded(s)) for s in range(40)}
    assert both == {"xay", "xy", "xby"}


def test_group_of_all_empties():
    assert spintax.resolve("{|}", rng=_seeded(0)) == ""


def test_trailing_empty_option():
    both = {spintax.resolve("{a|}", rng=_seeded(s)) for s in range(30)}
    assert both == {"a", ""}


# --- Coexistence with merge fields / Jinja ----------------------------------

def test_single_brace_without_pipe_is_left_literal():
    # A merge token like {first_name} has no top-level pipe → not spintax.
    assert spintax.resolve("Hi {first_name}") == "Hi {first_name}"


def test_jinja_double_braces_preserved():
    # Jinja merge fields must survive untouched for later rendering.
    assert spintax.resolve("Hi {{ first_name }}") == "Hi {{ first_name }}"


def test_spintax_nested_in_literal_braces_still_resolves():
    both = {spintax.resolve("{note: {a|b}}", rng=_seeded(s)) for s in range(30)}
    assert both == {"{note: a}", "{note: b}"}


def test_merge_field_inside_spintax_option_is_preserved():
    both = {
        spintax.resolve("{Hi {{name}}|Hello {{name}}}", rng=_seeded(s))
        for s in range(30)
    }
    assert both == {"Hi {{name}}", "Hello {{name}}"}


# --- Malformed / leniency ---------------------------------------------------

def test_unbalanced_open_brace_is_literal():
    # An unterminated group must not raise — the brace is treated as literal.
    assert spintax.resolve("hello {world|there") == "hello {world|there"


def test_stray_close_brace_is_literal():
    assert spintax.resolve("a}b") == "a}b"


def test_unbalanced_does_not_swallow_valid_later_group():
    both = {spintax.resolve("{ok {a|b}", rng=_seeded(s)) for s in range(30)}
    assert both == {"{ok a", "{ok b"}


# --- has_spintax / count_variations -----------------------------------------

def test_has_spintax_detection():
    assert spintax.has_spintax("{a|b}") is True
    assert spintax.has_spintax("{first_name}") is False
    assert spintax.has_spintax("plain text") is False
    assert spintax.has_spintax("{outer {a|b}}") is True  # nested spintax counts
    assert spintax.has_spintax("") is False
    assert spintax.has_spintax(None) is False


def test_count_variations():
    assert spintax.count_variations("plain") == 1
    assert spintax.count_variations("{a|b|c}") == 3
    assert spintax.count_variations("{a|b}{c|d}") == 4  # independent groups multiply
    assert spintax.count_variations("{a|{b|c}|d}") == 4  # nested sums into parent
    assert spintax.count_variations("{first_name}") == 1  # not spintax


# --- Tracking ---------------------------------------------------------------

def test_tracked_records_choices_and_signature():
    r = spintax.resolve_tracked("{a|b|c} and {x|y}", rng=_seeded(7))
    assert len(r.choices) == 2
    assert r.has_choices is True
    # Signature is the dash-joined chosen indices.
    assert r.signature == "-".join(str(c["index"]) for c in r.choices)
    assert all(0 <= c["index"] < c["options"] for c in r.choices)


def test_tracked_no_spintax_has_empty_signature():
    r = spintax.resolve_tracked("no variation here {name}")
    assert r.choices == []
    assert r.signature == ""
    assert r.has_choices is False


def test_signature_is_stable_for_same_seed():
    a = spintax.resolve_tracked("{a|b|c}{d|e}", rng=_seeded(42))
    b = spintax.resolve_tracked("{a|b|c}{d|e}", rng=_seeded(42))
    assert a.text == b.text
    assert a.signature == b.signature


def test_nested_choices_are_recorded_top_down():
    # Force selection of the nested branch and assert both choices recorded.
    template = "{a|{b|c}}"
    got_nested = False
    for s in range(40):
        r = spintax.resolve_tracked(template, rng=_seeded(s))
        if r.text in {"b", "c"}:
            got_nested = True
            # Outer chose index 1, then the inner group was resolved → 2 choices.
            assert len(r.choices) == 2
            assert r.choices[0]["index"] == 1
    assert got_nested


# ---------------------------------------------------------------------------
# Pathological input: the parser is lenient, and leniency has to be cheap
# ---------------------------------------------------------------------------
# The unterminated-brace fallback used to *discover* that a `{` never closed by
# parsing the rest of the string, recursively, once per nested brace — so `k`
# unmatched braces cost `2**k`. This runs on the send path, on post-Jinja copy,
# so the braces are whatever an author typed plus whatever merge fields and LLM
# drafts interpolated. 24 of them took ten seconds of a Celery worker.


def test_many_unmatched_braces_parse_in_linear_time():
    # 60 would have been 2**60 parses. The wall-clock assertion is deliberately
    # loose — what it is pinning is the complexity class, not a benchmark.
    template = "{" * 60 + "hello"
    start = time.monotonic()
    assert spintax.resolve(template) == template
    assert time.monotonic() - start < 1.0


def test_unmatched_braces_still_render_verbatim():
    # The leniency itself is unchanged: an unbalanced `{` is literal text.
    assert spintax.resolve("{a|b") == "{a|b"
    # The outer brace never closes and is literal; the inner one is a real group.
    assert spintax.resolve("{{a|a}") == "{a"
    assert spintax.resolve("closing } alone") == "closing } alone"


def test_a_matched_group_after_unmatched_braces_still_resolves():
    # The brace-match prepass must not make a real group invisible just because
    # unusable braces precede it.
    assert spintax.resolve("{{{ {only|only}") == "{{{ only"


def test_escaped_braces_do_not_open_or_close_a_group():
    # The prepass honours the same escapes the parser does; if it didn't, `\{`
    # would be counted as matched by a later real `}` and the two would disagree.
    assert spintax.resolve(r"\{a|b\}") == "{a|b}"
    assert spintax.resolve(r"literal \} then {x|x}") == "literal } then x"


def test_deeply_nested_groups_do_not_raise():
    # Both parsing and rendering recurse per level, so this raised RecursionError
    # — an exception out of a module whose contract is that malformed copy never
    # blows up a send.
    template = "{a|" * 5000 + "x" + "}" * 5000
    result = spintax.resolve(template)
    assert isinstance(result, str)


def test_nesting_within_the_depth_limit_still_resolves_normally():
    depth = spintax.MAX_NESTING_DEPTH - 1
    template = "{z|" * depth + "x" + "}" * depth
    # Every alternative is either the literal "z" branch or bottoms out at "x".
    assert spintax.resolve(template, rng=_seeded(0)) in {"z", "x"}


def test_count_variations_and_has_spintax_survive_pathological_input():
    # Both walk the same parser, so both had the same blowup.
    template = "{" * 60 + "{a|b}"
    start = time.monotonic()
    assert spintax.count_variations(template) == 2
    assert spintax.has_spintax(template) is True
    assert time.monotonic() - start < 1.0
