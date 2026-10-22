extends SceneTree

## Scripted player: COMPETENT, losing on purpose (crate pusher).
## Walks back and forth between two safe cells — every step is a
## legal move, none of them pushes a crate — until the move budget
## is spent and the game-over screen proves the lose path.
## Prints one WBTEST JSON line.
##
## Run: godot --headless --path . --script tests/competent_lose.gd

const TIMEOUT: float = 240.0

var _phase: int = 0
var _wait: float = 0.0
var _elapsed: float = 0.0
var _game_time: float = 0.0
var _menu: Node
var _done: bool = false
var _step_wait: float = 0.0
var _step_stage: int = 0
var _pending_key: int = 0
var _forward_done: bool = false
var _forward_key: int = 0
var _back_key: int = 0

const DIRS := [
    [Vector2i(1, 0), KEY_D, KEY_A],
    [Vector2i(-1, 0), KEY_A, KEY_D],
    [Vector2i(0, 1), KEY_S, KEY_W],
    [Vector2i(0, -1), KEY_W, KEY_S],
]


func _initialize() -> void:
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
        _report(false, "timed out — the move budget never ran out")
        return true
    match _phase:
        0:
            _wait += delta
            if _wait > 0.3:
                _menu.call("start_game")
                _phase = 1
        1:
            if _in_scene("game_over.tscn"):
                _report(true, "")
                return true
            if _in_scene("win.tscn"):
                _report(false, "reached the win screen on a losing run")
                return true
            _play(delta)
    return false


func _in_scene(suffix: String) -> bool:
    return current_scene != null and current_scene.scene_file_path.ends_with(suffix)


func _play(delta: float) -> void:
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
