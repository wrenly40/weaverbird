extends SceneTree

## Scripted player: COMPETENT (platformer). Plays to a win through
## the game's real physics and input. It reads the level's
## platform list from game_config.json — the same layout the
## player sees on screen — builds the graph of platforms a jump
## can connect (the generator's own invariants: rises up to ~80px,
## gaps up to ~150px), and plans a route to the platform its
## target star sits on. It commits to one star at a time, executes
## the route edge by edge (staged takeoffs for climbs, walk-offs
## for drops, mid-arc braking so a running jump drops onto the
## step it aimed at instead of sailing over it), and replans from
## wherever it actually lands. Reflex steering remains only as a
## fallback for the frames before a route exists.
##
## It must never hang. If it makes no progress for a few seconds
## (a star uncollected, no level change, no real approach to its
## goal) it treats that as being stuck: the route is discarded
## and replanned from its current platform. A level that defeats
## three recoveries — or the player overall — is reported as a
## loud, located failure (level, position, goal), and the whole
## run has a 90-second cap, comfortably inside the factory
## runner's kill timeout, so a failure is always the bot's own
## JSON report and never a silent kill. Prints one WBTEST JSON
## line, plus SMOKE PASS so this file also serves as the flow
## smoke.
##
## Run: godot --headless --path . --script tests/competent_win.gd

const TIMEOUT: float = 90.0
const STUCK_AFTER: float = 6.0
const MAX_RECOVERIES_PER_LEVEL: int = 3
const MAX_DEATHS: int = 6

var _phase: int = 0
var _wait: float = 0.0
var _elapsed: float = 0.0
var _game_time: float = 0.0
var _frames: int = 0
var _start_ticks: int = 0
var _load_ms: int = 0
var _menu: Node
var _done: bool = false
var _jump_cooldown: float = 0.0
var _last_x: float = 0.0
var _blocked_for: float = 0.0
var _deaths: int = 0
# Route planning over the level's platform graph.
var _cfg: Dictionary = {}
var _graph_level: int = -1
var _nodes: Array = []  # {x0, x1, top} per platform
var _adj: Dictionary = {}  # node index -> Array of node indices
var _plan: Array = []  # node indices from current platform to goal's
var _plan_target_id: int = 0
var _exec_from: int = -1  # the edge being executed: from this node
var _exec_to: int = -1  # ...to this node, until it is stood on
var _off_edge_for: float = 0.0
var _cur_node: int = -1
var _target_ref: Object = null  # the star/door committed to
var _edge_penalty: Dictionary = {}  # "from>to" -> stuck count this level
# Stuck detection / recovery.
var _no_progress: float = 0.0
var _stars_left: int = -1
var _level_seen: int = -1
var _goal_sig: String = ""
var _best_dist: float = 1e18
var _recoveries: int = 0
var _recoveries_level: int = 0
var _facing_mem: float = 1.0
var _takeoff_top: float = 1e18
# Per-unit (per-level) records for the balance lab.
var _units: Array = []
var _unit_level: int = -1
var _unit_t0: float = 0.0
var _unit_deaths0: int = 0


func _initialize() -> void:
    _start_ticks = Time.get_ticks_msec()
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
                print("SMOKE PASS: menu -> game -> every level -> win screen")
                _report(true, "")
                return true
            if _in_scene("game_over.tscn"):
                _release_all()
                _close_unit(false)
                _report(false, "the competent player ran out of lives")
                return true
            if current_scene != null and _load_ms == 0:
                _load_ms = Time.get_ticks_msec() - _start_ticks
            _steer(delta)
    return false


func _in_scene(suffix: String) -> bool:
    return current_scene != null and current_scene.scene_file_path.ends_with(suffix)


func _where() -> String:
    if not _in_scene("game.tscn"):
        return ""
    var player := current_scene.get_node_or_null("Player") as Node2D
    if player == null:
        return ""
    return " on level %d at (%d, %d)" % [
        int(current_scene.get("level_index")) + 1,
        int(player.global_position.x), int(player.global_position.y)]


func _goal() -> Dictionary:
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
    var player := current_scene.get_node_or_null("Player") as Node2D
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


func _steer(delta: float) -> void:
    if not _in_scene("game.tscn"):
        return
    var player := current_scene.get_node_or_null("Player") as CharacterBody2D
    if player == null:
        return
    _deaths = int(current_scene.get("deaths"))
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
    # Know where we stand, and keep a route to the goal's platform.
    _ensure_graph(int(current_scene.get("level_index")))
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
        # Execute the committed edge of the route.
        var step := _execute_edge(player, _nodes[_exec_from],
                                  _nodes[_exec_to], on_floor, delta)
        dir = step[0]
        hop = step[1]
    elif _cur_node >= 0 and target_node == _cur_node:
        # On the goal's own platform: walk to its column. A star
        # here is collected by walking through it; the door by
        # reaching it.
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
        # cut the direction and drop onto it. Without this, a
        # running jump's 218px arc sails clean over the step it
        # was aimed at and the bot falls back to the ground
        # between staircases. The descent case (goal on a lower
        # surface than takeoff) brakes too, so a drop lands on
        # the first surface under the arc instead of overshooting
        # it. Both comparisons are against the TAKEOFF surface,
        # never the mid-arc position: mid-arc the player rises
        # above even a higher goal, and comparing there drops the
        # bot out of legitimate climbs.
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
    var level := int(current_scene.get("level_index"))
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
    # against a baseline that is itself only ever set on the floor:
    # a trapped bot hopping in place under a high star gets closer
    # at every jump apex without actually getting anywhere, and
    # that must not reset the stuck timer. The goal signature is
    # the goal NODE's identity — positions bob (the stars float a
    # few pixels), and any position-based signature eventually
    # straddles a rounding boundary and re-arms the baseline.
    var goal_node: Object = goal.get("node")
    var sig := "node:%d" % goal_node.get_instance_id() \
        if goal_node != null else str(goal["kind"])
    if sig != _goal_sig:
        _goal_sig = sig
        _best_dist = 1e18
    if player.is_on_floor():
        var dist := player.global_position.distance_to(goal_pos)
        # The 40px band matters: a hopping bot's landing spots
        # scatter by ~30px, and each new minimum inside that
        # scatter must not count as progress.
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
        # If the edge being executed is a climb over the current
        # platform, its staging side may be shadowed by another
        # slab: try the other side after the replan. (Keyed by
        # the edge at the CURRENT node — the plan's origin entry
        # is stale by the time the bot gets stuck.)
        var at := _plan.find(_cur_node)
        if at >= 0 and at + 1 < _plan.size():
            var ekey := "%d>%d" % [int(_plan[at]), int(_plan[at + 1])]
            # And price the edge up: after a failure or two the
            # direct climb everyone keeps failing costs more
            # than the long way round (the staircase base), so
            # the next plan takes the chain instead.
            _edge_penalty[ekey] = int(_edge_penalty.get(ekey, 0)) + 1
        _plan = []  # discard the route; replan from here
        _no_progress = 0.0
        _best_dist = 1e18


func _track_unit() -> void:
    # Per-level records: a level opens when first seen; when the
    # level index moves on, the level just left was completed.
    if not _in_scene("game.tscn"):
        return
    var level := int(current_scene.get("level_index"))
    var deaths := int(current_scene.get("deaths"))
    if _unit_level < 0:
        _unit_level = level
        _unit_t0 = _game_time
        _unit_deaths0 = deaths
    elif level != _unit_level:
        _units.append({
            "unit": _unit_level + 1, "completed": true,
            "deaths": deaths - _unit_deaths0,
            "time_s": snappedf(_game_time - _unit_t0, 0.01),
        })
        _unit_level = level
        _unit_t0 = _game_time
        _unit_deaths0 = deaths


func _close_unit(completed: bool) -> void:
    if _unit_level < 0:
        return
    _units.append({
        "unit": _unit_level + 1, "completed": completed,
        "deaths": _deaths - _unit_deaths0,
        "time_s": snappedf(_game_time - _unit_t0, 0.01),
    })
    _unit_level = -1


func _fail(reason: String) -> void:
    _release_all()
    _report(false, reason)


func _ensure_graph(level: int) -> void:
    # (Re)build this level's platform graph from the level data
    # the game itself ships: game_config.json's platform list,
    # [x, top, width] per platform — ground segments first, then
    # the floating steps. Edges mirror how the generator builds
    # a level: one staircase of steps in x order over a segmented
    # ground. A mid-chain step can only be climbed from its
    # chain neighbours — from the ground, both of its takeoff
    # stagings are shadowed by the slabs of the steps beside it
    # — so ground -> step edges exist only for a chain's first
    # step. Drops are freer: anything can fall to anything lower
    # within drift range, and the ground is all one tier.
    if level == _graph_level and not _nodes.is_empty():
        return
    _graph_level = level
    _nodes = []
    _adj = {}
    _plan = []
    _cur_node = -1
    if _cfg.is_empty():
        var f := FileAccess.open("res://game_config.json",
                                 FileAccess.READ)
        if f != null:
            var parsed: Variant = JSON.parse_string(f.get_as_text())
            if typeof(parsed) == TYPE_DICTIONARY:
                _cfg = parsed
    var levels: Array = _cfg.get("levels", [])
    if level < 0 or level >= levels.size():
        return
    var plats: Array = levels[level].get("platforms", [])
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
    # Chain starts: steps with no other step just west of them
    # within jump reach — those are the steps the ground can
    # legitimately climb to.
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
        # Ground up to a step: only a chain start (see above),
        # and only within honest jump geometry.
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
    # In a staircase, dropping to the ground and climbing back
    # is almost always the wrong route — the chain along the
    # steps is — and a ground detour can also be a dead end
    # (a step whose staging from the ground is shadowed by the
    # next slab). Descents still win when they are the way
    # (a star on the ground below), just not as a shortcut.
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
    # ground segments), keep the current node — flapping between
    # them restarts the route on alternate frames.
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
    # with the reflexes that traverse these staircases: run at
    # the next platform's span, hop when a face blocks the way
    # or the current platform ends (for a climb, the edge hop +
    # mid-arc brake lands on the next step), walk off when a
    # surface continues below. Inside the target's span, keep
    # the current heading: for a step overhead that means
    # running out from under it and hopping its face; for the
    # ground underneath, walking off the current step's edge.
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
        # A climb: stage the takeoff with the surface probe. The
        # graph only offers climbs that are honestly reachable
        # (a chain start from the ground, the next step along
        # the chain), and for those the probe finds the target's
        # near edge: jump 64-150px before it, walk toward that
        # zone from further out, back up when already under it.
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
        # At the platform's edge. A real gap (> ~20px) to the
        # next platform is jumped — the floor far below the gap
        # does not count as "a surface continuing"; walking off
        # drops the bot into the gap. Only a surface that truly
        # continues below (a drop, a touching seam) is walked.
        var gap_ab := maxf(maxf(bx0 - float(a["x1"]),
                                float(a["x0"]) - bx1), 0.0)
        if gap_ab > 20.0 or not _landing_ahead(player, dir):
            hop = true
    return [dir, hop]


func _climb_step(player: CharacterBody2D, dir_sign: float) -> Dictionary:
    # Profile the surfaces ahead with downward rays — the same
    # information a player gets from looking at the screen — and
    # return the near edge of the first surface that is a real
    # step UP from the one under the player (rise 16-84px: the
    # generated jump clears ~106, and generated steps rise <= 64
    # from a neighbour, <= 80 from the ground). {} when there is
    # no such step within ~310px. Only meaningful on the floor:
    # mid-jump, "feet" are higher and mid-chain steps would
    # falsely qualify as reachable from here.
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
    # A standable surface at about this level or below, just
    # ahead: crossing it is a walk or a drop, not a pit jump.
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
    # The y of the first surface straight below the player, or -1.
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
    if _unit_level >= 0:
        _close_unit(false)  # still mid-level: it was not completed
    var fps := 0.0
    if _game_time > 0.5:
        fps = _frames / _game_time
    var level := -1
    var pos: Array = []
    var goal_pos: Array = []
    if _in_scene("game.tscn"):
        level = int(current_scene.get("level_index"))
        var player := current_scene.get_node_or_null("Player") as Node2D
        if player != null:
            pos = [snappedf(player.global_position.x, 0.1),
                   snappedf(player.global_position.y, 0.1)]
        var goal := _goal()
        if not goal.is_empty():
            var spot: Vector2 = goal["pos"]
            goal_pos = [snappedf(spot.x, 0.1), snappedf(spot.y, 0.1)]
    var data := {
        "player": "competent", "mode": "win", "ok": ok,
        "reason": reason, "time_s": snappedf(_game_time, 0.01),
        "deaths": _deaths, "fps_avg": snappedf(fps, 0.1),
        "load_ms": _load_ms, "findings": [],
        "level": level, "player_pos": pos, "goal_pos": goal_pos,
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
