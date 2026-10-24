extends SceneTree

## Scripted player: COMPETENT (generic runtime). Plays any
## plan-built game to a win through the game's real logic — it
## steers with the real input actions, reads only what a player
## could see (entity positions, the world data the game itself
## ships), and never touches game state. How it plays follows
## the plan:
##
##   top_down (2D or 3D) — routes to the objective entities:
##     collect the targets (then the goal, when the plan has
##     one), reach the goal, or survive by orbiting the middle
##     and dodging hazards.
##   side_view — a route planner over the level's platform
##     graph (built from the world data): commits to one star
##     at a time, executes the route edge by edge with staged
##     takeoffs and mid-arc braking, replans when stuck. For a
##     reach plan it heads straight for the door.
##   grid_step — replays each puzzle's verified solution (the
##     world data carries it) as real key events, so every push
##     goes through the game's own rules.
##
## Prints one WBTEST JSON line with per-unit records for the
## balance lab, plus SMOKE PASS so this file also serves as the
## project's flow smoke.
##
## Run: godot --headless --path . --script tests/competent_win.gd

const TIMEOUT: float = 420.0
const STUCK_AFTER: float = 6.0
const MAX_RECOVERIES_PER_LEVEL: int = 3
const MAX_DEATHS: int = 6
const KEY_FOR := {"u": KEY_W, "d": KEY_S, "l": KEY_A, "r": KEY_D}

var _phase: int = 0
var _wait: float = 0.0
var _elapsed: float = 0.0
var _game_time: float = 0.0
var _frames: int = 0
var _start_ticks: int = 0
var _load_ms: int = 0
var _menu: Node
var _done: bool = false
var _deaths: int = 0
var _movement: String = "top_down_2d"
var _objective: String = "collect"
var _world_units: Array = []
# Per-unit records for the balance lab.
var _units: Array = []
var _unit_seen: int = -1
var _unit_t0: float = 0.0
var _unit_deaths0: int = 0
var _cur_moves: int = 0
var _cur_budget: int = 0
# --- top-down 3D obstacle detour state ---
var _top_last_pos := Vector2(-9999, -9999)
var _top_stuck_for: float = 0.0
var _top_detour: float = 0.0
var _top_detour_dir := Vector2.ZERO
var _top_detour_sign: float = 1.0
# --- side_view planner state ---
var _jump_cooldown: float = 0.0
var _last_x: float = 0.0
var _blocked_for: float = 0.0
var _graph_level: int = -1
var _nodes: Array = []
var _adj: Dictionary = {}
var _plan: Array = []
var _plan_target_id: int = 0
var _exec_from: int = -1
var _exec_to: int = -1
var _off_edge_for: float = 0.0
var _cur_node: int = -1
var _target_ref: Object = null
var _edge_penalty: Dictionary = {}
var _no_progress: float = 0.0
var _stars_left: int = -1
var _level_seen: int = -1
var _goal_sig: String = ""
var _best_dist: float = 1e18
var _recoveries: int = 0
var _recoveries_level: int = 0
var _facing_mem: float = 1.0
var _takeoff_top: float = 1e18
# --- grid_step replay state ---
var _solutions: Array = []
var _moves: Array = []
var _move_index: int = 0
var _step_wait: float = 0.0
var _step_stage: int = 0
var _advance_wait: float = 0.0


func _initialize() -> void:
    _start_ticks = Time.get_ticks_msec()
    if FileAccess.file_exists("res://game_config.json"):
        var parsed: Variant = JSON.parse_string(
            FileAccess.get_file_as_string("res://game_config.json"))
        if typeof(parsed) == TYPE_DICTIONARY:
            var plan: Dictionary = (parsed as Dictionary).get("plan", {})
            if typeof(plan) == TYPE_DICTIONARY:
                _movement = str(plan.get("movement", _movement))
                _objective = str(plan.get("objective", _objective))
    if FileAccess.file_exists("res://world/world.json"):
        var wparsed: Variant = JSON.parse_string(
            FileAccess.get_file_as_string("res://world/world.json"))
        if typeof(wparsed) == TYPE_DICTIONARY:
            _world_units = (wparsed as Dictionary).get("units", [])
    for unit in _world_units:
        _solutions.append(str((unit as Dictionary).get("solution", "")))
    var packed: PackedScene = load("res://scenes/main_menu.tscn")
    if packed == null:
        _report(false, "main_menu.tscn failed to load")
        return
    _menu = packed.instantiate()
    root.add_child(_menu)


func _process(delta: float) -> bool:
    if _done:
        return true
    _elapsed += delta
    if _phase == 1:
        _frames += 1
        _game_time += delta
    if _elapsed > TIMEOUT:
        _release_all()
        _report(false, "timed out after %d seconds%s" % [
            int(TIMEOUT), _where()])
        return true
    match _phase:
        0:
            _wait += delta
            if _wait > 0.3:
                _menu.call("start_game")
                _phase = 1
        1:
            if _in_scene("win.tscn"):
                _release_all()
                _close_unit(true)
                print("SMOKE PASS: menu -> game -> plan played to a win")
                _report(true, "")
                return true
            if _in_scene("game_over.tscn"):
                _release_all()
                _close_unit(false)
                _report(false, "the competent player lost the run")
                return true
            if current_scene != null and _load_ms == 0:
                _load_ms = Time.get_ticks_msec() - _start_ticks
            match _movement:
                "side_view":
                    _steer_side(delta)
                "grid_step":
                    _play_grid(delta)
                _:
                    _play_top(delta)
    return false


func _in_scene(suffix: String) -> bool:
    return current_scene != null and current_scene.scene_file_path.ends_with(suffix)


func _where() -> String:
    if not _in_scene("game.tscn"):
        return ""
    var player := current_scene.find_child("Player", true, false)
    if player == null:
        return ""
    var spot := _node_pos(player)
    return " on unit %d at (%d, %d)" % [
        int(current_scene.get("_unit_index")) + 1,
        int(spot.x), int(spot.y)]


func _node_pos(node: Node) -> Vector2:
    # One position type for both dimensions: (x, y) in 2D,
    # (x, z) on the 3D ground plane.
    if node is Node3D:
        var p := (node as Node3D).global_position
        return Vector2(p.x, p.z)
    if node is Node2D:
        return (node as Node2D).global_position
    return Vector2.ZERO


func _scene_deaths() -> int:
    if current_scene == null:
        return _deaths
    var value: Variant = current_scene.get("_deaths")
    return int(value) if value != null else _deaths


func _track_unit() -> void:
    # Per-unit records: a unit opens when first seen; when the
    # unit index moves on, the unit just left was completed.
    if not _in_scene("game.tscn"):
        return
    var index := int(current_scene.get("_unit_index"))
    _deaths = _scene_deaths()
    if _unit_seen < 0:
        _unit_seen = index
        _unit_t0 = _game_time
        _unit_deaths0 = _deaths
    elif index != _unit_seen:
        _append_unit(_unit_seen, true)
        _unit_seen = index
        _unit_t0 = _game_time
        _unit_deaths0 = _deaths
        _cur_moves = 0
    elif _movement == "grid_step":
        # Sampled while the unit is unchanged: the last sample
        # before the index moves is the finished unit's count
        # (it resets to 0 the moment the next unit loads).
        _cur_moves = int(current_scene.get("moves_used"))
        _cur_budget = int(current_scene.get("move_budget"))


func _append_unit(index: int, completed: bool) -> void:
    var record := {
        "unit": index + 1, "completed": completed,
        "deaths": _deaths - _unit_deaths0,
        "time_s": snappedf(_game_time - _unit_t0, 0.01),
    }
    if _movement == "grid_step":
        record["moves_used"] = _cur_moves
        record["move_budget"] = _cur_budget
    _units.append(record)


func _close_unit(completed: bool) -> void:
    if _unit_seen < 0:
        return
    _deaths = _scene_deaths()
    _append_unit(_unit_seen, completed)
    _unit_seen = -1


# ---------------------------------------------------------------------------
# top_down (2D and 3D)

func _live_nodes(group_name: String) -> Array:
    # Group members that are still in play: a collected entity
    # may be hidden for a frame before it leaves its group.
    var out: Array = []
    for node in get_nodes_in_group(group_name):
        var vis = node.get("visible")
        if vis == null or bool(vis):
            out.append(node)
    return out


func _play_top(delta: float) -> void:
    if not _in_scene("game.tscn"):
        return
    _track_unit()
    var player := current_scene.find_child("Player", true, false)
    if player == null:
        return
    var ppos := _node_pos(player)
    var goal := Vector2.ZERO
    var have_goal := false
    if _objective == "survive":
        # Orbit the middle of the ground, dodging what comes.
        var centre := Vector2(576, 324) if _movement == "top_down_2d" \
            else Vector2.ZERO
        var radius := 170.0 if _movement == "top_down_2d" else 8.0
        goal = centre + Vector2(cos(_game_time * 0.7),
                                sin(_game_time * 0.7)) * radius
        have_goal = true
    else:
        var targets := _live_nodes("targets")
        if _objective == "collect" and not targets.is_empty():
            var best := 1e18
            for target in targets:
                var spot := _node_pos(target)
                var dist := ppos.distance_squared_to(spot)
                if dist < best:
                    best = dist
                    goal = spot
            have_goal = true
        if not have_goal:
            var goals := _live_nodes("goal")
            if not goals.is_empty():
                goal = _node_pos(goals[0])
                have_goal = true
            elif not targets.is_empty():
                var best := 1e18
                for target in targets:
                    var spot := _node_pos(target)
                    var dist := ppos.distance_squared_to(spot)
                    if dist < best:
                        best = dist
                        goal = spot
                have_goal = true
    if not have_goal:
        _release_all()
        return
    var is_3d := _movement == "top_down_3d"
    var deadband := 0.4 if is_3d else 8.0
    var direction := goal - ppos
    if direction.length() > deadband:
        direction = direction.normalized()
    else:
        direction = Vector2.ZERO
    # Hazard awareness: veer away from a hazard that is close
    # and ahead.
    for hazard in get_nodes_in_group("hazards"):
        var spot := _node_pos(hazard)
        var away := ppos - spot
        var dist := away.length()
        var close := 110.0 if not is_3d else 3.6
        if dist < close and dist > 0.05:
            direction += away.normalized() * (close - dist) / close * 1.6
    if is_3d:
        # Obstacle awareness: the world data carries the blocks
        # (centre + size) — veer around them, and if wedged
        # anyway, detour sideways for a beat like a player does.
        var index := int(current_scene.get("_unit_index"))
        if index >= 0 and index < _world_units.size():
            var unit: Dictionary = _world_units[index]
            for raw in unit.get("obstacles", []):
                var obstacle: Dictionary = raw
                var pos: Array = obstacle.get("pos", [0, 0])
                var size: Array = obstacle.get("size", [2, 2])
                var spot := Vector2(float(pos[0]), float(pos[1]))
                var reach := maxf(float(size[0]), float(size[1])) * 0.5 + 1.1
                var away := ppos - spot
                var dist := away.length()
                if dist < reach + 1.6 and dist > 0.05:
                    direction += away.normalized() \
                        * (reach + 1.6 - dist) / (reach + 1.6) * 1.8
        if direction.length() > 0.05 \
                and ppos.distance_to(_top_last_pos) < 0.03:
            _top_stuck_for += delta
        else:
            _top_stuck_for = 0.0
        _top_last_pos = ppos
        if _top_detour > 0.0:
            _top_detour -= delta
            direction = _top_detour_dir
        elif _top_stuck_for > 0.6 and (goal - ppos).length() > deadband:
            _top_detour_sign = -_top_detour_sign
            var forward := (goal - ppos).normalized()
            _top_detour_dir = Vector2(-forward.y, forward.x) \
                * _top_detour_sign
            _top_detour = 0.8
            _top_stuck_for = 0.0
            direction = _top_detour_dir
    if direction.length() > 0.05:
        direction = direction.normalized()
        _apply_direction(direction)
    else:
        _release_all()


func _apply_direction(direction: Vector2) -> void:
    _set_action("move_left", direction.x < -0.35)
    _set_action("move_right", direction.x > 0.35)
    _set_action("move_up", direction.y < -0.35)
    _set_action("move_down", direction.y > 0.35)


# ---------------------------------------------------------------------------
# grid_step: replay the verified solutions as real key events

func _play_grid(delta: float) -> void:
    if not _in_scene("game.tscn"):
        return
    var index := int(current_scene.get("_unit_index"))
    if index != _unit_seen:
        _track_unit()
        _moves.clear()
        if index < _solutions.size():
            for ch in _solutions[index].split(""):
                if ch != "":
                    _moves.append(ch)
        _move_index = 0
        _step_stage = 0
        _step_wait = 0.2
        _advance_wait = 0.0
    else:
        _track_unit()
    if _move_index >= _moves.size():
        # Solution played; the game should advance on its own.
        _advance_wait += delta
        if _advance_wait > 4.0:
            _report(false,
                "solution for puzzle %d did not solve it" % (index + 1))
        return
    _step_wait -= delta
    if _step_wait > 0.0:
        return
    match _step_stage:
        0:
            _tap(KEY_FOR[_moves[_move_index]], true)
            _step_stage = 1
            _step_wait = 0.07
        1:
            _tap(KEY_FOR[_moves[_move_index]], false)
            _move_index += 1
            _step_stage = 0
            _step_wait = 0.11


func _tap(keycode: int, pressed: bool) -> void:
    var event := InputEventKey.new()
    event.physical_keycode = keycode
    event.pressed = pressed
    Input.parse_input_event(event)


# ---------------------------------------------------------------------------
# side_view: the route planner over the level's platform graph

func _goal() -> Dictionary:
    if _objective == "reach":
        var doors := get_nodes_in_group("exit_door")
        if not doors.is_empty():
            _target_ref = doors[0]
            return {"pos": (doors[0] as Node2D).global_position,
                    "kind": "door", "node": doors[0]}
    var stars := get_nodes_in_group("collectibles")
    # Commit to one target at a time: re-picking the "best" star
    # every frame flips the target as the player crosses star
    # columns, and each flip re-arms the progress baseline — the
    # bot can oscillate between two stars' basins forever. The
    # committed star stays the goal until it is collected.
    if _target_ref != null and is_instance_valid(_target_ref) \
            and stars.has(_target_ref):
        var held := _target_ref as Node2D
        return {"pos": held.global_position, "kind": "star", "node": held}
    var player := current_scene.find_child("Player", true, false) as Node2D
    var best: Node2D = null
    var best_key := 1e18
    for star in stars:
        var spot := (star as Node2D).global_position
        var key := spot.x * 10000.0 + absf(spot.y)
        if player != null and spot.x < player.global_position.x - 40.0:
            key += 1e12  # prefer stars still ahead; sweep back if needed
        if key < best_key:
            best_key = key
            best = star
    if best != null:
        _target_ref = best
        return {"pos": best.global_position, "kind": "star", "node": best}
    var doors := get_nodes_in_group("exit_door")
    if not doors.is_empty():
        _target_ref = doors[0]
        return {"pos": (doors[0] as Node2D).global_position,
                "kind": "door", "node": doors[0]}
    return {}


func _steer_side(delta: float) -> void:
    if not _in_scene("game.tscn"):
        return
    var player := current_scene.find_child("Player", true, false) as CharacterBody2D
    if player == null:
        return
    _deaths = _scene_deaths()
    _track_unit()
    if _deaths >= MAX_DEATHS:
        _fail("the competent player died %d times%s" % [_deaths, _where()])
        return
    var goal := _goal()
    if goal.is_empty():
        _release_all()
        return
    var goal_pos: Vector2 = goal["pos"]
    _track_progress(delta, player, goal, goal_pos)
    if _done:
        return
    _jump_cooldown = maxf(_jump_cooldown - delta, 0.0)
    var on_floor := player.is_on_floor()
    var dx := goal_pos.x - player.global_position.x
    var dir := 0.0
    var hop := false
    _ensure_graph(int(current_scene.get("_unit_index")))
    if on_floor:
        var standing := _node_at(player)
        if standing >= 0:
            _cur_node = standing
    var target_node := _goal_node(goal)
    var target_id := int((goal["node"] as Object).get_instance_id()) \
        if target_node >= 0 else 0
    # Committed edges: an edge, once started, keeps its original
    # endpoints until its target is stood on. Replanning every
    # frame flaps at platform boundaries — adjacent ground
    # segments hand the bot back and forth between a travel edge
    # and a climb edge, and it crosses the boundary forever.
    if _plan.is_empty():
        _exec_from = -1
        _exec_to = -1
    var need_plan := false
    if _exec_to < 0:
        need_plan = _cur_node >= 0 and target_node >= 0 \
            and _cur_node != target_node
    elif target_id != _plan_target_id:
        need_plan = true
    elif _cur_node == _exec_to:
        _plan.pop_front()  # the finished edge leaves the queue
        _exec_from = _exec_to
        _exec_to = int(_plan[1]) if _plan.size() >= 2 else -1
        _off_edge_for = 0.0
    elif _cur_node >= 0 and _cur_node != _exec_from:
        # Standing on a third platform mid-edge (an overshoot, a
        # lucky landing): give the edge a moment, then replan.
        _off_edge_for += delta
        if _off_edge_for > 0.75:
            need_plan = true
    else:
        _off_edge_for = 0.0
    if need_plan and _cur_node >= 0 and target_node >= 0:
        _plan = _find_path(_cur_node, target_node)
        _plan_target_id = target_id
        _exec_from = _cur_node
        _exec_to = int(_plan[1]) if _plan.size() >= 2 else -1
        _off_edge_for = 0.0
    if _exec_to >= 0 and _plan.size() >= 2:
        var step := _execute_edge(player, _nodes[_exec_from],
                                  _nodes[_exec_to], on_floor, delta)
        dir = step[0]
        hop = step[1]
    elif _cur_node >= 0 and target_node == _cur_node:
        if absf(dx) > 10.0:
            dir = signf(dx)
    else:
        # No route (yet): reflex fallback — head for the goal,
        # hop pits, never hop toward a distant height.
        if absf(dx) > 10.0:
            dir = signf(dx)
        if on_floor and dir != 0.0 \
                and absf(player.global_position.x - _last_x) < 0.6:
            _blocked_for += delta
        else:
            _blocked_for = 0.0
        if on_floor and _jump_cooldown <= 0.0:
            if _blocked_for > 0.22:
                hop = true
            elif dir != 0.0 and not _ground_ahead(player, dir) \
                    and not _landing_ahead(player, dir):
                hop = true
            elif goal_pos.y < player.global_position.y - 26.0 \
                    and absf(dx) <= 60.0:
                hop = true
    if on_floor:
        _takeoff_top = player.global_position.y + 18.0
    elif player.velocity.y > 60.0 and dir != 0.0:
        # Braking: falling toward a nearby surface that is
        # progress — higher than the one this arc took off from —
        # cut the direction and drop onto it. Both comparisons
        # are against the TAKEOFF surface, never the mid-arc
        # position.
        var below := _surface_below(player)
        if below > 0.0 \
                and below - (player.global_position.y + 18.0) < 130.0 \
                and (below < _takeoff_top - 8.0
                     or (goal_pos.y > _takeoff_top
                         and below > _takeoff_top)):
            dir = 0.0
    _last_x = player.global_position.x
    if dir != 0.0:
        _facing_mem = dir
    _set_action("move_left", dir < 0.0)
    _set_action("move_right", dir > 0.0)
    if hop and _jump_cooldown <= 0.0:
        _tap_jump()
        _jump_cooldown = 0.3
        _blocked_for = 0.0


func _track_progress(delta: float, player: CharacterBody2D,
        goal: Dictionary, goal_pos: Vector2) -> void:
    var progressed := false
    var level := int(current_scene.get("_unit_index"))
    if level != _level_seen:
        _level_seen = level
        _recoveries_level = 0
        _edge_penalty = {}
        progressed = true
    var stars_left := get_nodes_in_group("collectibles").size()
    if _stars_left >= 0 and stars_left < _stars_left:
        progressed = true
    _stars_left = stars_left
    # Distance counts as progress only when measured on the floor,
    # against a baseline that is itself only ever set on the floor.
    var goal_node: Object = goal.get("node")
    var sig := "node:%d" % goal_node.get_instance_id() \
        if goal_node != null else str(goal["kind"])
    if sig != _goal_sig:
        _goal_sig = sig
        _best_dist = 1e18
    if player.is_on_floor():
        var dist := player.global_position.distance_to(goal_pos)
        if dist < _best_dist - 40.0:
            _best_dist = dist
            progressed = true
        elif _best_dist >= 1e17:
            _best_dist = dist
    if progressed:
        _no_progress = 0.0
        return
    _no_progress += delta
    if _no_progress > STUCK_AFTER:
        _recoveries += 1
        _recoveries_level += 1
        if _recoveries_level > MAX_RECOVERIES_PER_LEVEL:
            _fail("stuck on level %d after %d recoveries%s — goal %s "
                    % [level + 1, MAX_RECOVERIES_PER_LEVEL, _where(),
                       str(goal["kind"])]
                    + "at (%d, %d)" % [int(goal_pos.x), int(goal_pos.y)])
            return
        var at := _plan.find(_cur_node)
        if at >= 0 and at + 1 < _plan.size():
            var ekey := "%d>%d" % [int(_plan[at]), int(_plan[at + 1])]
            _edge_penalty[ekey] = int(_edge_penalty.get(ekey, 0)) + 1
        _plan = []  # discard the route; replan from here
        _no_progress = 0.0
        _best_dist = 1e18


func _fail(reason: String) -> void:
    _release_all()
    _report(false, reason)


func _ensure_graph(level: int) -> void:
    # (Re)build this level's platform graph from the world data
    # the game itself ships: the level's platform list,
    # [x, top, width] per platform — ground segments first, then
    # the floating steps. Edges mirror how the generator builds
    # a level: one staircase of steps in x order over a segmented
    # ground. A mid-chain step can only be climbed from its chain
    # neighbours — from the ground, both of its takeoff stagings
    # are shadowed by the slabs of the steps beside it — so
    # ground -> step edges exist only for a chain's first step.
    # Drops are freer: anything can fall to anything lower within
    # drift range, and the ground is all one tier.
    if level == _graph_level and not _nodes.is_empty():
        return
    _graph_level = level
    _nodes = []
    _adj = {}
    _plan = []
    _cur_node = -1
    if level < 0 or level >= _world_units.size():
        return
    var plats: Array = _world_units[level].get("platforms", [])
    var ground_top := 0.0
    for p in plats:
        ground_top = maxf(ground_top, float(p[1]))
        _nodes.append({"x0": float(p[0]),
                       "x1": float(p[0]) + float(p[2]),
                       "top": float(p[1])})
    var steps: Array = []
    for i in _nodes.size():
        if float(_nodes[i]["top"]) < ground_top - 1.0:
            steps.append(i)
    steps.sort_custom(func(a: int, b: int) -> bool:
        return float(_nodes[a]["x0"]) < float(_nodes[b]["x0"]))
    var starts: Dictionary = {}
    for pos in steps.size():
        var j := int(steps[pos])
        var is_start := true
        for k in steps:
            var c: Dictionary = _nodes[int(k)]
            if int(k) != j and float(c["x1"]) <= float(_nodes[j]["x0"]) + 2.0 \
                    and float(_nodes[j]["x0"]) - float(c["x1"]) <= 170.0 \
                    and absf(float(c["top"]) - float(_nodes[j]["top"])) <= 106.0:
                is_start = false
        starts[j] = is_start
    for i in _nodes.size():
        var lst: Array = []
        for j in _nodes.size():
            if i != j and _edge_ok(i, j, ground_top, steps, starts):
                lst.append(j)
        _adj[i] = lst


func _gap(a: Dictionary, b: Dictionary) -> float:
    return maxf(maxf(float(b["x0"]) - float(a["x1"]),
                     float(a["x0"]) - float(b["x1"])), 0.0)


func _edge_ok(i: int, j: int, ground_top: float, steps: Array,
        starts: Dictionary) -> bool:
    # Directed: can the player get from platform i to platform j?
    var a: Dictionary = _nodes[i]
    var b: Dictionary = _nodes[j]
    var gap := _gap(a, b)
    var rise := float(a["top"]) - float(b["top"])
    var a_ground := float(a["top"]) >= ground_top - 1.0
    var b_ground := float(b["top"]) >= ground_top - 1.0
    if a_ground and b_ground:
        return gap <= 150.0
    if b_ground:
        return gap <= 150.0  # anything can drop to the ground
    if a_ground:
        # Ground up to a step: only a chain start, and only
        # within honest jump geometry.
        return bool(starts.get(j, false)) and rise <= 80.0 \
            and gap <= 140.0
    # Step to step.
    if rise >= 0.0:
        # A climb: only along the chain (x-adjacent steps).
        var pos := steps.find(i)
        return rise <= 80.0 and gap <= 150.0 and pos >= 0 \
            and pos + 1 < steps.size() and int(steps[pos + 1]) == j
    # A drop to a lower step: adjacent or a clear fall past one.
    return gap <= 120.0


func _find_path(from_node: int, to_node: int) -> Array:
    # Cheapest route, not fewest hops: a descent costs extra.
    if from_node == to_node:
        return [from_node]
    var dist: Dictionary = {from_node: 0.0}
    var prev: Dictionary = {}
    var done: Dictionary = {}
    while true:
        var cur := -1
        var best := 1e18
        for n in dist.keys():
            if not done.has(n) and float(dist[n]) < best:
                best = float(dist[n])
                cur = int(n)
        if cur == -1:
            return []
        if cur == to_node:
            break
        done[cur] = true
        for nxt in _adj.get(cur, []):
            var m := int(nxt)
            var w := 1.0
            var r := float(_nodes[cur]["top"]) - float(_nodes[m]["top"])
            if r < -14.0:
                w = 2.5
            w += 6.0 * int(_edge_penalty.get("%d>%d" % [cur, m], 0))
            var nd := best + w
            if not dist.has(m) or nd < float(dist[m]):
                dist[m] = nd
                prev[m] = cur
    var path: Array = [to_node]
    var back := to_node
    while back != from_node:
        back = int(prev.get(back, -1))
        if back == -1:
            return []
        path.push_front(back)
    return path


func _node_at(player: CharacterBody2D) -> int:
    # The platform whose top the player is standing on.
    var feet := player.global_position.y + 18.0
    var x := player.global_position.x
    var best := -1
    var best_err := 1e18
    for i in _nodes.size():
        var n: Dictionary = _nodes[i]
        if x < float(n["x0"]) - 14.0 or x > float(n["x1"]) + 14.0:
            continue
        var err := absf(float(n["top"]) - feet)
        if err < best_err:
            best_err = err
            best = i
    if best_err > 5.0:
        return -1
    # Hysteresis: where two platforms both fit (the seam between
    # ground segments), keep the current node.
    if _cur_node >= 0 and _cur_node < _nodes.size():
        var c: Dictionary = _nodes[_cur_node]
        if x >= float(c["x0"]) - 14.0 and x <= float(c["x1"]) + 14.0 \
                and absf(float(c["top"]) - feet) <= 5.0:
            return _cur_node
    return best


func _goal_node(goal: Dictionary) -> int:
    # The platform a goal sits on: a star hovers 30px over its
    # platform; the exit door stands 44px over its.
    var pos: Vector2 = goal["pos"]
    var lift := 30.0 if str(goal["kind"]) == "star" else 44.0
    var want_top := pos.y + lift
    var best := -1
    var best_err := 1e18
    for i in _nodes.size():
        var n: Dictionary = _nodes[i]
        if pos.x < float(n["x0"]) - 18.0 \
                or pos.x > float(n["x1"]) + 18.0:
            continue
        var err := absf(float(n["top"]) - want_top)
        if err < best_err:
            best_err = err
            best = i
    return best if best_err <= 10.0 else -1


func _execute_edge(player: CharacterBody2D, a: Dictionary,
        b: Dictionary, on_floor: bool, delta: float) -> Array:
    # One planned move between neighbouring platforms, executed
    # with the reflexes that traverse these staircases.
    var x := player.global_position.x
    var bx0 := float(b["x0"])
    var bx1 := float(b["x1"])
    var rise := float(a["top"]) - float(b["top"])
    var dir := _facing_mem
    if x < bx0 - 6.0:
        dir = 1.0
    elif x > bx1 + 6.0:
        dir = -1.0
    if not on_floor:
        return [dir, false]
    var hop := false
    if absf(x - _last_x) < 0.6:
        _blocked_for += delta
    else:
        _blocked_for = 0.0
    if rise > 14.0:
        # A climb: stage the takeoff with the surface probe.
        var climb := _climb_step(player, dir)
        if not climb.is_empty():
            var rel := (float(climb["edge"]) - x) * dir
            if rel > 150.0:
                hop = _blocked_for > 0.25 \
                        or (not _ground_ahead(player, dir)
                            and not _landing_ahead(player, dir))
            elif rel >= 64.0:
                hop = true
            else:
                dir = -dir
            return [dir, hop]
    if _blocked_for > 0.25:
        hop = true
    elif dir != 0.0 and not _ground_ahead(player, dir):
        # At the platform's edge. A real gap to the next
        # platform is jumped; only a surface that truly
        # continues below is walked off.
        var gap_ab := maxf(maxf(bx0 - float(a["x1"]),
                                float(a["x0"]) - bx1), 0.0)
        if gap_ab > 20.0 or not _landing_ahead(player, dir):
            hop = true
    return [dir, hop]


func _climb_step(player: CharacterBody2D, dir_sign: float) -> Dictionary:
    # Profile the surfaces ahead with downward rays and return
    # the near edge of the first surface that is a real step UP
    # from the one under the player (rise 16-84px). {} when
    # there is no such step within ~310px.
    if not player.is_on_floor():
        return {}
    var space := player.get_world_2d().direct_space_state
    var feet := player.global_position.y + 18.0
    var groups: Array = []
    var cur_top := 1e18
    var cur_edge := 0.0
    var gx := player.global_position.x + dir_sign * 10.0
    for i in 21:
        var from := Vector2(gx, player.global_position.y - 340.0)
        var to := Vector2(gx, feet + 40.0)
        var query := PhysicsRayQueryParameters2D.create(from, to)
        query.exclude = [player.get_rid()]
        var hit := space.intersect_ray(query)
        var top := 1e18
        if not hit.is_empty():
            top = (hit["position"] as Vector2).y
        if top < 1e17 and absf(top - cur_top) <= 3.0:
            pass  # same surface continues
        else:
            if cur_top < 1e17:
                groups.append({"edge": cur_edge, "top": cur_top})
            cur_top = top
            cur_edge = gx
        gx += dir_sign * 15.0
    if cur_top < 1e17:
        groups.append({"edge": cur_edge, "top": cur_top})
    for group in groups:
        var rise := feet - float(group["top"])
        if rise >= 16.0 and rise <= 84.0:
            return group
    return {}


func _landing_ahead(player: CharacterBody2D, facing: float) -> bool:
    var space := player.get_world_2d().direct_space_state
    var feet := player.global_position.y + 18.0
    var from := player.global_position + Vector2(34.0 * facing, 8.0)
    var to := from + Vector2(0, 200.0)
    var query := PhysicsRayQueryParameters2D.create(from, to)
    query.exclude = [player.get_rid()]
    var hit := space.intersect_ray(query)
    if hit.is_empty():
        return false
    return (hit["position"] as Vector2).y > feet - 14.0


func _surface_below(player: CharacterBody2D) -> float:
    var space := player.get_world_2d().direct_space_state
    var from := player.global_position + Vector2(0, 10.0)
    var to := from + Vector2(0, 190.0)
    var query := PhysicsRayQueryParameters2D.create(from, to)
    query.exclude = [player.get_rid()]
    var hit := space.intersect_ray(query)
    if hit.is_empty():
        return -1.0
    return (hit["position"] as Vector2).y


func _ground_ahead(player: CharacterBody2D, facing: float) -> bool:
    var space := player.get_world_2d().direct_space_state
    var from := player.global_position + Vector2(facing * 30.0, 4.0)
    var to := from + Vector2(0, 54.0)
    var query := PhysicsRayQueryParameters2D.create(from, to)
    query.exclude = [player.get_rid()]
    var hit := space.intersect_ray(query)
    return not hit.is_empty()


func _tap_jump() -> void:
    var press := InputEventKey.new()
    press.physical_keycode = KEY_SPACE
    press.pressed = true
    Input.parse_input_event(press)
    var release := InputEventKey.new()
    release.physical_keycode = KEY_SPACE
    release.pressed = false
    Input.parse_input_event(release)


# ---------------------------------------------------------------------------

func _set_action(action: String, pressed: bool) -> void:
    if pressed:
        Input.action_press(action)
    else:
        Input.action_release(action)


func _release_all() -> void:
    for action in ["move_left", "move_right", "move_up", "move_down"]:
        Input.action_release(action)


func _report(ok: bool, reason: String) -> void:
    _done = true
    if _unit_seen >= 0:
        _close_unit(false)  # still mid-unit: it was not completed
    var fps := 0.0
    if _game_time > 0.5:
        fps = _frames / _game_time
    var data := {
        "player": "competent", "mode": "win", "ok": ok,
        "reason": reason, "time_s": snappedf(_game_time, 0.01),
        "deaths": _deaths, "fps_avg": snappedf(fps, 0.1),
        "load_ms": _load_ms, "findings": [],
        "recoveries": _recoveries, "units": _units,
    }
    _silence()
    OS.delay_msec(400)
    print("WBTEST " + JSON.stringify(data))
    quit(0 if ok else 1)


func _silence() -> void:
    # Stop any playing audio before quitting: under the headless
    # dummy audio driver, a playback left running is reported as a
    # leaked resource at exit, which would read as a game error.
    var stack: Array = [root]
    while not stack.is_empty():
        var node: Node = stack.pop_back()
        if node is AudioStreamPlayer:
            (node as AudioStreamPlayer).stop()
        for child in node.get_children():
            stack.append(child)
