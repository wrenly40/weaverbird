extends SceneTree

## Scripted player: COMPETENT, losing on purpose (generic
## runtime). Proves the plan's lose path end to end through the
## game's real logic — which way it loses follows the plan's
## fail rules:
##
##   hazard_contacts — steer straight into the nearest hazard,
##     as many times as the plan's lives require.
##   timer (no hazard rule) — stand still and let the clock run
##     out. A survive plan's clock is its win, so there the
##     hazard rule (or a pit) does the work instead.
##   fall (side_view) — walk into the nearest pit, repeatedly.
##   grid_step — pace between two safe cells, never pushing,
##     until the move budget is spent.
##
## Prints one WBTEST JSON line.
##
## Run: godot --headless --path . --script tests/competent_lose.gd

const TIMEOUT: float = 300.0
const DIRS := [
    [Vector2i(1, 0), KEY_D, KEY_A],
    [Vector2i(-1, 0), KEY_A, KEY_D],
    [Vector2i(0, 1), KEY_S, KEY_W],
    [Vector2i(0, -1), KEY_W, KEY_S],
]

var _phase: int = 0
var _wait: float = 0.0
var _elapsed: float = 0.0
var _game_time: float = 0.0
var _menu: Node
var _done: bool = false
var _movement: String = "top_down_2d"
var _objective: String = "collect"
var _fail: Array = []
var _world_units: Array = []
# grid pacing state
var _step_wait: float = 0.0
var _step_stage: int = 0
var _pending_key: int = 0
var _forward_done: bool = false
var _forward_key: int = 0
var _back_key: int = 0


func _initialize() -> void:
    if FileAccess.file_exists("res://game_config.json"):
        var parsed: Variant = JSON.parse_string(
            FileAccess.get_file_as_string("res://game_config.json"))
        if typeof(parsed) == TYPE_DICTIONARY:
            var plan: Dictionary = (parsed as Dictionary).get("plan", {})
            if typeof(plan) == TYPE_DICTIONARY:
                _movement = str(plan.get("movement", _movement))
                _objective = str(plan.get("objective", _objective))
                _fail = plan.get("fail", [])
    if FileAccess.file_exists("res://world/world.json"):
        var wparsed: Variant = JSON.parse_string(
            FileAccess.get_file_as_string("res://world/world.json"))
        if typeof(wparsed) == TYPE_DICTIONARY:
            _world_units = (wparsed as Dictionary).get("units", [])
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
        _game_time += delta
    if _elapsed > TIMEOUT:
        _release_all()
        _report(false, "timed out — the game could not be lost")
        return true
    match _phase:
        0:
            _wait += delta
            if _wait > 0.3:
                _menu.call("start_game")
                _phase = 1
        1:
            if _in_scene("game_over.tscn"):
                _release_all()
                _report(true, "")
                return true
            if _in_scene("win.tscn"):
                _release_all()
                _report(false, "reached the win screen on a losing run")
                return true
            match _movement:
                "grid_step":
                    _play_grid(delta)
                "side_view":
                    _play_side()
                _:
                    _play_top()
    return false


func _in_scene(suffix: String) -> bool:
    return current_scene != null and current_scene.scene_file_path.ends_with(suffix)


func _node_pos(node: Node) -> Vector2:
    if node is Node3D:
        var p := (node as Node3D).global_position
        return Vector2(p.x, p.z)
    if node is Node2D:
        return (node as Node2D).global_position
    return Vector2.ZERO


func _play_top() -> void:
    if not _in_scene("game.tscn"):
        return
    var hazards := get_nodes_in_group("hazards")
    if _fail.has("hazard_contacts") and not hazards.is_empty():
        var player := current_scene.find_child("Player", true, false)
        if player == null:
            return
        var ppos := _node_pos(player)
        var goal := Vector2.ZERO
        var best := 1e18
        for hazard in hazards:
            var spot := _node_pos(hazard)
            var dist := ppos.distance_squared_to(spot)
            if dist < best:
                best = dist
                goal = spot
        var direction := goal - ppos
        var deadband := 0.3 if _movement == "top_down_3d" else 4.0
        if direction.length() > deadband:
            direction = direction.normalized()
            _set_action("move_left", direction.x < -0.3)
            _set_action("move_right", direction.x > 0.3)
            _set_action("move_up", direction.y < -0.3)
            _set_action("move_down", direction.y > 0.3)
        else:
            _release_all()
    else:
        # No hazard rule to die by: the timer is the lose path.
        # Stand still and let it run out.
        _release_all()


func _play_side() -> void:
    if not _in_scene("game.tscn"):
        return
    # Walk into the nearest spike pit: head for the nearest
    # hazard zone's column and keep walking — pits and spikes
    # both cost lives, and the plan's lives are finite.
    var player := current_scene.find_child("Player", true, false) as Node2D
    if player == null:
        return
    var hazards := get_nodes_in_group("hazards")
    var dir := 1.0
    if not hazards.is_empty():
        var best := 1e18
        var goal_x := player.global_position.x
        for hazard in hazards:
            var spot := (hazard as Node2D).global_position
            var dist := absf(spot.x - player.global_position.x)
            if dist < best:
                best = dist
                goal_x = spot.x
        dir = signf(goal_x - player.global_position.x)
        if absf(goal_x - player.global_position.x) < 12.0:
            dir = 0.0  # standing in it — the game does the rest
    _set_action("move_left", dir < 0.0)
    _set_action("move_right", dir > 0.0)


func _play_grid(delta: float) -> void:
    if not _in_scene("game.tscn"):
        return
    _step_wait -= delta
    if _step_wait > 0.0:
        return
    match _step_stage:
        0:
            var key := _next_key()
            if key == 0:
                _report(false, "no safe pacing move found — boxed in")
                return
            _pending_key = key
            _tap(key, true)
            _step_stage = 1
            _step_wait = 0.06
        1:
            _tap(_pending_key, false)
            _step_stage = 0
            _step_wait = 0.09


func _next_key() -> int:
    if _forward_done:
        _forward_done = false
        return _back_key
    var game := current_scene
    var player: Vector2i = game.get("_player_cell")
    var walls: Array = game.get("_walls")
    var crates: Array = game.get("_crates")
    for entry in DIRS:
        var step: Vector2i = entry[0]
        var cell := player + step
        if not walls.has(cell) and not crates.has(cell):
            _forward_key = int(entry[1])
            _back_key = int(entry[2])
            _forward_done = true
            return _forward_key
    return 0


func _tap(keycode: int, pressed: bool) -> void:
    var event := InputEventKey.new()
    event.physical_keycode = keycode
    event.pressed = pressed
    Input.parse_input_event(event)


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
    var data := {
        "player": "competent", "mode": "lose", "ok": ok,
        "reason": reason, "time_s": snappedf(_game_time, 0.01),
        "deaths": 1 if ok else 0, "findings": [],
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
