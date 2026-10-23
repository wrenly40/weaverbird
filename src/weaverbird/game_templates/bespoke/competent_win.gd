extends SceneTree

## Scripted player: COMPETENT (bespoke). Plays any bespoke
## mechanics module through its contract: it reads the module's
## bridge_state() (player_pos / objective_pos) and steers with
## the real input actions — the same facts a human player sees.
## Prints one WBTEST JSON line, plus SMOKE PASS on a win, so this
## file also serves as the project's flow smoke.
##
## Run: godot --headless --path . --script tests/competent_win.gd

const TIMEOUT: float = 120.0

var _phase: int = 0
var _wait: float = 0.0
var _elapsed: float = 0.0
var _game_time: float = 0.0
var _frames: int = 0
var _start_ticks: int = 0
var _load_ms: int = 0
var _menu: Node
var _done: bool = false


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
                print("SMOKE PASS: menu -> game -> bespoke win screen")
                _report(true, "")
                return true
            if _in_scene("game_over.tscn"):
                _release_all()
                _report(false, "the competent player lost the run")
                return true
            if current_scene != null and _load_ms == 0:
                _load_ms = Time.get_ticks_msec() - _start_ticks
            _steer()
    return false


func _in_scene(suffix: String) -> bool:
    return current_scene != null and current_scene.scene_file_path.ends_with(suffix)


func _steer() -> void:
    if not _in_scene("game.tscn"):
        return
    if not current_scene.has_method("bridge_state"):
        return
    var facts: Dictionary = current_scene.call("bridge_state")
    var player_pos: Variant = facts.get("player_pos")
    var goal_pos: Variant = facts.get("objective_pos")
    if typeof(player_pos) != TYPE_ARRAY or typeof(goal_pos) != TYPE_ARRAY:
        _release_all()
        return
    var direction := Vector2(float(goal_pos[0]), float(goal_pos[1])) \
        - Vector2(float(player_pos[0]), float(player_pos[1]))
    if direction.length() > 8.0:
        direction = direction.normalized()
        _set_action("move_left", direction.x < -0.35)
        _set_action("move_right", direction.x > 0.35)
        _set_action("move_up", direction.y < -0.35)
        _set_action("move_down", direction.y > 0.35)
    else:
        _release_all()


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
    var units: Array = []
    if ok:
        units = [{"unit": 1, "completed": true, "deaths": 0,
                  "time_s": snappedf(_game_time, 0.01)}]
    var data := {
        "player": "competent", "mode": "win", "ok": ok,
        "reason": reason, "time_s": snappedf(_game_time, 0.01),
        "deaths": 0, "fps_avg": snappedf(fps, 0.1),
        "load_ms": _load_ms, "findings": [], "units": units,
    }
    _silence()
    OS.delay_msec(400)
    print("WBTEST " + JSON.stringify(data))
    quit(0 if ok else 1)


func _silence() -> void:
    var stack: Array = [root]
    while not stack.is_empty():
        var node: Node = stack.pop_back()
        if node is AudioStreamPlayer:
            (node as AudioStreamPlayer).stop()
        for child in node.get_children():
            stack.append(child)
