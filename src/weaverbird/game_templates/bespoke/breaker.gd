extends SceneTree

## Scripted player: BREAKER (bespoke). Fuzzes the module with
## seeded random inputs and pause spam for a fixed run, looking
## for crashes and soft-locks. Findings are warnings; a crash or
## an engine error fails the run (the test stage judges that
## from the output). Prints one WBTEST JSON line.
##
## Run: godot --headless --path . --script tests/breaker.gd

const RUN_SECONDS: float = 20.0

var _phase: int = 0
var _wait: float = 0.0
var _elapsed: float = 0.0
var _game_time: float = 0.0
var _menu: Node
var _done: bool = false
var _rng := RandomNumberGenerator.new()
var _switch_in: float = 0.0
var _findings: Array = []


func _initialize() -> void:
    _rng.seed = 4242
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
    match _phase:
        0:
            _wait += delta
            if _wait > 0.3:
                _menu.call("start_game")
                _phase = 1
        1:
            _game_time += delta
            if _in_scene("win.tscn") or _in_scene("game_over.tscn"):
                _release_all()
                _report(true, "")
                return true
            _fuzz(delta)
            if _game_time >= RUN_SECONDS:
                _release_all()
                _report(true, "")
                return true
    if _elapsed > RUN_SECONDS + 30.0:
        _release_all()
        _report(false, "the breaker run did not finish")
        return true
    return false


func _in_scene(suffix: String) -> bool:
    return current_scene != null and current_scene.scene_file_path.ends_with(suffix)


func _fuzz(delta: float) -> void:
    _switch_in -= delta
    if _switch_in > 0.0:
        return
    _switch_in = _rng.randf_range(0.1, 0.45)
    _release_all()
    var choices := ["move_left", "move_right", "move_up", "move_down",
                    "interact"]
    Input.action_press(choices[_rng.randi_range(0, choices.size() - 1)])
    if _rng.randf() < 0.12:
        var pressed := InputEventAction.new()
        pressed.action = "pause"
        pressed.pressed = true
        Input.parse_input_event(pressed)
        var released := InputEventAction.new()
        released.action = "pause"
        released.pressed = false
        Input.parse_input_event(released)
        if _rng.randf() < 0.5:
            Input.parse_input_event(pressed)
            Input.parse_input_event(released)


func _release_all() -> void:
    for action in ["move_left", "move_right", "move_up", "move_down",
                   "interact"]:
        Input.action_release(action)


func _report(ok: bool, reason: String) -> void:
    _done = true
    var data := {
        "player": "breaker", "mode": "fuzz", "ok": ok,
        "reason": reason, "time_s": snappedf(_game_time, 0.01),
        "deaths": 0, "findings": _findings, "units": [],
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
