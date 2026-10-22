extends SceneTree

## Scripted player: COMPETENT (platformer). Plays to a win through
## the game's real physics and input: it runs with the move actions
## and taps jump (real key events) when its goal is higher, when a
## wall blocks it, or when the ground ahead runs out — sensing the
## world with the same kind of ray a player gets from looking at
## the screen. It gathers the stars in left-to-right order and takes
## the exit door of every level. Prints one WBTEST JSON line, plus
## SMOKE PASS so this file also serves as the flow smoke.
##
## Run: godot --headless --path . --script tests/competent_win.gd

const TIMEOUT: float = 300.0

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
        _report(false, "timed out after %d seconds" % int(TIMEOUT))
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
                print("SMOKE PASS: menu -> game -> every level -> win screen")
                _report(true, "")
                return true
            if _in_scene("game_over.tscn"):
                _release_all()
                _report(false, "the competent player ran out of lives")
                return true
            if current_scene != null and _load_ms == 0:
                _load_ms = Time.get_ticks_msec() - _start_ticks
            _steer(delta)
    return false


func _in_scene(suffix: String) -> bool:
    return current_scene != null and current_scene.scene_file_path.ends_with(suffix)


func _goal() -> Dictionary:
    var stars := get_nodes_in_group("collectibles")
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
        return {"pos": best.global_position, "kind": "star"}
    var doors := get_nodes_in_group("exit_door")
    if not doors.is_empty():
        return {"pos": (doors[0] as Node2D).global_position, "kind": "door"}
    return {}


func _steer(delta: float) -> void:
    if not _in_scene("game.tscn"):
        return
    var player := current_scene.get_node_or_null("Player") as CharacterBody2D
    if player == null:
        return
    _deaths = int(current_scene.get("deaths"))
    _jump_cooldown = maxf(_jump_cooldown - delta, 0.0)
    var goal := _goal()
    if goal.is_empty():
        _release_all()
        return
    var goal_pos: Vector2 = goal["pos"]
    var dx := goal_pos.x - player.global_position.x
    var moving := absf(dx) > 10.0
    _set_action("move_left", moving and dx < 0.0)
    _set_action("move_right", moving and dx > 0.0)
    var on_floor := player.is_on_floor()
    if moving and on_floor and absf(player.global_position.x - _last_x) < 0.6:
        _blocked_for += delta
    else:
        _blocked_for = 0.0
    _last_x = player.global_position.x
    if not on_floor or _jump_cooldown > 0.0:
        return
    var want_jump := false
    if goal_pos.y < player.global_position.y - 26.0:
        want_jump = true
    elif _blocked_for > 0.22:
        want_jump = true
    elif moving and not _ground_ahead(player, signf(dx)):
        want_jump = true
    if want_jump:
        _tap_jump()
        _jump_cooldown = 0.3
        _blocked_for = 0.0


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
    var fps := 0.0
    if _game_time > 0.5:
        fps = _frames / _game_time
    var data := {
        "player": "competent", "mode": "win", "ok": ok,
        "reason": reason, "time_s": snappedf(_game_time, 0.01),
        "deaths": _deaths, "fps_avg": snappedf(fps, 0.1),
        "load_ms": _load_ms, "findings": [],
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
