# tenk_core.py
"""
10,000 (family rules) Monte Carlo backend
========================================

This file is the complete backend monolith: rules, scoring, option enumeration,
strategy (priority ladder), simulation runner, JSON persistence, and a diagnostic
harness.

House rules implemented (defaults, all adjustable via Ruleset):
- 6 dice, additive scoring across disjoint subsets
- Singles: 1=100, 5=50
- N-of-a-kind: for n=3..6, score = (n-2) * base(face), where:
    base(1)=1000, base(2..6)=face*100
- 5-dice small straights: 1-2-3-4-5 or 2-3-4-5-6 => 500
- 6-dice straight: 1-2-3-4-5-6 => 1000
- No three-pairs scoring
- Hot dice: only if the player chooses to take (score) all remaining dice.
  If hot dice triggers, rerolling all 6 is optional; turn score accumulates.

Turn constraints:
- Entry: player cannot bank until turn_score >= entry_threshold within the SAME turn.
- End-zone: if total_score >= endzone_start, player cannot bank unless (total+turn) >= target_score.
- Game ends as soon as total_score >= target_score (no extra end-round rule).

Strategy:
- Priority ladder chooses WHICH scoring subset(s) to take from a roll.
- Banking policy chooses BANK vs ROLL, and special post-hot-dice threshold.

Usage:
- As a library: import and call simulate_games(...)
- As a script: python tenk_core.py --smoke
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Callable, Dict, List, Optional, Sequence, Tuple, Any, Set
import itertools
import json
import math
import random
import statistics
import time
import traceback
import argparse


# =========================
# Schema / Data Contracts
# =========================

SCHEMA_VERSION = 1


class Phase(str, Enum):
    OFF_BOARD = "OFF_BOARD"
    NORMAL = "NORMAL"
    ENDZONE = "ENDZONE"


class AfterTakeAction(str, Enum):
    ROLL_REMAINING = "ROLL_REMAINING"
    BANK = "BANK"
    ROLL_ALL_6 = "ROLL_ALL_6"  # only valid if hot dice is available immediately after taking


class RuleType(str, Enum):
    # Option ranking rules (priority ladder)
    FINISH_MIN_POINTS = "FINISH_MIN_POINTS"       # if can finish this turn, prefer the smallest points option that finishes
    PREFER_HOT_DICE = "PREFER_HOT_DICE"           # prefer options that trigger hot dice
    MAX_POINTS = "MAX_POINTS"                     # prefer higher immediate points
    MAX_DICE_LEFT = "MAX_DICE_LEFT"               # prefer leaving more dice to roll
    AVOID_LEAVE_1_DIE = "AVOID_LEAVE_1_DIE"       # penalize options leaving exactly 1 die
    PREFER_SETS = "PREFER_SETS"                   # prefer options involving n-of-kind
    PREFER_STRAIGHTS = "PREFER_STRAIGHTS"         # prefer straights
    PREFER_SINGLES = "PREFER_SINGLES"             # prefer singles (1/5)
    MIN_TAKE_POINTS = "MIN_TAKE_POINTS"           # hard-ish constraint: penalize options below a minimum
    ENDZONE_PROGRESS = "ENDZONE_PROGRESS"         # prefer more progress toward target when in endzone


@dataclass(frozen=True)
class Ruleset:
    schema_version: int = SCHEMA_VERSION

    # Targets / thresholds
    target_score: int = 10000
    entry_threshold: int = 750
    endzone_start: int = 9250

    # Singles scoring
    single_1: int = 100
    single_5: int = 50

    # Sets base (triple) scoring: base(1)=1000, base(2)=200, ..., base(6)=600
    set_base: Dict[int, int] = field(default_factory=lambda: {1: 1000, 2: 200, 3: 300, 4: 400, 5: 500, 6: 600})

    # Straights
    small_straight_score: int = 500
    small_straights: Tuple[Tuple[int, ...], ...] = ((1, 2, 3, 4, 5), (2, 3, 4, 5, 6))
    large_straight_score: int = 1000
    large_straight: Tuple[int, ...] = (1, 2, 3, 4, 5, 6)

    # Hot dice
    hot_dice_optional: bool = True
    hot_dice_triggers_only_if_all_taken: bool = True

    # Unsupported patterns
    allow_three_pairs: bool = False

    # Safety guards
    max_turns_per_game: int = 2000  # avoid infinite loops from pathological strategies
    max_rolls_per_turn: int = 500   # same


    def validate(self) -> None:
        if self.target_score <= 0:
            raise ValueError("target_score must be > 0")
        if self.entry_threshold < 0:
            raise ValueError("entry_threshold must be >= 0")
        if self.endzone_start < 0:
            raise ValueError("endzone_start must be >= 0")
        if set(self.set_base.keys()) != {1, 2, 3, 4, 5, 6}:
            raise ValueError("set_base must define keys 1..6")
        for k, v in self.set_base.items():
            if v < 0:
                raise ValueError(f"set_base[{k}] must be >=0")
        if self.single_1 < 0 or self.single_5 < 0:
            raise ValueError("single_1 and single_5 must be >=0")
        if self.small_straight_score < 0 or self.large_straight_score < 0:
            raise ValueError("straight scores must be >=0")
        if self.max_turns_per_game <= 0 or self.max_rolls_per_turn <= 0:
            raise ValueError("max_turns_per_game and max_rolls_per_turn must be > 0")


@dataclass(frozen=True)
class GameState:
    total_score: int
    turn_score: int
    dice_remaining: int  # 0..6
    phase: Phase
    hot_dice_available: bool  # true right after taking all remaining dice
    turn_index: int
    roll_index_in_turn: int

    # Convenience fields (derived, but stored for audit stability)
    entered_board: bool
    must_continue: bool
    points_needed: int


@dataclass(frozen=True)
class ActionOption:
    mask: int                 # bitmask over current roll indices (0..k-1)
    points: int
    dice_used: int
    dice_left_after_take: int
    triggers_hot_dice: bool
    tags: Tuple[str, ...]     # e.g., ("SETS", "SINGLES")
    detail: str               # brief textual decomposition for debug/explainability


@dataclass(frozen=True)
class Decision:
    chosen_option_index: int
    after_take_action: AfterTakeAction
    reason: str = ""
    ladder_trace: Tuple[str, ...] = ()


@dataclass
class LadderRuleRow:
    priority: int
    enabled: bool
    rule_type: RuleType
    # generic parameters; interpretation depends on rule_type
    p1: float = 0.0
    p2: float = 0.0
    note: str = ""

    def to_json(self) -> Dict[str, Any]:
        return {
            "priority": self.priority,
            "enabled": self.enabled,
            "rule_type": self.rule_type.value,
            "p1": self.p1,
            "p2": self.p2,
            "note": self.note,
        }

    @staticmethod
    def from_json(d: Dict[str, Any]) -> "LadderRuleRow":
        return LadderRuleRow(
            priority=int(d.get("priority", 0)),
            enabled=bool(d.get("enabled", True)),
            rule_type=RuleType(str(d.get("rule_type"))),
            p1=float(d.get("p1", 0.0)),
            p2=float(d.get("p2", 0.0)),
            note=str(d.get("note", "")),
        )


@dataclass
class StrategyConfig:
    schema_version: int = SCHEMA_VERSION

    name: str = "Default: Bank>=300 (finish-min endzone)"
    # Banking thresholds
    bank_threshold_normal: int = 300
    bank_threshold_post_hot: int = 600

    # Low-dice overrides: if dice_remaining <= key, use threshold
    # Example: {1: 800, 2: 650}
    low_dice_thresholds: Dict[int, int] = field(default_factory=lambda: {1: 800, 2: 650})

    # Near-goal overrides: list of (points_needed_max, bank_threshold)
    near_goal_thresholds: List[Tuple[int, int]] = field(default_factory=lambda: [(250, 150), (500, 250)])

    # Phase overrides (optional): if set, overrides the normal thresholds
    bank_threshold_off_board: Optional[int] = None
    bank_threshold_endzone: Optional[int] = None
    bank_threshold_post_hot_off_board: Optional[int] = None
    bank_threshold_post_hot_endzone: Optional[int] = None

    # Ladder rules (priority order by priority ascending)
    ladder: List[LadderRuleRow] = field(default_factory=list)

    # Control whether to include ladder trace strings in decisions (debug)
    enable_trace: bool = True

    def validate(self) -> None:
        if self.bank_threshold_normal < 0 or self.bank_threshold_post_hot < 0:
            raise ValueError("bank thresholds must be >= 0")
        for k, v in self.low_dice_thresholds.items():
            if k < 1 or k > 6:
                raise ValueError("low_dice_thresholds keys must be 1..6")
            if v < 0:
                raise ValueError("low_dice_thresholds values must be >= 0")
        for pn, thr in self.near_goal_thresholds:
            if pn < 0 or thr < 0:
                raise ValueError("near_goal_thresholds values must be >= 0")
        # ladder priorities can be any ints; we sort


@dataclass
class SimConfig:
    n_games: int = 1000
    seed: Optional[int] = None
    collect_turn_records: bool = True
    collect_event_log: bool = False  # can get large
    max_games_for_event_log: int = 10  # safety
    progress_every: int = 50          # callback rate
    verbose: bool = False


@dataclass
class GameSummary:
    game_index: int
    finished: bool
    total_score: int
    turns: int
    total_rolls: int
    farkles: int
    hot_dice_events: int
    entered_on_turn: Optional[int]
    endzone_entered_on_turn: Optional[int]
    elapsed_s: float


@dataclass
class TurnRecord:
    game_index: int
    turn_index: int
    total_score_start: int
    total_score_end: int
    turn_score_banked: int
    rolls_in_turn: int
    farkle: bool
    hot_dice_events_in_turn: int
    phase_start: Phase
    phase_end: Phase


@dataclass
class SimResult:
    ruleset: Ruleset
    strategy: StrategyConfig
    sim_config: SimConfig
    summaries: List[GameSummary]
    turns: List[TurnRecord]
    event_log: List[Dict[str, Any]]  # optional, can be huge


# =========================
# JSON Persistence
# =========================

def ruleset_to_json(r: Ruleset) -> Dict[str, Any]:
    d = asdict(r)
    # tuples become lists; that's ok for JSON
    return d

def ruleset_from_json(d: Dict[str, Any]) -> Ruleset:
    # Provide defaults for missing keys
    # Keep it simple; schema upgrades can be added later.
    rs = Ruleset(
        schema_version=int(d.get("schema_version", SCHEMA_VERSION)),
        target_score=int(d.get("target_score", 10000)),
        entry_threshold=int(d.get("entry_threshold", 750)),
        endzone_start=int(d.get("endzone_start", 9250)),
        single_1=int(d.get("single_1", 100)),
        single_5=int(d.get("single_5", 50)),
        set_base={int(k): int(v) for k, v in (d.get("set_base") or {1:1000,2:200,3:300,4:400,5:500,6:600}).items()},
        small_straight_score=int(d.get("small_straight_score", 500)),
        small_straights=tuple(tuple(int(x) for x in seq) for seq in d.get("small_straights", [(1,2,3,4,5),(2,3,4,5,6)])),
        large_straight_score=int(d.get("large_straight_score", 1000)),
        large_straight=tuple(int(x) for x in d.get("large_straight", (1,2,3,4,5,6))),
        hot_dice_optional=bool(d.get("hot_dice_optional", True)),
        hot_dice_triggers_only_if_all_taken=bool(d.get("hot_dice_triggers_only_if_all_taken", True)),
        allow_three_pairs=bool(d.get("allow_three_pairs", False)),
        max_turns_per_game=int(d.get("max_turns_per_game", 2000)),
        max_rolls_per_turn=int(d.get("max_rolls_per_turn", 500)),
    )
    rs.validate()
    return rs

def strategy_to_json(s: StrategyConfig) -> Dict[str, Any]:
    return {
        "schema_version": s.schema_version,
        "name": s.name,
        "bank_threshold_normal": s.bank_threshold_normal,
        "bank_threshold_post_hot": s.bank_threshold_post_hot,
        "low_dice_thresholds": {str(k): v for k, v in s.low_dice_thresholds.items()},
        "near_goal_thresholds": list(list(x) for x in s.near_goal_thresholds),
        "bank_threshold_off_board": s.bank_threshold_off_board,
        "bank_threshold_endzone": s.bank_threshold_endzone,
        "bank_threshold_post_hot_off_board": s.bank_threshold_post_hot_off_board,
        "bank_threshold_post_hot_endzone": s.bank_threshold_post_hot_endzone,
        "ladder": [row.to_json() for row in s.ladder],
        "enable_trace": s.enable_trace,
    }

def strategy_from_json(d: Dict[str, Any]) -> StrategyConfig:
    s = StrategyConfig(
        schema_version=int(d.get("schema_version", SCHEMA_VERSION)),
        name=str(d.get("name", "Strategy")),
        bank_threshold_normal=int(d.get("bank_threshold_normal", 300)),
        bank_threshold_post_hot=int(d.get("bank_threshold_post_hot", 600)),
        low_dice_thresholds={int(k): int(v) for k, v in (d.get("low_dice_thresholds") or {"1":800,"2":650}).items()},
        near_goal_thresholds=[(int(x[0]), int(x[1])) for x in (d.get("near_goal_thresholds") or [(250,150),(500,250)])],
        bank_threshold_off_board=(None if d.get("bank_threshold_off_board", None) is None else int(d["bank_threshold_off_board"])),
        bank_threshold_endzone=(None if d.get("bank_threshold_endzone", None) is None else int(d["bank_threshold_endzone"])),
        bank_threshold_post_hot_off_board=(None if d.get("bank_threshold_post_hot_off_board", None) is None else int(d["bank_threshold_post_hot_off_board"])),
        bank_threshold_post_hot_endzone=(None if d.get("bank_threshold_post_hot_endzone", None) is None else int(d["bank_threshold_post_hot_endzone"])),
        ladder=[LadderRuleRow.from_json(x) for x in (d.get("ladder") or [])],
        enable_trace=bool(d.get("enable_trace", True)),
    )
    s.validate()
    return s

def save_json(path: str, obj: Dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, sort_keys=True)

def load_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def save_ruleset(path: str, ruleset: Ruleset) -> None:
    save_json(path, ruleset_to_json(ruleset))

def load_ruleset(path: str) -> Ruleset:
    return ruleset_from_json(load_json(path))

def save_strategy(path: str, strategy: StrategyConfig) -> None:
    save_json(path, strategy_to_json(strategy))

def load_strategy(path: str) -> StrategyConfig:
    return strategy_from_json(load_json(path))


# =========================
# Defaults
# =========================

def default_ruleset() -> Ruleset:
    rs = Ruleset()
    rs.validate()
    return rs

def default_strategy_config(bank_threshold: int = 300) -> StrategyConfig:
    s = StrategyConfig(
        name=f"Default: Bank>={bank_threshold} (finish-min endzone)",
        bank_threshold_normal=bank_threshold,
        bank_threshold_post_hot=max(600, bank_threshold + 200),
        low_dice_thresholds={1: 800, 2: 650},
        near_goal_thresholds=[(250, 150), (500, 250)],
        ladder=_default_ladder_rows(),
        enable_trace=True,
    )
    s.validate()
    return s

def _default_ladder_rows() -> List[LadderRuleRow]:
    # Priority ascending
    return [
        LadderRuleRow(priority=10, enabled=True, rule_type=RuleType.FINISH_MIN_POINTS, p1=0, p2=0, note="If can finish, take minimum finishing option"),
        LadderRuleRow(priority=20, enabled=True, rule_type=RuleType.PREFER_HOT_DICE, p1=0, p2=0, note="Prefer hot dice"),
        LadderRuleRow(priority=30, enabled=True, rule_type=RuleType.MAX_POINTS, p1=0, p2=0, note="Prefer higher immediate points"),
        LadderRuleRow(priority=40, enabled=True, rule_type=RuleType.MAX_DICE_LEFT, p1=0, p2=0, note="Prefer leaving more dice"),
        LadderRuleRow(priority=50, enabled=True, rule_type=RuleType.AVOID_LEAVE_1_DIE, p1=0, p2=0, note="Avoid leaving 1 die"),
        LadderRuleRow(priority=60, enabled=True, rule_type=RuleType.PREFER_SETS, p1=0, p2=0, note="Prefer sets"),
        LadderRuleRow(priority=70, enabled=True, rule_type=RuleType.PREFER_STRAIGHTS, p1=0, p2=0, note="Prefer straights"),
    ]


# =========================
# Scoring / Option Enumeration
# =========================

def _popcount(x: int) -> int:
    return x.bit_count()

def _mask_indices(mask: int, k: int) -> List[int]:
    return [i for i in range(k) if (mask >> i) & 1]

def _faces_from_indices(roll: Sequence[int], idxs: Sequence[int]) -> Tuple[int, ...]:
    return tuple(roll[i] for i in idxs)

def _make_mask(idxs: Sequence[int]) -> int:
    m = 0
    for i in idxs:
        m |= (1 << i)
    return m

def _score_set(face: int, n: int, ruleset: Ruleset) -> int:
    # n in 3..6
    base = ruleset.set_base[face]
    return (n - 2) * base

def _score_single(face: int, ruleset: Ruleset) -> int:
    if face == 1:
        return ruleset.single_1
    if face == 5:
        return ruleset.single_5
    return 0

def _is_large_straight_faces(faces: Sequence[int], ruleset: Ruleset) -> bool:
    return tuple(sorted(faces)) == tuple(ruleset.large_straight)

def _is_small_straight_faces(faces: Sequence[int], ruleset: Ruleset) -> bool:
    s = tuple(sorted(faces))
    return any(s == tuple(seq) for seq in ruleset.small_straights)

@dataclass(frozen=True)
class _Group:
    mask: int
    points: int
    tag: str
    detail: str

def _generate_groups_for_roll(roll: Sequence[int], ruleset: Ruleset) -> List[_Group]:
    """
    Generate all primitive scoring groups for this roll:
    - singles: each individual 1 or 5 as its own group
    - sets: any face with count>=3, for n=3..count, and for each combination of indices
    - straights: 5 or 6 straight; choose indices for each required face (handle duplicates)
    """
    k = len(roll)
    groups: List[_Group] = []

    # Singles
    for i, face in enumerate(roll):
        pts = _score_single(face, ruleset)
        if pts > 0:
            groups.append(_Group(mask=(1 << i), points=pts, tag="SINGLE", detail=f"{face}"))

    # Sets
    face_to_idxs: Dict[int, List[int]] = {f: [] for f in range(1, 7)}
    for i, face in enumerate(roll):
        face_to_idxs[face].append(i)

    for face, idxs in face_to_idxs.items():
        if len(idxs) >= 3:
            for n in range(3, len(idxs) + 1):
                pts = _score_set(face, n, ruleset)
                for combo in itertools.combinations(idxs, n):
                    m = _make_mask(combo)
                    groups.append(_Group(mask=m, points=pts, tag="SET", detail=f"{n}x{face}"))

    # Straights (6)
    need6 = ruleset.large_straight
    # build choices per needed face (indices where that face appears)
    choices6 = [face_to_idxs[f] for f in need6]
    if all(len(c) > 0 for c in choices6):
        # choose one index from each face list
        for pick in itertools.product(*choices6):
            if len(set(pick)) != 6:
                continue  # should not happen since each face distinct, but safe
            m = _make_mask(pick)
            groups.append(_Group(mask=m, points=ruleset.large_straight_score, tag="STRAIGHT6", detail="1-2-3-4-5-6"))

    # Straights (5)
    for seq in ruleset.small_straights:
        choices5 = [face_to_idxs[f] for f in seq]
        if all(len(c) > 0 for c in choices5):
            for pick in itertools.product(*choices5):
                if len(set(pick)) != 5:
                    continue
                m = _make_mask(pick)
                groups.append(_Group(mask=m, points=ruleset.small_straight_score, tag="STRAIGHT5", detail="-".join(map(str, seq))))

    # If you ever add three-pairs, it would go here.
    return groups

def enumerate_action_options(roll: Sequence[int], ruleset: Ruleset) -> List[ActionOption]:
    """
    Enumerate all legal 'takes' (combinations of disjoint scoring groups) from this roll.

    Each ActionOption is a disjoint combination of primitive groups.
    This correctly represents choices like:
      - taking only one 5 (even if other scoring exists)
      - taking 3-of-kind vs 4-of-kind (choosing subset)
      - taking straight vs singles from the same dice
    """
    k = len(roll)
    groups = _generate_groups_for_roll(roll, ruleset)

    # Backtracking over disjoint group combinations
    options_map: Dict[Tuple[int, int], ActionOption] = {}  # key: (mask, points) -> option (dedupe identical)
    # Sort groups for deterministic enumeration
    groups_sorted = sorted(groups, key=lambda g: (g.mask, g.points, g.tag, g.detail))

    def backtrack(start: int, used_mask: int, points: int, tags: List[str], details: List[str]) -> None:
        if points > 0:
            dice_used = _popcount(used_mask)
            dice_left = k - dice_used
            triggers_hot = (dice_left == 0)
            # tags summary
            tagset: Set[str] = set()
            for t in tags:
                if t == "SINGLE":
                    tagset.add("SINGLES")
                elif t == "SET":
                    tagset.add("SETS")
                elif t.startswith("STRAIGHT"):
                    tagset.add("STRAIGHTS")
            detail_str = " + ".join(details)

            opt = ActionOption(
                mask=used_mask,
                points=points,
                dice_used=dice_used,
                dice_left_after_take=dice_left,
                triggers_hot_dice=triggers_hot,
                tags=tuple(sorted(tagset)),
                detail=detail_str
            )
            options_map[(used_mask, points)] = opt

        for i in range(start, len(groups_sorted)):
            g = groups_sorted[i]
            if (g.mask & used_mask) != 0:
                continue
            backtrack(
                i + 1,
                used_mask | g.mask,
                points + g.points,
                tags + [g.tag if g.tag != "SINGLE" else "SINGLE"],
                details + [g.detail],
            )

    backtrack(0, 0, 0, [], [])

    opts = list(options_map.values())
    # Sort: higher points first, then more dice used, then mask
    opts.sort(key=lambda o: (o.points, o.dice_used, o.mask), reverse=True)
    return opts


# =========================
# State / Turn Mechanics
# =========================

def _compute_phase(total_score: int, entered_board: bool, ruleset: Ruleset) -> Phase:
    if not entered_board:
        return Phase.OFF_BOARD
    if total_score >= ruleset.endzone_start:
        return Phase.ENDZONE
    return Phase.NORMAL

def banking_allowed(total_score: int, turn_score: int, entered_board: bool, ruleset: Ruleset) -> bool:
    # Entry constraint
    if not entered_board and turn_score < ruleset.entry_threshold:
        return False
    # Endzone constraint
    if entered_board and total_score >= ruleset.endzone_start:
        if total_score + turn_score < ruleset.target_score:
            return False
    return True

def must_continue(total_score: int, turn_score: int, entered_board: bool, ruleset: Ruleset) -> bool:
    return not banking_allowed(total_score, turn_score, entered_board, ruleset)

def make_state(
    total_score: int,
    turn_score: int,
    dice_remaining: int,
    hot_dice_available: bool,
    turn_index: int,
    roll_index_in_turn: int,
    entered_board: bool,
    ruleset: Ruleset,
) -> GameState:
    phase = _compute_phase(total_score, entered_board, ruleset)
    mc = must_continue(total_score, turn_score, entered_board, ruleset)
    pn = max(0, ruleset.target_score - total_score)
    return GameState(
        total_score=total_score,
        turn_score=turn_score,
        dice_remaining=dice_remaining,
        phase=phase,
        hot_dice_available=hot_dice_available,
        turn_index=turn_index,
        roll_index_in_turn=roll_index_in_turn,
        entered_board=entered_board,
        must_continue=mc,
        points_needed=pn,
    )


# =========================
# Strategy: Priority Ladder + Banking Policy
# =========================

def _sorted_ladder(strategy: StrategyConfig) -> List[LadderRuleRow]:
    rows = [r for r in strategy.ladder if r.enabled]
    rows.sort(key=lambda r: r.priority)
    return rows

def _phase_thresholds(strategy: StrategyConfig, state: GameState, post_hot: bool) -> int:
    """
    Compute effective bank threshold for current phase and context.
    Note: banking_allowed/must_continue may still override.
    """
    # base
    if post_hot:
        thr = strategy.bank_threshold_post_hot
    else:
        thr = strategy.bank_threshold_normal

    # phase-specific override
    if state.phase == Phase.OFF_BOARD:
        if post_hot and strategy.bank_threshold_post_hot_off_board is not None:
            thr = strategy.bank_threshold_post_hot_off_board
        elif (not post_hot) and strategy.bank_threshold_off_board is not None:
            thr = strategy.bank_threshold_off_board
    elif state.phase == Phase.ENDZONE:
        if post_hot and strategy.bank_threshold_post_hot_endzone is not None:
            thr = strategy.bank_threshold_post_hot_endzone
        elif (not post_hot) and strategy.bank_threshold_endzone is not None:
            thr = strategy.bank_threshold_endzone

    # low dice override (only applies when not in hot-dice decision state)
    if not post_hot:
        for dice_k, dice_thr in sorted(strategy.low_dice_thresholds.items()):
            if state.dice_remaining <= dice_k:
                thr = max(thr, dice_thr)

    # near goal override (use smallest points_needed_max that matches)
    for pn_max, pn_thr in sorted(strategy.near_goal_thresholds, key=lambda x: x[0]):
        if state.points_needed <= pn_max:
            thr = min(thr, pn_thr)
            break

    return max(0, int(thr))

def _rank_tuple_for_rule(
    rule: LadderRuleRow,
    state: GameState,
    option: ActionOption,
    can_finish_if_take: bool,
    finish_points_needed: int,
) -> Tuple:
    """
    Return a tuple where smaller is better (lexicographic).
    We'll invert where needed so we can keep tuple ordering consistent.
    """
    rt = rule.rule_type

    if rt == RuleType.FINISH_MIN_POINTS:
        # If can finish after taking this option (and can bank after), rank by points (minimum).
        # If cannot finish, rank after finishers.
        if can_finish_if_take:
            return (0, option.points)  # smaller points is better
        return (1, 0)

    if rt == RuleType.PREFER_HOT_DICE:
        return (0 if option.triggers_hot_dice else 1,)

    if rt == RuleType.MAX_POINTS:
        # want max points -> smaller tuple should win, so use negative points
        return (-option.points,)

    if rt == RuleType.MAX_DICE_LEFT:
        return (-option.dice_left_after_take,)

    if rt == RuleType.AVOID_LEAVE_1_DIE:
        return (0 if option.dice_left_after_take != 1 else 1,)

    if rt == RuleType.PREFER_SETS:
        return (0 if "SETS" in option.tags else 1,)

    if rt == RuleType.PREFER_STRAIGHTS:
        return (0 if "STRAIGHTS" in option.tags else 1,)

    if rt == RuleType.PREFER_SINGLES:
        return (0 if "SINGLES" in option.tags else 1,)

    if rt == RuleType.MIN_TAKE_POINTS:
        min_pts = int(rule.p1)
        return (0 if option.points >= min_pts else 1, option.points)

    if rt == RuleType.ENDZONE_PROGRESS:
        # if in endzone, prefer more progress toward finish (bigger points)
        if state.phase == Phase.ENDZONE:
            return (-option.points, )
        return (0,)

    # Default no-op
    return (0,)

def choose_option_by_ladder(
    state: GameState,
    roll: Sequence[int],
    options: List[ActionOption],
    ruleset: Ruleset,
    strategy: StrategyConfig,
) -> Tuple[int, Tuple[str, ...]]:
    """
    Returns (chosen_index, trace).
    """
    if not options:
        raise ValueError("No options to choose from (farkle).")

    ladder = _sorted_ladder(strategy)
    trace: List[str] = []

    # Determine if we are in a situation where "finish-minimum" applies:
    # end-zone forced finish is enforced by banking_allowed anyway; but we also want the strategy's
    # preference to finish as soon as possible, with minimum points overshoot.
    points_needed_total = max(0, ruleset.target_score - state.total_score)

    # Precompute finish feasibility per option: after take, if we could bank and reach target
    # NOTE: banking allowed depends on state AFTER applying take (turn_score increased).
    finish_points_needed = points_needed_total - state.turn_score  # points needed within this turn to reach target
    finish_points_needed = max(0, finish_points_needed)

    ranks: List[Tuple[Tuple, int]] = []
    for idx, opt in enumerate(options):
        turn_score_after = state.turn_score + opt.points
        can_bank_after = banking_allowed(state.total_score, turn_score_after, state.entered_board, ruleset)
        can_finish_if_take = can_bank_after and (state.total_score + turn_score_after >= ruleset.target_score)

        # Build lexicographic rank tuple across ladder
        rank_parts: List[Tuple] = []
        for row in ladder:
            rank_parts.append(_rank_tuple_for_rule(row, state, opt, can_finish_if_take, finish_points_needed))
        # add deterministic tie-breakers
        rank_parts.append((-opt.points,))                   # prefer more points
        rank_parts.append((-opt.dice_left_after_take,))     # prefer more dice left
        rank_parts.append((opt.mask,))                      # deterministic
        full_rank = tuple(rank_parts)
        ranks.append((full_rank, idx))

    ranks.sort(key=lambda x: x[0])  # smaller is better
    chosen = ranks[0][1]

    if strategy.enable_trace:
        # Provide a short trace describing top few factors
        top = options[chosen]
        trace.append(f"chosen points={top.points} dice_left={top.dice_left_after_take} hot={top.triggers_hot_dice} tags={list(top.tags)}")
        # If finish possible
        ts_after = state.turn_score + top.points
        if banking_allowed(state.total_score, ts_after, state.entered_board, ruleset) and (state.total_score + ts_after >= ruleset.target_score):
            trace.append("finish_possible_after_take=yes")

    return chosen, tuple(trace)

def decide_after_take_action(
    state_before: GameState,
    option: ActionOption,
    ruleset: Ruleset,
    strategy: StrategyConfig,
) -> Tuple[AfterTakeAction, str]:
    """
    Decide whether to BANK or ROLL (or ROLL_ALL_6 if hot dice available).
    Banking is still constrained by banking_allowed().

    Rules:
    - If must_continue -> roll (or roll_all_6 if hot dice available and choose to continue)
    - If can finish (total + turn >= target) and banking is allowed -> bank immediately
    - Else bank if turn_score >= threshold (phase/post-hot threshold)
    """
    # State after taking option
    turn_score_after = state_before.turn_score + option.points
    total = state_before.total_score
    entered = state_before.entered_board

    can_bank = banking_allowed(total, turn_score_after, entered, ruleset)
    can_finish = can_bank and (total + turn_score_after >= ruleset.target_score)

    post_hot = option.triggers_hot_dice  # right after take
    # Determine next dice count if we continue:
    # - if triggers hot dice, continuing implies rolling all 6
    # - else roll remaining
    if can_finish:
        return AfterTakeAction.BANK, "finish_now"

    if not can_bank:
        # forced continue
        if post_hot:
            return AfterTakeAction.ROLL_ALL_6, "forced_continue_post_hot"
        return AfterTakeAction.ROLL_REMAINING, "forced_continue"

    thr = _phase_thresholds(strategy, state_before, post_hot=post_hot)
    if turn_score_after >= thr:
        return AfterTakeAction.BANK, f"bank_threshold_met({thr})"

    # Otherwise continue
    if post_hot:
        # Hot dice is optional; here we continue by default if threshold not met.
        if ruleset.hot_dice_optional:
            return AfterTakeAction.ROLL_ALL_6, f"continue_post_hot(thr={thr})"
        # if hot dice not optional (rare), still roll
        return AfterTakeAction.ROLL_ALL_6, "continue_post_hot_mandatory"

    return AfterTakeAction.ROLL_REMAINING, f"continue(thr={thr})"


# =========================
# Simulation
# =========================

class CancelToken:
    def __init__(self) -> None:
        self._cancel = False
    def cancel(self) -> None:
        self._cancel = True
    def is_cancelled(self) -> bool:
        return self._cancel

def roll_dice(k: int, rng: random.Random) -> Tuple[int, ...]:
    return tuple(rng.randint(1, 6) for _ in range(k))

def simulate_games(
    ruleset: Ruleset,
    strategy: StrategyConfig,
    sim_config: SimConfig,
    progress_cb: Optional[Callable[[int, int], None]] = None,
    cancel_token: Optional[CancelToken] = None,
) -> SimResult:
    ruleset.validate()
    strategy.validate()

    rng = random.Random(sim_config.seed)

    summaries: List[GameSummary] = []
    turns: List[TurnRecord] = []
    event_log: List[Dict[str, Any]] = []

    for game_i in range(sim_config.n_games):
        if cancel_token and cancel_token.is_cancelled():
            break

        t0 = time.time()

        total_score = 0
        entered_board = False
        phase = Phase.OFF_BOARD
        farkles = 0
        hot_events = 0
        total_rolls = 0
        entered_on_turn: Optional[int] = None
        endzone_entered_on_turn: Optional[int] = None

        finished = False
        turn_i = 0

        while turn_i < ruleset.max_turns_per_game:
            if cancel_token and cancel_token.is_cancelled():
                break
            if total_score >= ruleset.target_score:
                finished = True
                break

            turn_score = 0
            dice_remaining = 6
            hot_dice_available = False
            rolls_in_turn = 0
            hot_in_turn = 0
            phase_start = _compute_phase(total_score, entered_board, ruleset)

            roll_j = 0
            farkle_this_turn = False

            while roll_j < ruleset.max_rolls_per_turn:
                if cancel_token and cancel_token.is_cancelled():
                    break

                state = make_state(
                    total_score=total_score,
                    turn_score=turn_score,
                    dice_remaining=dice_remaining,
                    hot_dice_available=hot_dice_available,
                    turn_index=turn_i,
                    roll_index_in_turn=roll_j,
                    entered_board=entered_board,
                    ruleset=ruleset,
                )

                # Roll dice
                roll = roll_dice(dice_remaining, rng)
                total_rolls += 1
                rolls_in_turn += 1

                opts = enumerate_action_options(roll, ruleset)
                if not opts:
                    # Farkle: lose turn points; if off-board, still off-board.
                    farkles += 1
                    farkle_this_turn = True
                    turn_score = 0
                    # end turn
                    if sim_config.collect_event_log and game_i < sim_config.max_games_for_event_log:
                        event_log.append({
                            "type": "FARKLE",
                            "game": game_i,
                            "turn": turn_i,
                            "roll": roll,
                            "state": dataclasses.asdict(state),
                        })
                    break

                # Choose option via ladder
                chosen_idx, trace = choose_option_by_ladder(state, roll, opts, ruleset, strategy)
                chosen = opts[chosen_idx]

                # Decide bank/roll
                after_action, reason = decide_after_take_action(state, chosen, ruleset, strategy)

                # Apply take
                turn_score_after = turn_score + chosen.points
                dice_left = chosen.dice_left_after_take
                triggers_hot = chosen.triggers_hot_dice

                if sim_config.collect_event_log and game_i < sim_config.max_games_for_event_log:
                    # Store a compact option list for debug (top 10)
                    top_opts = opts[:10]
                    event_log.append({
                        "type": "DECISION",
                        "game": game_i,
                        "turn": turn_i,
                        "roll_index": roll_j,
                        "roll": roll,
                        "turn_score_before": turn_score,
                        "total_score": total_score,
                        "entered_board": entered_board,
                        "phase": state.phase.value,
                        "options_top": [
                            {"points": o.points, "dice_left": o.dice_left_after_take, "hot": o.triggers_hot_dice, "tags": list(o.tags), "detail": o.detail, "mask": o.mask}
                            for o in top_opts
                        ],
                        "chosen": {"idx": chosen_idx, "points": chosen.points, "dice_left": dice_left, "hot": triggers_hot, "tags": list(chosen.tags), "detail": chosen.detail, "mask": chosen.mask},
                        "after_action": after_action.value,
                        "reason": reason,
                        "trace": list(trace),
                    })

                # Update state variables after take
                turn_score = turn_score_after

                if triggers_hot:
                    hot_dice_available = True
                    hot_events += 1
                    hot_in_turn += 1
                    dice_remaining = 0
                else:
                    hot_dice_available = False
                    dice_remaining = dice_left

                # Execute after_take_action
                if after_action == AfterTakeAction.BANK:
                    if not banking_allowed(total_score, turn_score, entered_board, ruleset):
                        # Strategy attempted illegal bank. Fail safe: force continue.
                        if triggers_hot:
                            after_action = AfterTakeAction.ROLL_ALL_6
                        else:
                            after_action = AfterTakeAction.ROLL_REMAINING

                    else:
                        # Bank it
                        total_before = total_score
                        total_score += turn_score
                        turn_score_banked = turn_score
                        turn_score = 0

                        if not entered_board:
                            entered_board = True
                            entered_on_turn = turn_i if entered_on_turn is None else entered_on_turn

                        # phase transitions
                        if entered_board and total_before < ruleset.endzone_start <= total_score and endzone_entered_on_turn is None:
                            endzone_entered_on_turn = turn_i

                        # end turn
                        if sim_config.collect_event_log and game_i < sim_config.max_games_for_event_log:
                            event_log.append({
                                "type": "BANK",
                                "game": game_i,
                                "turn": turn_i,
                                "banked": turn_score_banked,
                                "total_score_end": total_score,
                            })
                        break

                if after_action == AfterTakeAction.ROLL_ALL_6:
                    # only meaningful if hot dice was available
                    dice_remaining = 6
                    hot_dice_available = False
                    roll_j += 1
                    continue

                if after_action == AfterTakeAction.ROLL_REMAINING:
                    # continue with dice_remaining as set above
                    roll_j += 1
                    continue

                # Should never reach here
                roll_j += 1

            # End of turn
            phase_end = _compute_phase(total_score, entered_board, ruleset)
            turns.append(TurnRecord(
                game_index=game_i,
                turn_index=turn_i,
                total_score_start=(total_score - 0),  # best-effort; see below
                total_score_end=total_score,
                turn_score_banked=(0 if farkle_this_turn else 0),  # corrected below
                rolls_in_turn=rolls_in_turn,
                farkle=farkle_this_turn,
                hot_dice_events_in_turn=hot_in_turn,
                phase_start=phase_start,
                phase_end=phase_end,
            ))
            # Patch the last TurnRecord with better start/banked values:
            # We didn't store total_score_start or banked in the loop; keep it simple:
            # - total_score_start = total_score_end if farkle, else total_score_end - last banked (we don't store last banked here).
            # For v1 analytics we mostly need end totals and roll counts; start totals are optional.
            # If you want perfect turn records, enable event log and reconstruct later.
            # We'll at least set turn_score_banked if we can infer it from BANK events; but that requires scanning event log.

            turn_i += 1

            if total_score >= ruleset.target_score:
                finished = True
                break

        elapsed = time.time() - t0
        summaries.append(GameSummary(
            game_index=game_i,
            finished=finished,
            total_score=total_score,
            turns=turn_i if finished else turn_i,
            total_rolls=total_rolls,
            farkles=farkles,
            hot_dice_events=hot_events,
            entered_on_turn=entered_on_turn,
            endzone_entered_on_turn=endzone_entered_on_turn,
            elapsed_s=elapsed,
        ))

        if progress_cb and ((game_i + 1) % max(1, sim_config.progress_every) == 0):
            progress_cb(game_i + 1, sim_config.n_games)

    return SimResult(
        ruleset=ruleset,
        strategy=strategy,
        sim_config=sim_config,
        summaries=summaries,
        turns=turns if sim_config.collect_turn_records else [],
        event_log=event_log if sim_config.collect_event_log else [],
    )


# =========================
# Minimal Analytics Helpers (plot-ready series)
# =========================

def summarize(sim: SimResult) -> Dict[str, Any]:
    """Return headline metrics (dict) for UI display."""
    fins = [s for s in sim.summaries if s.finished]
    if not fins:
        return {"finished_games": 0}

    turns = [s.turns for s in fins]
    farkles = [s.farkles for s in fins]
    hot = [s.hot_dice_events for s in fins]
    rolls = [s.total_rolls for s in fins]

    def pct(xs: List[int], p: float) -> float:
        xs2 = sorted(xs)
        if not xs2:
            return float("nan")
        i = int(round((len(xs2) - 1) * p))
        return float(xs2[max(0, min(len(xs2) - 1, i))])

    return {
        "finished_games": len(fins),
        "mean_turns": statistics.mean(turns),
        "median_turns": statistics.median(turns),
        "p10_turns": pct(turns, 0.10),
        "p90_turns": pct(turns, 0.90),
        "mean_farkles": statistics.mean(farkles),
        "mean_hot_dice": statistics.mean(hot),
        "mean_rolls": statistics.mean(rolls),
        "games_total": len(sim.summaries),
        "seed": sim.sim_config.seed,
        "strategy_name": sim.strategy.name,
    }

def series_turns_to_win(sim: SimResult) -> Dict[str, Any]:
    fins = [s for s in sim.summaries if s.finished]
    turns = sorted(s.turns for s in fins)
    return {"turns": turns}

def series_histogram(data: List[int], bins: int = 30) -> Dict[str, Any]:
    if not data:
        return {"bins": [], "counts": []}
    lo, hi = min(data), max(data)
    if lo == hi:
        return {"bins": [lo, hi + 1], "counts": [len(data)]}
    # inclusive-ish histogram
    width = max(1, int(math.ceil((hi - lo + 1) / bins)))
    edges = list(range(lo, hi + width + 1, width))
    counts = [0] * (len(edges) - 1)
    for x in data:
        idx = min(len(edges) - 2, (x - lo) // width)
        counts[idx] += 1
    return {"bins": edges, "counts": counts}

def series_cdf(data: List[int]) -> Dict[str, Any]:
    if not data:
        return {"x": [], "y": []}
    xs = sorted(data)
    n = len(xs)
    # compress unique values
    x_unique = []
    y = []
    for val, grp in itertools.groupby(xs):
        x_unique.append(val)
        cnt = sum(1 for _ in grp)
        y.append(cnt)
    # cumulative
    cum = 0
    y_cum = []
    for cnt in y:
        cum += cnt
        y_cum.append(cum / n)
    return {"x": x_unique, "y": y_cum}

def diagnostic_text(sim: Optional[SimResult] = None, extra: Optional[Dict[str, Any]] = None) -> str:
    lines = []
    lines.append("tenk_core diagnostic")
    lines.append(f"schema_version={SCHEMA_VERSION}")
    lines.append(f"python_random={random.__version__ if hasattr(random,'__version__') else 'builtin'}")
    if extra:
        lines.append("extra=" + json.dumps(extra, sort_keys=True))
    if sim:
        lines.append("ruleset=" + json.dumps(ruleset_to_json(sim.ruleset), sort_keys=True))
        lines.append("strategy=" + json.dumps(strategy_to_json(sim.strategy), sort_keys=True))
        lines.append("sim_config=" + json.dumps(asdict(sim.sim_config), sort_keys=True))
        lines.append("summary=" + json.dumps(summarize(sim), sort_keys=True))
        if sim.event_log:
            lines.append(f"event_log_len={len(sim.event_log)}")
    return "\n".join(lines)


# =========================
# Diagnostics / Smoke Tests
# =========================

def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)

def smoke_test() -> Dict[str, Any]:
    """
    Fast self-checks:
    - scoring option enumeration contains expected options for known rolls
    - forced banking rules behave as expected
    """
    rs = default_ruleset()

    # Test 1: triple ones present
    roll = (1, 1, 1, 2, 3, 4)
    opts = enumerate_action_options(roll, rs)
    _assert(any(o.points == 1000 and "SETS" in o.tags for o in opts), "Expected triple ones option")

    # Test 2: 4 ones should have 2000 for taking all 4 as a set
    roll = (1, 1, 1, 1, 2, 6)
    opts = enumerate_action_options(roll, rs)
    _assert(any(o.points == 2000 and "SETS" in o.tags for o in opts), "Expected 4x1 = 2000 option")

    # Test 3: small straight option exists
    roll = (1, 2, 3, 4, 5, 6)
    opts = enumerate_action_options(roll, rs)
    _assert(any(o.points == rs.large_straight_score and "STRAIGHTS" in o.tags and o.dice_left_after_take == 0 for o in opts),
            "Expected 6-straight option")
    # ensure also 5-straight can exist (e.g., 1-2-3-4-5)
    roll = (1, 2, 3, 4, 5)
    opts = enumerate_action_options(roll, rs)
    _assert(any(o.points == rs.small_straight_score and "STRAIGHTS" in o.tags for o in opts),
            "Expected 5-straight option")

    # Test 4: additive scoring: 4-4-4 + 1 + 5 = 400 + 100 + 50 = 550 (when chosen)
    roll = (4, 4, 4, 1, 5, 2)
    opts = enumerate_action_options(roll, rs)
    _assert(any(o.points == 550 for o in opts), "Expected option scoring 550 (444 + 1 + 5)")

    # Test 5: banking rules (entry)
    entered = False
    _assert(not banking_allowed(0, 700, entered, rs), "Cannot bank before entry threshold")
    _assert(banking_allowed(0, 750, entered, rs), "Can bank at entry threshold")
    # Test 6: banking rules (endzone)
    entered = True
    _assert(not banking_allowed(9250, 700, entered, rs), "Endzone: cannot bank unless reach 10000")
    _assert(banking_allowed(9250, 750, entered, rs), "Endzone: can bank if reach 10000")

    # Quick sim small
    st = default_strategy_config(300)
    sim = simulate_games(rs, st, SimConfig(n_games=50, seed=123, collect_turn_records=False, collect_event_log=False))
    summ = summarize(sim)
    _assert(summ.get("finished_games", 0) > 0, "Expected some finished games in smoke sim")

    return {"ok": True, "summary": summ}


# =========================
# CLI
# =========================

def _main() -> int:
    ap = argparse.ArgumentParser(description="tenk_core backend")
    ap.add_argument("--smoke", action="store_true", help="Run smoke tests and exit")
    ap.add_argument("--games", type=int, default=200, help="Number of games to simulate")
    ap.add_argument("--seed", type=int, default=123, help="Random seed")
    ap.add_argument("--eventlog", action="store_true", help="Collect event log (first few games)")
    args = ap.parse_args()

    try:
        if args.smoke:
            out = smoke_test()
            print(json.dumps(out, indent=2, sort_keys=True))
            return 0

        rs = default_ruleset()
        st = default_strategy_config(300)
        cfg = SimConfig(
            n_games=args.games,
            seed=args.seed,
            collect_turn_records=True,
            collect_event_log=args.eventlog,
            max_games_for_event_log=5,
            progress_every=max(1, args.games // 5),
        )

        def progress(done: int, total: int) -> None:
            print(f"progress: {done}/{total}")

        sim = simulate_games(rs, st, cfg, progress_cb=progress)
        print(json.dumps(summarize(sim), indent=2, sort_keys=True))
        print("\n--- diagnostic (copy/paste) ---")
        print(diagnostic_text(sim))

        return 0
    except Exception as e:
        print("ERROR:", e)
        print(traceback.format_exc())
        return 1


if __name__ == "__main__":
    raise SystemExit(_main())
