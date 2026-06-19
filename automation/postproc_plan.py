"""
Post-processing fan-out planner — the single source of truth for WHICH
post-processing variants a run produces and in WHAT order each is sequenced.

Both orchestrators (the CLI ``automation/pipeline.py`` and the GUI
``kling_gui/queue_manager.py``) and both read-only preview renderers import
``build_plan`` / ``plan_preview_line`` from here, so they can never drift.

Background
----------
The five optional post-processing steps run after Kling video generation in a
FIXED canonical order (load-bearing — rPPG must inject on raw frames, Oldcam
re-encodes last)::

    Kling -> rPPG -> Loop -> Crush -> AA -> Oldcam

Historically the orchestrators applied this as ONE cumulative chain: enabling
rPPG + Oldcam produced only ``Kling->rPPG->Oldcam``. This module generalises
that into two selectable fan-out modes:

- ``combined_only``  — reproduces the legacy behaviour: a single recipe that
  chains every enabled modifier (sub-options — crush tiers / AA attacks /
  oldcam versions — still multiply in).
- ``separate_and_combined`` (default) — the POWERSET: every non-empty subset of
  the enabled modifier *types*, each sequenced in canonical order, with the
  same sub-option multiply-in. N enabled types -> ``2**N - 1`` recipe families.

Purity
------
This module is intentionally I/O-free and side-effect-free. It enumerates an
abstract plan (``Recipe`` = an ordered subsequence of canonical steps). The
orchestrators are responsible for actually running each step's processor; the
canonical-order constraint is satisfied *by construction* because every recipe
is generated as an ordered subsequence of :data:`CANONICAL_ORDER` and always
starts from the raw Kling base.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional, Sequence, Tuple

from automation.video_aa import aa_suffix
from automation.video_crush import crush_suffix


class Modifier(str, Enum):
    """A post-processing modifier *type*. Declaration order == canonical order."""

    RPPG = "rppg"
    LOOP = "loop"
    CRUSH = "crush"
    AA = "aa"
    OLDCAM = "oldcam"


# The one true ordering. Every recipe's steps are a subsequence of this tuple.
CANONICAL_ORDER: Tuple[Modifier, ...] = (
    Modifier.RPPG,
    Modifier.LOOP,
    Modifier.CRUSH,
    Modifier.AA,
    Modifier.OLDCAM,
)

# Modifiers that have NO per-item sub-fan-out (on/off singletons). Crush, AA,
# and Oldcam each carry an option list (tier / attack / version).
_SINGLETONS: Tuple[Modifier, ...] = (Modifier.RPPG, Modifier.LOOP)

VALID_MODES: Tuple[str, ...] = ("combined_only", "separate_and_combined")
DEFAULT_MODE = "separate_and_combined"

# Human labels for the preview line (NOT used for filenames — see
# ``recipe_expected_suffixes`` for those).
_LABELS: Dict[Modifier, str] = {
    Modifier.RPPG: "rPPG",
    Modifier.LOOP: "Loop",
    Modifier.CRUSH: "Crush",
    Modifier.AA: "AA",
    Modifier.OLDCAM: "Oldcam",
}


@dataclass(frozen=True)
class Step:
    """One modifier applied with one concrete option.

    ``option`` carries the crush tier (``"720p"``), AA attack (``"prime"``), or
    oldcam version (``"v13"``); it is ``None`` for the rPPG/Loop singletons.
    """

    modifier: Modifier
    option: Optional[str] = None


@dataclass(frozen=True)
class Recipe:
    """An ordered subsequence of canonical steps to apply to the raw Kling base."""

    steps: Tuple[Step, ...]

    def modifiers(self) -> Tuple[Modifier, ...]:
        return tuple(s.modifier for s in self.steps)

    def prefix_key(self) -> Tuple[Tuple[str, Optional[str]], ...]:
        """Hashable identity of this recipe's full step chain.

        Doubles as the leading-prefix key a future memoizing executor could use
        to group recipes that share an initial subsequence (e.g. ``{rppg,
        oldcam}`` and ``{rppg, aa, oldcam}`` share the ``rppg`` prefix). The
        naive executor shipped today ignores this and re-derives each recipe
        from the raw base; the on-disk skip-mode reuse + ``is_rppg_artifact``
        guard give partial prefix-sharing for free.
        """
        return tuple((s.modifier.value, s.option) for s in self.steps)


@dataclass(frozen=True)
class PostProcPlan:
    """The full set of variants to produce for one source video."""

    recipes: Tuple[Recipe, ...]
    mode: str
    enabled_modifiers: Tuple[Modifier, ...]


def normalize_mode(mode) -> str:
    """Coerce any value to a valid fan-out mode, defaulting to the powerset."""
    token = str(mode or "").strip().lower()
    return token if token in VALID_MODES else DEFAULT_MODE


def _options_for(
    modifier: Modifier,
    *,
    rppg_enabled: bool,
    loop_enabled: bool,
    crush_resolutions: Sequence[str],
    aa_attacks: Sequence[str],
    oldcam_versions: Sequence[str],
) -> List[Optional[str]]:
    """Return the concrete option list for a modifier, or [] when disabled."""
    if modifier is Modifier.RPPG:
        return [None] if rppg_enabled else []
    if modifier is Modifier.LOOP:
        return [None] if loop_enabled else []
    if modifier is Modifier.CRUSH:
        return list(crush_resolutions or [])
    if modifier is Modifier.AA:
        return list(aa_attacks or [])
    if modifier is Modifier.OLDCAM:
        return list(oldcam_versions or [])
    return []


def build_plan(
    *,
    rppg_enabled: bool = False,
    loop_enabled: bool = False,
    crush_resolutions: Optional[Sequence[str]] = None,
    aa_attacks: Optional[Sequence[str]] = None,
    oldcam_versions: Optional[Sequence[str]] = None,
    mode: str = DEFAULT_MODE,
) -> PostProcPlan:
    """Enumerate the variant recipes for the enabled modifiers.

    Callers pass ALREADY-NORMALIZED sub-option lists (from
    ``normalize_crush_resolutions`` / ``normalize_aa_attacks`` /
    ``normalize_oldcam_versions``) so the legacy-bool migrations stay in their
    existing single-source resolvers and never get re-implemented here.

    A modifier *type* is "enabled" when its bool is True (rppg/loop) or its
    option list is non-empty (crush/aa/oldcam). Every produced ``Recipe`` is an
    ordered subsequence of :data:`CANONICAL_ORDER`.
    """
    mode = normalize_mode(mode)
    opts: Dict[Modifier, List[Optional[str]]] = {}
    for m in CANONICAL_ORDER:
        options = _options_for(
            m,
            rppg_enabled=rppg_enabled,
            loop_enabled=loop_enabled,
            crush_resolutions=crush_resolutions or [],
            aa_attacks=aa_attacks or [],
            oldcam_versions=oldcam_versions or [],
        )
        if options:
            opts[m] = options

    enabled: Tuple[Modifier, ...] = tuple(m for m in CANONICAL_ORDER if m in opts)
    if not enabled:
        return PostProcPlan(recipes=(), mode=mode, enabled_modifiers=())

    if mode == "combined_only":
        # A single recipe family using ALL enabled types (legacy behaviour).
        type_subsets: List[Tuple[Modifier, ...]] = [enabled]
    else:
        # Every non-empty subset, ordered by size then canonical position so the
        # output list is deterministic and the largest (full) chain comes last.
        type_subsets = []
        for size in range(1, len(enabled) + 1):
            for combo in itertools.combinations(enabled, size):
                type_subsets.append(combo)

    recipes: List[Recipe] = []
    seen: set = set()
    for subset in type_subsets:
        # ``combinations`` preserves input order, and ``enabled`` is already in
        # canonical order, so each subset is canonically ordered.
        option_lists = [opts[m] for m in subset]
        for product in itertools.product(*option_lists):
            steps = tuple(Step(m, product[i]) for i, m in enumerate(subset))
            recipe = Recipe(steps=steps)
            key = recipe.prefix_key()
            if key in seen:
                continue
            seen.add(key)
            recipes.append(recipe)

    return PostProcPlan(recipes=tuple(recipes), mode=mode, enabled_modifiers=enabled)


def full_chain_recipe(plan: PostProcPlan) -> Optional[Recipe]:
    """Return the recipe that uses EVERY enabled modifier (first option each).

    This is the "headline" variant for the preview line. Returns None when no
    modifiers are enabled.
    """
    if not plan.enabled_modifiers:
        return None
    target = set(plan.enabled_modifiers)
    for recipe in plan.recipes:
        if set(recipe.modifiers()) == target:
            return recipe
    return None


def step_label(step: Step) -> str:
    """``Step(CRUSH, "720p")`` -> ``"Crush(720p)"``; singletons drop the parens."""
    base = _LABELS[step.modifier]
    return f"{base}({step.option})" if step.option else base


def recipe_label(recipe: Recipe) -> str:
    """``"Kling -> rPPG -> AA(prime) -> Oldcam(v13)"`` (ASCII arrow swapped below)."""
    return " → ".join(["Kling"] + [step_label(s) for s in recipe.steps])


def plan_preview_line(plan: PostProcPlan) -> str:
    """Render the read-only one-line summary shown in the CLI table + GUI panel.

    Examples::

        Kling (no post-processing)
        Kling → rPPG → Oldcam(v13)  (combined only)
        Kling → rPPG → AA(prime) → Oldcam(v13)  ·  + 6 more variants (powerset)
    """
    if not plan.enabled_modifiers:
        return "Kling (no post-processing)"
    headline = full_chain_recipe(plan)
    chain = recipe_label(headline) if headline else "Kling"
    mode_word = "powerset" if plan.mode == "separate_and_combined" else "combined only"
    extra = len(plan.recipes) - 1
    if extra > 0:
        plural = "s" if extra != 1 else ""
        return f"{chain}  ·  + {extra} more variant{plural} ({mode_word})"
    return f"{chain}  ({mode_word})"


def recipe_expected_suffixes(recipe: Recipe) -> List[str]:
    """The ordered on-disk stem suffixes a recipe produces.

    Mirrors what each real processor appends (``-rppg``, ``_looped``,
    ``crush_suffix``, ``aa_suffix``, ``-oldcam-vN``). Used by tests to assert
    the executor's filenames match the recipe chain; the executor itself still
    composes names by calling the real processors so on-disk names can't drift
    from what actually ran.
    """
    out: List[str] = []
    for s in recipe.steps:
        if s.modifier is Modifier.RPPG:
            out.append("-rppg")
        elif s.modifier is Modifier.LOOP:
            out.append("_looped")
        elif s.modifier is Modifier.CRUSH:
            out.append(crush_suffix(s.option or ""))
        elif s.modifier is Modifier.AA:
            out.append(aa_suffix(s.option or ""))
        elif s.modifier is Modifier.OLDCAM:
            out.append(f"-oldcam-{(s.option or '').lower()}")
    return out
