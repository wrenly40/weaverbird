"""The game plan — genre-agnostic generation's decision layer (0.11.0).

Before 0.11.0 the factory matched every scope into a named
preset (arena collector, platformer, crate pusher) and built
that preset's game. That funnel is gone. What a scope becomes is
now a PLAN, derived from the scope's own words:

- a **movement** from the primitive vocabulary
  (``top_down_2d``, ``side_view``, ``grid_step``, ``top_down_3d``),
- an **objective** (``collect``, ``reach``, ``survive``,
  ``deliver``),
- **fail rules** (``timer``, ``hazard_contacts``, ``fall``),
- **entities** — kinds, the maker's own labels for them, and a
  behaviour per group (static, wander, patrol, chase, flee,
  pushable, ambient), each assigned from the scope text,
- the **numbers** (units, per-round targets, seconds, speeds,
  lives), honouring the scope-number rules: a stated target
  count is the maker's spec and pins every round,
- and a **derivation note** for every choice, quoting the scope
  words that caused it.

One generic runtime (game_templates/runtime/) executes any plan;
there is no per-genre code path left to select. When the scope's
core mechanic cannot be expressed in the vocabulary at all —
tower-defence economies, match-3 cascades, dialogue trees,
ordered-checkpoint racing — the plan says so in
``bespoke_required`` and generation stops at the plan unless a
brain is configured to write a bespoke mechanics module. No
substitute game is ever built quietly.
"""

from __future__ import annotations

import re
import zlib

# ---------------------------------------------------------------------------
# Scope facts the derivation reads — the scope-stated target
# count (the F7/D6 machinery), the content-unit count and the
# dimension. These live in generate.py, where they have always
# lived; they are imported lazily so this module never cycles
# with it (generate imports this module's derive_plan the same
# way).

def scope_target_count(values: dict) -> dict | None:
    from . import generate as generate_module
    return generate_module.scope_target_count(values)


def content_units(values: dict) -> int:
    from . import generate as generate_module
    return generate_module.content_units(values)


def scope_dimension(values: dict) -> str:
    from . import generate as generate_module
    return generate_module.scope_dimension(values)


# ---------------------------------------------------------------------------
# Text signals

# Fields whose text describes the game's mechanics (as opposed
# to its production or style), searched in this order.
_MECHANIC_FIELDS = (
    "Genre / archetype",
    "Core verb (what the player does most)",
    "Win condition",
    "Lose / fail condition",
    "One-line pitch",
    "Must-have features (max 3)",
    "Content counts (levels / waves / rooms / items, as applicable)",
    "Difficulty target (who it is for, how hard)",
)


def _field_texts(values: dict) -> list[tuple[str, str]]:
    return [(name, str(values.get(name, "") or ""))
            for name in _MECHANIC_FIELDS]


def _all_text(values: dict) -> str:
    return " ".join(text for _name, text in _field_texts(values)).lower()


def _find(values: dict, pattern: str):
    """First regex hit across the mechanic fields, as
    (field, matched text) — or None. Used for derivation notes:
    every plan choice quotes the scope words that caused it."""
    regex = re.compile(pattern, re.IGNORECASE)
    for name, text in _field_texts(values):
        match = regex.search(text)
        if match:
            return name, match.group(0)
    return None


_SIDE_SIGNALS = (
    r"platform", r"\bjump", r"side[-\s]?scroll", r"sidescroll",
    r"side[-\s]?view", r"rooftop", r"\bclimb", r"leap",
    r"2d\s+platformer",
)


def _side_hit(values: dict):
    for pattern in _SIDE_SIGNALS:
        hit = _find(values, pattern)
        if hit:
            return hit
    return None


def _push_hit(values: dict):
    """Pushing things onto places — the sokoban family. Needs
    pushing language plus something pushable, or the genre named
    outright; a bare 'push' (push through the crowd) is not it."""
    hit = _find(values, r"sokoban|crate\s*pusher|crate[-\s]pushing")
    if hit:
        return hit
    push = _find(values, r"\bpush(?:ing|es)?\b")
    if push and _find(values, r"crates?|boxes|blocks|barrels"):
        return push
    return None


# Mechanics the primitive vocabulary cannot express. These name
# whole mechanics, not verbs, so they are checked before the
# primitive signals: a tower-defence scope also says "waves" and
# "defend", and it is the tower economy — not the waves — that
# the vocabulary cannot build.
_BESPOKE_PATTERNS: list[tuple[str, str]] = [
    (r"tower\s*defen[cs]e",
     "tower defence (placing and upgrading towers, an economy "
     "between waves)"),
    (r"match[-\s]?3|match\s+three|tile[-\s]?matching",
     "match-3 cascades"),
    (r"dialogue\s+tree|conversation\s+tree|visual\s+novel|dating\s+sim",
     "dialogue trees"),
    (r"city[-\s]?build|base[-\s]?build|settlement\s+build",
     "building and construction"),
    (r"farming\s+sim|farm\s+simulation",
     "a farming simulation"),
    (r"deck[-\s]?build|card\s+battler|collectible\s+card",
     "card battling with a built deck"),
    (r"rhythm\s+(game|timing|action)",
     "rhythm timing"),
    (r"negotiat",
     "negotiation"),
    (r"interrogat|detective|deduce|deducing",
     "investigation and deduction"),
    (r"courtroom|debate",
     "argument and debate"),
    (r"trade\s+empire|trading\s+empire|stock\s+market",
     "a trading economy"),
    (r"compose\s+music|music\s+composition|dance\s+battle",
     "music composition / dance battles"),
    (r"romance|romancing",
     "romance"),
]

# The older, weaker bespoke signals, kept for scopes that name
# an unbuildable loop without any primitive verb at all.
_BESPOKE_WEAK = (
    "dialogue", "conversation", "city build", "farming sim",
    "courtroom", "debate", "negotiat",
)
_PRIMITIVE_WORDS = (
    "platform", "jump", "puzzle", "sokoban", "crate", "push",
    "collect", "gather", "arena", "surviv", "reach", "dodge",
    "avoid", "run", "wave", "deliver", "escape", "chase",
)


def _bespoke(values: dict) -> dict | None:
    """{"mechanic", "because"} when the scope's core mechanic is
    outside the vocabulary — else None."""
    for pattern, mechanic in _BESPOKE_PATTERNS:
        hit = _find(values, pattern)
        if hit:
            field, snippet = hit
            return {"mechanic": mechanic,
                    "because": f"the {field} says “{snippet}”"}
    # Ordered checkpoints are a racing structure the vocabulary
    # has no ordering primitive for: checkpoints PLUS racing.
    checkpoint = _find(values, r"checkpoints?")
    if checkpoint and _find(values, r"\brac(e|ing)\b|\blaps?\b"):
        field, snippet = checkpoint
        return {"mechanic": "ordered-checkpoint racing",
                "because": f"the {field} says “{snippet}”"}
    text = _all_text(values)
    weak = [signal for signal in _BESPOKE_WEAK if signal in text]
    if weak and not any(word in text for word in _PRIMITIVE_WORDS):
        return {"mechanic": f"a core loop built on “{weak[0]}”",
                "because": f"the scope's mechanic text says "
                           f"“{weak[0]}” and names no movement, "
                           "objective or fail primitive"}
    return None


# ---------------------------------------------------------------------------
# Labels — the maker's own nouns for their entities

_COLLECT_PHRASE_RE = re.compile(
    r"\b(?:collect|gather|grab|pick\s+up|collecting|gathering)\s+"
    r"(?:all\s+the\s+|the\s+|a\s+|an\s+)?(?:\d+\s+)?"
    r"([a-z][a-z\- ]{1,26}?)"
    r"(?=\s+(?:each|per|in|on|before|to|and|while|from|for|within|"
    r"around|across|under|over)\b|[.,;:!]|$)",
    re.IGNORECASE)
_LABEL_STOP = {
    "every", "all", "any", "them", "it", "one", "some", "each",
    "thing", "things", "item", "items",
}


def _clean_label(raw: str) -> str:
    words = [w for w in re.split(r"\s+", raw.strip().lower())
             if w and w not in ("the", "a", "an")]
    words = words[:3]
    while words and words[-1] in ("each", "per", "round", "level"):
        words.pop()
    return " ".join(words)


def collectible_label(values: dict) -> str:
    """The maker's noun for what is collected, title-cased:
    'Collect 8 comet pups each round' -> 'Comet Pups'. Falls
    back to the counted noun ('10 stars' -> 'Stars'), then to
    'Targets'."""
    for name in ("Win condition",
                 "Core verb (what the player does most)",
                 "Content counts (levels / waves / rooms / items, "
                 "as applicable)",
                 "One-line pitch"):
        text = str(values.get(name, "") or "")
        match = _COLLECT_PHRASE_RE.search(text)
        if match:
            label = _clean_label(match.group(1))
            if label and label.split()[0] not in _LABEL_STOP \
                    and len(label) >= 3:
                return label.title()
    noun = re.search(
        r"\b\d{1,4}\s+(stars?|targets?|points?|orbs?|coins?|gems?"
        r"|items?|crystals?)\b",
        str(values.get("Win condition", "") or ""), re.IGNORECASE)
    if noun:
        return noun.group(1).title()
    return "Targets"


_HAZARD_AFTER_RE = re.compile(
    r"\b(?:dodge|avoid|beware|escape|outrun|flee)\s+(?:the\s+|a\s+|an\s+)?"
    r"([a-z][a-z ]{2,24}?)(?=[.,;!]|$|\s+(?:and|that|who|while|before)\b)",
    re.IGNORECASE)
_HAZARD_BEFORE_RE = re.compile(
    r"\b(?:tag|chase|hunt|patrol|guard|attack|stalk|pursue)\b",
    re.IGNORECASE)
_HAZARD_LEAD_STOP = {
    "the", "a", "an", "while", "and", "that", "who", "to", "of",
    "as", "they", "their", "with", "when", "if", "so", "but",
    "or", "will", "can", "gather", "collect", "dodge", "avoid",
    "you", "your", "in", "on", "at", "by", "from",
}


def _hazard_before(text: str):
    """The noun phrase immediately before a hazard verb:
    '...while wasps patrol the orchard' -> 'wasps'."""
    for match in _HAZARD_BEFORE_RE.finditer(text):
        words = re.findall(r"[a-z]+", text[:match.start()].lower())
        words = words[-2:]
        while words and words[0] in _HAZARD_LEAD_STOP:
            words.pop(0)
        if words:
            return " ".join(words)
    return None
_HAZARD_WORDS = (
    "hazard", "spike", "enemy", "enemies", "monster", "mites",
    "ghost", "trap", "sentry", "sentries", "drone", "slime",
    "beetle", "wasp", "snake", "shark", "guard",
)


# Words that make a derived label a sentence fragment rather
# than a noun phrase — conjunctions, prepositions, articles and
# pronouns that dangle at the edge of a bad capture ("gathering
# star shards while patrol drones…" captured "Shards While" as
# the hazard). They are trimmed from a capture's edges; a
# capture made only of them is no label at all.
_LABEL_FRAGMENT = {
    "while", "when", "and", "or", "but", "that", "who", "whom",
    "to", "of", "with", "as", "if", "so", "in", "on", "at",
    "by", "from", "per", "each", "they", "their", "you",
    "your", "it", "its", "then", "than",
}


def _clean_hazard_label(raw: str, collectible: str) -> str | None:
    """A hazard label from a raw capture, or None when the
    capture is not a clean noun phrase. Fragment words are
    stripped from both ends; what remains must be 1–3 words and
    at least 3 characters — and never the collectible's noun, in
    whole or in part: the things you gather are not the things
    you avoid, so a derivation that lands on them (or on a
    phrase built from them) found the wrong words, and the
    caller falls back to the neutral "Hazards"."""
    words = [w for w in re.split(r"\s+", str(raw or "").strip().lower())
             if w and w not in ("the", "a", "an")]
    while words and words[-1] in _LABEL_FRAGMENT:
        words.pop()
    while words and words[0] in _LABEL_FRAGMENT:
        words.pop(0)
    words = words[:3]
    while words and words[-1] in _LABEL_FRAGMENT:
        words.pop()
    if not words or len(" ".join(words)) < 3:
        return None
    own = set(words)
    theirs = {w for w in re.split(
        r"\s+", str(collectible or "").strip().lower()) if w}
    if theirs and (own <= theirs or theirs <= own):
        return None
    return " ".join(words).title()


def _hazard_label(values: dict) -> str:
    fields = ("One-line pitch", "Lose / fail condition",
              "Core verb (what the player does most)",
              "Win condition")
    collectible = collectible_label(values)
    for name in fields:
        match = _HAZARD_AFTER_RE.search(str(values.get(name, "") or ""))
        if match:
            label = _clean_hazard_label(match.group(1), collectible)
            if label:
                return label
    for name in fields:
        hit = _hazard_before(str(values.get(name, "") or ""))
        if hit:
            label = _clean_hazard_label(hit, collectible)
            if label:
                return label
    return "Hazards"


def _hazard_mentioned(values: dict) -> bool:
    text = _all_text(values)
    return any(word in text for word in _HAZARD_WORDS) \
        or _HAZARD_AFTER_RE.search(text) is not None \
        or _HAZARD_BEFORE_RE.search(text) is not None


def _goal_label(values: dict) -> str:
    text = _all_text(values)
    for word, label in (("flag", "Flag"), ("door", "Door"),
                        ("exit", "Exit"), ("portal", "Portal"),
                        ("home", "Home")):
        if word in text:
            return label
    return "Goal"


def _pushable_label(values: dict) -> str:
    text = _all_text(values)
    for word, label in (("crate", "Crates"), ("barrel", "Barrels"),
                        ("block", "Blocks"), ("box", "Boxes")):
        if word in text:
            return label
    return "Crates"


def _zone_label(values: dict) -> str:
    text = _all_text(values)
    for word, label in (("dock", "Docks"), ("zone", "Zones"),
                        ("pad", "Pads"), ("square", "Squares")):
        if word in text:
            return label
    return "Pads"


_AMBIENT_WORDS = (
    "butterflies", "fireflies", "birds", "critters",
    "falling leaves", "dust motes", "ambient wildlife", "bees",
    "dragonflies", "petals",
)


# ---------------------------------------------------------------------------
# The derivation

def _reach_is_primary(win_text: str) -> bool:
    """True when the win sentence leads with reaching and treats
    collecting as a side activity ("reach the flag; you may
    gather fallen stars on the way")."""
    text = " " + re.sub(r"\s+", " ", str(win_text or "")).strip().lower()
    if re.search(r"\b(may|might|optional|bonus|on the way|"
                 r"along the way|if you like)\b", text):
        return True
    return bool(re.match(
        r"(reach|get to|make it to|arrive|escape|climb to|"
        r"run to|head to)\b", text))


def _scope_lives(values: dict) -> int | None:
    for name in ("Lose / fail condition",
                 "Difficulty target (who it is for, how hard)"):
        match = re.search(r"(\d+)\s*(?:lives|life)",
                          str(values.get(name, "") or ""), re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


def derive_plan(values: dict) -> dict:
    """The game plan for a scope — see the module docstring.

    Pure and deterministic: the same scope text always derives
    the same plan. `values` is the scope's {field: value} map."""
    from . import skeleton as skeleton_module

    derivations: list[str] = []
    dimension = scope_dimension(values)
    units = content_units(values)
    title = str(values.get("Title (working)", "") or "")
    seed = zlib.crc32(title.encode("utf-8")) + units
    minutes = skeleton_module._session_minutes(
        values.get("Session length (one sitting)", ""))
    round_seconds = skeleton_module._round_seconds(minutes)

    bespoke = _bespoke(values)
    side = _side_hit(values)
    if bespoke is None and dimension == "3d" and side:
        bespoke = {
            "mechanic": "3D platforming (side-view movement in "
                        "three dimensions)",
            "because": f"the scope asks for 3D and its "
                       f"{side[0]} says “{side[1]}”",
        }
    if bespoke is not None:
        return {
            "dimension": dimension,
            "movement": None,
            "objective": None,
            "fail": [],
            "lives": 1,
            "entities": [],
            "numbers": {"units": units,
                        "round_seconds": round_seconds,
                        "world_seed": seed},
            "collectible_label": collectible_label(values),
            "derivations": [
                f"bespoke required: {bespoke['mechanic']} — "
                f"scope: {bespoke['because']}"],
            "bespoke_required": bespoke,
        }

    # --- movement -------------------------------------------------
    push = _push_hit(values)
    if dimension == "3d":
        movement = "top_down_3d"
        derivations.append(
            "movement top_down_3d — scope: the Dimension field "
            "asks for 3D, and the vocabulary's 3D movement is "
            "top-down on a ground plane")
    elif push:
        movement = "grid_step"
        derivations.append(
            f"movement grid_step — scope: the {push[0]} says "
            f"“{push[1]}” (pushing is stepped, cell by cell)")
    elif side:
        movement = "side_view"
        derivations.append(
            f"movement side_view — scope: the {side[0]} says "
            f"“{side[1]}” (gravity and jumping)")
    else:
        movement = "top_down_2d"
        derivations.append(
            "movement top_down_2d — no side-view, pushing or 3D "
            "signal in the scope; free top-down movement is the "
            "plain default")

    # --- objective --------------------------------------------------
    win_text = str(values.get("Win condition", "") or "")
    count = scope_target_count(values)
    collect_hit = _find(
        values, r"\bcollect|\bgather|\bgrab|pick\s+up")
    reach_hit = _find(
        values, r"\breach|\bescape to|\bget to|make it to|"
                r"\barrive at|flag\b|exit\b|finish line")
    survive_hit = _find(
        values, r"\bsurviv|\bstay alive|\bhold out|\bendure|"
                r"\blast\s+(?:for|the|\d)")
    if movement == "grid_step":
        objective = "deliver"
        derivations.append(
            "objective deliver — scope: pushing ends with the "
            "pushables resting on their zones")
    elif survive_hit and count is None:
        objective = "survive"
        derivations.append(
            f"objective survive — scope: the {survive_hit[0]} "
            f"says “{survive_hit[1]}”")
    elif count is not None:
        objective = "collect"
        derivations.append(
            f"objective collect — scope: the {count['field']} "
            f"states “{count['matched']}”")
    elif collect_hit and reach_hit and _reach_is_primary(win_text):
        objective = "reach"
        derivations.append(
            f"objective reach (collecting is optional on the "
            f"way) — scope: the {reach_hit[0]} says "
            f"“{reach_hit[1]}”")
    elif collect_hit and reach_hit:
        objective = "collect"
        derivations.append(
            f"objective collect (the goal opens once everything "
            f"is collected) — scope: the {collect_hit[0]} says "
            f"“{collect_hit[1]}” and the {reach_hit[0]} says "
            f"“{reach_hit[1]}”")
    elif reach_hit:
        objective = "reach"
        derivations.append(
            f"objective reach — scope: the {reach_hit[0]} says "
            f"“{reach_hit[1]}”")
    elif collect_hit:
        objective = "collect"
        derivations.append(
            f"objective collect — scope: the {collect_hit[0]} "
            f"says “{collect_hit[1]}”")
    elif movement == "side_view":
        objective = "reach"
        derivations.append(
            "objective reach — side-view levels end at a goal; "
            "no collecting was stated")
    else:
        objective = "collect"
        derivations.append(
            "objective collect — no objective stated outright; "
            "collecting is the plain default")

    # --- entities ---------------------------------------------------
    entities: list[dict] = [{
        "kind": "player", "label": "Player", "behaviour": "player",
        "derivation": "the one who moves",
    }]
    label = collectible_label(values)
    text = _all_text(values)

    def entity(kind, ent_label, behaviour, derivation, **extra):
        entry = {"kind": kind, "label": ent_label,
                 "behaviour": behaviour, "derivation": derivation}
        entry.update(extra)
        entities.append(entry)
        derivations.append(
            f"entity {kind} “{ent_label}” ({behaviour}) — "
            f"scope: {derivation}")

    if objective == "collect" or (objective == "reach" and collect_hit) \
            or movement == "side_view":
        behaviour = "static"
        why = "they wait to be collected"
        if _find(values, r"wander|drift|roam|float"):
            behaviour = "wander"
            why = "the scope has them wandering"
        if _find(values, r"flee|run away|scatter when"):
            behaviour = "flee"
            why = "the scope has them fleeing"
        entity("collectible", label, behaviour, why,
               optional=objective != "collect")

    hazards = _hazard_mentioned(values) or movement == "side_view" \
        or objective == "survive"
    if hazards:
        hlabel = _hazard_label(values)
        if movement == "side_view":
            behaviour = "static"
            why = "spike pits are part of side-view levels"
        elif _find(values, r"chas|hunt|stalk|pursue|seek"):
            behaviour = "chase"
            why = "the scope has them hunting the player"
        elif _find(values, r"drift|wander|roam"):
            behaviour = "wander"
            why = "the scope has them drifting"
        else:
            behaviour = "patrol"
            why = "they patrol their ground"
        entity("hazard", hlabel, behaviour, why)

    if objective == "reach" or (objective == "collect" and reach_hit) \
            or movement == "side_view":
        glabel = _goal_label(values)
        entity("goal", glabel, "static",
               "the place the scope says to reach")

    if movement == "grid_step":
        entity("pushable", _pushable_label(values), "pushable",
               "the scope's pushing acts on these")
        entity("zone", _zone_label(values), "static",
               "where the pushables belong")
        entity("wall", "Walls", "static",
               "the grid's fixed geometry")

    if movement == "side_view":
        entity("platform", "Platforms", "static",
               "the ground and steps the scope's jumping crosses")

    if movement == "top_down_3d":
        entity("obstacle", "Obstacles", "static",
               "blocks that break up the ground plane")

    for word in _AMBIENT_WORDS:
        if word in text and movement in ("top_down_2d", "side_view"):
            entity("ambient", word.title(), "wander",
                   f"the scope mentions {word} — decoration that "
                   "neither helps nor harms")
            break

    # --- fail rules ---------------------------------------------------
    fail: list[str] = []
    lose_text = str(values.get("Lose / fail condition", "") or "").lower()
    time_hit = ("time" in lose_text or "timer" in lose_text
                or "clock" in lose_text)
    if hazards:
        fail.append("hazard_contacts")
        derivations.append(
            "fail hazard_contacts — hazards exist, so touching "
            "them costs the player")
    if movement == "side_view":
        fail.append("fall")
        derivations.append(
            "fail fall — side-view levels have pits; falling in "
            "costs the player")
    if objective == "survive":
        derivations.append(
            "fail: surviving the full duration is the win; the "
            "timer is the objective, not a fail rule")
    elif time_hit or (objective == "collect"
                      and movement in ("top_down_2d", "top_down_3d")):
        fail.append("timer")
        derivations.append(
            "fail timer — " + (
                "the scope's lose condition is the clock"
                if time_hit else
                "collecting against a round timer is the "
                "pressure this plan states"))
    if not fail and movement != "grid_step":
        fail.append("timer")
        derivations.append(
            "fail timer — the scope states no fail pressure, so "
            "a per-unit timer is the default (a game nobody can "
            "lose is not finished)")
    if movement == "grid_step":
        derivations.append(
            "fail: a move budget per puzzle (from its verified "
            "solution) is the pressure; spending it loses the "
            "puzzle")

    lives = _scope_lives(values)
    if lives is None:
        lives = 3 if movement == "side_view" else 1
    elif "hazard_contacts" in fail:
        derivations.append(
            f"lives {lives} — scope: the lose condition states "
            f"{lives} lives")

    # --- numbers ------------------------------------------------------
    numbers: dict = {
        "units": units,
        "round_seconds": round_seconds,
        "world_seed": seed,
        "lives": lives,
    }
    if objective == "collect" and movement in ("top_down_2d",
                                               "top_down_3d"):
        numbers["targets_per_round"] = count["used"] if count else 5
        numbers["targets_pinned"] = count is not None
        if count:
            derivations.append(
                f"targets_per_round {count['used']} — scope: "
                f"“{count['matched']}” in the {count['field']} "
                f"(a stated count pins every round)")
        else:
            derivations.append(
                "targets_per_round 5 — the factory default; the "
                "scope states no count")
    if movement in ("top_down_2d", "top_down_3d", "side_view"):
        numbers["player_speed"] = {"top_down_2d": 330.0,
                                   "top_down_3d": 11.0,
                                   "side_view": 280.0}[movement]
    if movement == "side_view":
        numbers["jump_strength"] = 545.0
        numbers["gravity"] = 1400.0
    if hazards and movement != "side_view":
        numbers["hazard_speed"] = 1.4
    if movement == "grid_step":
        numbers["move_budget_multiplier"] = 2.5

    return {
        "dimension": dimension,
        "movement": movement,
        "objective": objective,
        "fail": fail,
        "lives": lives,
        "entities": entities,
        "numbers": numbers,
        "collectible_label": label if objective == "collect"
        else collectible_label(values),
        "derivations": derivations,
        "bespoke_required": None,
    }


# ---------------------------------------------------------------------------
# Small plan accessors shared by the workers, the summary and the
# patch/repair loops.

def plan_movement(plan: dict | None) -> str:
    return str((plan or {}).get("movement") or "")


def plan_objective(plan: dict | None) -> str:
    return str((plan or {}).get("objective") or "")


def entity_kinds(plan: dict | None) -> list[str]:
    return [str(e.get("kind", "")) for e in (plan or {}).get("entities", [])
            if e.get("kind")]


def unit_word(plan: dict | None) -> str:
    return {"side_view": "levels", "grid_step": "puzzles"}.get(
        plan_movement(plan), "rounds")


def objective_hint(plan: dict, values: dict) -> str:
    """The one-line objective hint the game shows, in the
    maker's own nouns."""
    objective = plan_objective(plan)
    label = str(plan.get("collectible_label") or "Targets")
    hazard = next((e for e in plan.get("entities", [])
                   if e.get("kind") == "hazard"), None)
    goal = next((e for e in plan.get("entities", [])
                 if e.get("kind") == "goal"), None)
    hazard_bit = (f" Avoid the {hazard['label']}!"
                  if hazard else "")
    if objective == "collect":
        if goal is not None:
            return (f"Collect the {label} and reach the "
                    f"{goal['label']}."
                    + hazard_bit)
        return (f"Collect the {label} before the time "
                f"runs out." + hazard_bit)
    if objective == "reach":
        return (f"Reach the {(goal or {}).get('label', 'goal')}."
                + hazard_bit)
    if objective == "survive":
        return ("Stay alive until the time is up." + hazard_bit)
    if objective == "deliver":
        pushable = next((e for e in plan.get("entities", [])
                         if e.get("kind") == "pushable"), None)
        zone = next((e for e in plan.get("entities", [])
                     if e.get("kind") == "zone"), None)
        return (f"Push every {(pushable or {}).get('label', 'Crate')} "
                f"onto a {(zone or {}).get('label', 'Pad').rstrip('s')} "
                "— E undoes a step.")
    return "Follow the on-screen objective."


# Brain/balance tuning bounds for plan-built games, by movement.
# (The legacy per-preset bounds stay in generate.BOUNDS, where
# the patch loop reads them for pre-0.11 projects.)
PLAN_BOUNDS: dict[str, dict[str, tuple[float, float]]] = {
    "top_down_2d": {
        "player_speed": (220.0, 420.0),
        "hazard_speed": (0.6, 2.4),
        "targets_per_round": (3, 10),
    },
    "side_view": {
        "player_speed": (200.0, 360.0),
        "jump_strength": (420.0, 660.0),
        "gravity": (1100.0, 1800.0),
    },
    "grid_step": {
        "move_budget_multiplier": (1.5, 4.0),
    },
    "top_down_3d": {
        "player_speed": (7.0, 16.0),
        "hazard_speed": (0.6, 2.4),
        "targets_per_round": (3, 10),
    },
}
