extends SceneTree

## Scripted player: FIRST-TIME (generic runtime). Starts at the
## main menu and presses the real Play button, then plays using
## only what the game teaches — the on-screen hint and the
## visible entities. It decides at human-ish intervals, heads
## for the first objective entity it can see (not the optimal
## one), does not know about hazards, and in a side_view game
## jumps only when its goal is plainly higher or the ground
## runs out. In a grid game it pokes at the nearest pushable
## from whichever side it happens to reach. Records
## time-to-first-objective and whether it finished. Not
## finishing is an observation, not a failure.
##
## Run: godot --headless --path . --script tests/first_time.gd

const TIMEOUT: float = 240.0

var _phase: int = 0
var _wait: float = 0.0
var _elapsed: float = 0.0
var _game_time: float = 0.0
var _menu: Node
var _done: bool = false
var _movement: String = "top_down_2d"
var _objective: String = "collect"
var _decide_in: float = 0.0
var _direction := Vector2.ZERO
var _want_jump: bool = false
var _jump_cooldown: float = 0.0
var _last_x: float = 0.0
var _blocked_for: float = 0.0
var _first_objective_s: float = -1.0
# The stuck shuffle (top-down): an anchor position + time, and
# the sideways shuffle they trigger when a window passes with
# no real movement. See _play_top.
var _anchor_pos := Vector2(1e9, 1e9)
var _anchor_time: float = -1.0
var _shuffle: float = 0.0
var _shuffle_dir := Vector2.ZERO
var _shuffle_sign: float = 1.0
var _finished: bool = false
var _outcome: String = ""
var _deaths: int = 0
var _moves_made: int = 0
# Per-unit records for the balance lab.
var _units: Array = []
var _unit_seen: int = -1
var _unit_t0: float = 0.0
var _unit_deaths0: int = 0


func _initialize() -> void:
    if FileAccess.file_exists("res://game_config.json"):
        var parsed: Variant = JSON.parse_string(
            FileAccess.get_file_as_string("res://game_config.json"))
        if typeof(parsed) == TYPE_DICTIONARY:
            var plan: Dictionary = (parsed as Dictionary).get("plan", {})
            if typeof(plan) == TYPE_DICTIONARY:
                _movement = str(plan.get("movement", _movement))
                _objective = str(plan.get("objective", _objective))
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
    if _elapsed > TIMEOUT:
        _release_all()
        _report(true, "")
        return true
    match _phase:
        0:
            _wait += delta
            if _wait > 0.4:
                if _press_play_button():
                    _phase = 1
                else:
                    _report(false, "no Play button found on the main menu")
                    return true
        1:
            if _in_scene("win.tscn"):
                _finished = true
                _outcome = "win"
                _release_all()
                _close_unit(true)
                _report(true, "")
                return true
            if _in_scene("game_over.tscn"):
                _outcome = "lose"
                _release_all()
                _close_unit(false)
                _report(true, "")
                return true
            _game_time += delta
            _play(delta)
    return false


func _in_scene(suffix: String) -> bool:
    return current_scene != null and current_scene.scene_file_path.ends_with(suffix)


func _press_play_button() -> bool:
    return _find_button(_menu, "Play")


func _find_button(node: Node, text: String) -> bool:
    if node is Button and (node as Button).text == text:
        (node as Button).pressed.emit()
        return true
    for child in node.get_children():
        if _find_button(child, text):
            return true
    return false


func _node_pos(node: Node) -> Vector2:
    if node is Node3D:
        var p := (node as Node3D).global_position
        return Vector2(p.x, p.z)
    if node is Node2D:
        return (node as Node2D).global_position
    return Vector2.ZERO


func _track_unit() -> void:
    if not _in_scene("game.tscn"):
        return
    var index := int(current_scene.get("_unit_index"))
    var deaths_value: Variant = current_scene.get("_deaths")
    _deaths = int(deaths_value) if deaths_value != null else _deaths
    if _unit_seen < 0:
        _unit_seen = index
        _unit_t0 = _game_time
        _unit_deaths0 = _deaths
    elif index != _unit_seen:
        _units.append({
            "unit": _unit_seen + 1, "completed": true,
            "deaths": _deaths - _unit_deaths0,
            "time_s": snappedf(_game_time - _unit_t0, 0.01),
        })
        _unit_seen = index
        _unit_t0 = _game_time
        _unit_deaths0 = _deaths


func _close_unit(completed: bool) -> void:
    if _unit_seen < 0:
        return
    # The death that ends the run is counted by the game after
    # this bot's last sample; the game leaves the total in
    # RunInfo, so a lost run no longer reports zero deaths.
    _deaths = maxi(_deaths, RunInfo.deaths)
    _units.append({
        "unit": _unit_seen + 1, "completed": completed,
        "deaths": _deaths - _unit_deaths0,
        "time_s": snappedf(_game_time - _unit_t0, 0.01),
    })
    _unit_seen = -1


func _objective_count() -> int:
    # How much objective progress the game shows, in its own
    # terms: collectibles gathered, or pushables resting on
    # their zones.
    var game := current_scene
    if game == null:
        return 0
    match _movement:
        "side_view":
            return int(game.get("stars_collected"))
        "grid_step":
            var crates: Array = game.get("_crates")
            var pads: Array = game.get("_pads")
            var on := 0
            for crate in crates:
                if pads.has(crate):
                    on += 1
            return on
        _:
            return int(game.get("_total_collected"))


func _play(delta: float) -> void:
    if not _in_scene("game.tscn"):
        return
    _track_unit()
    if _first_objective_s < 0.0 and _objective_count() > 0:
        _first_objective_s = _game_time
    match _movement:
        "side_view":
            _play_side(delta)
        "grid_step":
            _play_grid(delta)
        _:
            _play_top(delta)


func _live_nodes(group_name: String) -> Array:
    var out: Array = []
    for node in get_nodes_in_group(group_name):
        var vis = node.get("visible")
        if vis == null or bool(vis):
            out.append(node)
    return out


func _play_top(delta: float) -> void:
    var player := current_scene.find_child("Player", true, false)
    if player == null:
        return
    var ppos := _node_pos(player)
    _decide_in -= delta
    if _decide_in <= 0.0:
        _decide_in = 0.35
        var goal := Vector2.ZERO
        var have := false
        # A first-timer goes for the FIRST objective it sees,
        # not the nearest, and re-decides slowly.
        var targets := _live_nodes("targets")
        if _objective != "reach" and not targets.is_empty():
            goal = _node_pos(targets[0])
            have = true
        if not have:
            var goals := _live_nodes("goal")
            if not goals.is_empty():
                goal = _node_pos(goals[0])
                have = true
            elif not targets.is_empty():
                goal = _node_pos(targets[0])
                have = true
        if _objective == "survive":
            var centre := Vector2(576, 324) \
                if _movement == "top_down_2d" else Vector2.ZERO
            goal = centre
            have = true
        var deadband := 0.6 if _movement == "top_down_3d" else 10.0
        _direction = (goal - ppos).normalized() if have \
            and goal.distance_to(ppos) > deadband else Vector2.ZERO
    # The stuck shuffle: a first-timer who pushes into something
    # solid and goes nowhere does what a person does — shuffles
    # sideways for a moment, then tries the straight line again.
    # It knows nothing about obstacles; it only notices "I asked
    # to move and barely did" over a whole window. Without it,
    # a target centred behind a block face is a stable trap:
    # the push into the face cancels exactly, the bot re-aims at
    # the same target forever, and the round clock runs out
    # with the bot standing still (the dogfood round-2 stall).
    var eff := _direction
    if _shuffle > 0.0:
        _shuffle -= delta
        eff = _shuffle_dir
    elif _direction.length() > 0.05:
        if _anchor_time < 0.0:
            _anchor_pos = ppos
            _anchor_time = _game_time
        elif _game_time - _anchor_time >= 1.2:
            var min_travel := 0.5 if _movement == "top_down_3d" \
                else 12.0
            if ppos.distance_to(_anchor_pos) < min_travel:
                _shuffle_sign = -_shuffle_sign
                _shuffle_dir = Vector2(-_direction.y, _direction.x) \
                    * _shuffle_sign
                _shuffle = 0.7
                eff = _shuffle_dir
            _anchor_pos = ppos
            _anchor_time = _game_time
    if eff.length() > 0.05:
        _set_action("move_left", eff.x < -0.35)
        _set_action("move_right", eff.x > 0.35)
        _set_action("move_up", eff.y < -0.35)
        _set_action("move_down", eff.y > 0.35)
    else:
        _release_all()


func _play_side(delta: float) -> void:
    var player := current_scene.find_child("Player", true, false) as CharacterBody2D
    if player == null:
        return
    _jump_cooldown = maxf(_jump_cooldown - delta, 0.0)
    _decide_in -= delta
    if _decide_in <= 0.0:
        _decide_in = 0.3
        var goal := Vector2.ZERO
        var have := false
        var stars := get_nodes_in_group("collectibles")
        if _objective != "reach" and not stars.is_empty():
            goal = (stars[0] as Node2D).global_position
            have = true
        if not have:
            var doors := get_nodes_in_group("exit_door")
            if not doors.is_empty():
                goal = (doors[0] as Node2D).global_position
                have = true
            elif not stars.is_empty():
                goal = (stars[0] as Node2D).global_position
                have = true
        if have:
            var dx := goal.x - player.global_position.x
            _direction = Vector2(signf(dx), 0) if absf(dx) > 12.0 \
                else Vector2.ZERO
            _want_jump = goal.y < player.global_position.y - 30.0
        else:
            _direction = Vector2.ZERO
            _want_jump = false
    var dir := _direction.x
    if player.is_on_floor() and dir != 0.0 \
            and absf(player.global_position.x - _last_x) < 0.6:
        _blocked_for += delta
    else:
        _blocked_for = 0.0
    _last_x = player.global_position.x
    _set_action("move_left", dir < 0.0)
    _set_action("move_right", dir > 0.0)
    if player.is_on_floor() and _jump_cooldown <= 0.0 \
            and (_want_jump or _blocked_for > 0.4):
        _tap_jump()
        _jump_cooldown = 0.5
        _blocked_for = 0.0


func _play_grid(delta: float) -> void:
    # Poke at the nearest pushable: walk toward the cell behind
    # it (whichever side is closest), one tap at a time, with
    # pauses to look. It will solve some puzzles and corner
    # others — both are first-timer data.
    _decide_in -= delta
    if _decide_in > 0.0 or _moves_made >= 60:
        return
    _decide_in = 0.22
    var game := current_scene
    var player: Vector2i = game.get("_player_cell")
    var crates: Array = game.get("_crates")
    var pads: Array = game.get("_pads")
    var walls: Array = game.get("_walls")
    if crates.is_empty():
        return
    var crate: Vector2i = crates[0]
    var best := 1e18
    for c in crates:
        var cell: Vector2i = c
        if pads.has(cell):
            continue
        var d: int = absi(cell.x - player.x) + absi(cell.y - player.y)
        if d < best:
            best = d
            crate = cell
    # Push toward the nearest pad: stand on the opposite side.
    var pad: Vector2i = pads[0]
    best = 1e18
    for p in pads:
        var cell: Vector2i = p
        var d: int = absi(cell.x - crate.x) + absi(cell.y - crate.y)
        if d < best:
            best = d
            pad = cell
    var push_dir := Vector2i(signi(pad.x - crate.x),
                             signi(pad.y - crate.y))
    if push_dir == Vector2i.ZERO:
        push_dir = Vector2i(1, 0)
    var stand := crate - push_dir
    var step := Vector2i.ZERO
    if player == stand:
        step = push_dir
    elif player.x != stand.x:
        step = Vector2i(signi(stand.x - player.x), 0)
    elif player.y != stand.y:
        step = Vector2i(0, signi(stand.y - player.y))
    if step == Vector2i.ZERO:
        return
    var next := player + step
    if walls.has(next):
        step = Vector2i(step.y, step.x)  # try the other axis
        next = player + step
        if walls.has(next):
            return
    var key := 0
    if step == Vector2i(1, 0):
        key = KEY_D
    elif step == Vector2i(-1, 0):
        key = KEY_A
    elif step == Vector2i(0, 1):
        key = KEY_S
    elif step == Vector2i(0, -1):
        key = KEY_W
    if key != 0:
        _tap(key)
        _moves_made += 1


func signi(value: int) -> int:
    return 1 if value > 0 else (-1 if value < 0 else 0)


func _tap(keycode: int) -> void:
    var press := InputEventKey.new()
    press.physical_keycode = keycode
    press.pressed = true
    Input.parse_input_event(press)
    var release := InputEventKey.new()
    release.physical_keycode = keycode
    release.pressed = false
    Input.parse_input_event(release)


func _tap_jump() -> void:
    _tap(KEY_SPACE)


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
    _deaths = maxi(_deaths, RunInfo.deaths)
    var data := {
        "player": "first_time", "mode": "explore", "ok": ok,
        "reason": reason, "time_s": snappedf(_game_time, 0.01),
        "deaths": _deaths,
        "first_objective_s": snappedf(_first_objective_s, 0.01),
        "finished": _finished, "outcome": _outcome,
        "findings": [], "units": _units,
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
