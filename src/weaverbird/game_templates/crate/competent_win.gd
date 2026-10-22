extends SceneTree

## Scripted player: COMPETENT (crate pusher). Every puzzle the
## factory ships carries a verified solution, recorded in
## game_config.json. This player replays those moves as real key
## events, so every push goes through the game's own movement and
## pushing rules — it knows the answers, but it still has to play
## them. Prints one WBTEST JSON line, plus SMOKE PASS so this file
## also serves as the flow smoke.
##
## Run: godot --headless --path . --script tests/competent_win.gd

const TIMEOUT: float = 240.0
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
var _solutions: Array = []
var _puzzle_seen: int = -1
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
            for puzzle in (parsed as Dictionary).get("puzzles", []):
                _solutions.append(str((puzzle as Dictionary).get("solution", "")))
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
                print("SMOKE PASS: menu -> game -> solve every puzzle -> win screen")
                _report(true, "")
                return true
            if _in_scene("game_over.tscn"):
                _report(false, "ran out of moves on a solution replay")
                return true
            if current_scene != null and _load_ms == 0:
                _load_ms = Time.get_ticks_msec() - _start_ticks
            _play(delta)
    return false


func _in_scene(suffix: String) -> bool:
    return current_scene != null and current_scene.scene_file_path.ends_with(suffix)


func _play(delta: float) -> void:
    if not _in_scene("game.tscn"):
        return
    var game := current_scene
    var puzzle_index := int(game.get("puzzle_index"))
    if puzzle_index != _puzzle_seen:
        _puzzle_seen = puzzle_index
        _moves.clear()
        if puzzle_index < _solutions.size():
            for ch in _solutions[puzzle_index].split(""):
                if ch != "":
                    _moves.append(ch)
        _move_index = 0
        _step_stage = 0
        _step_wait = 0.2
        _advance_wait = 0.0
    if _move_index >= _moves.size():
        # Solution played; the game should advance on its own.
        _advance_wait += delta
        if _advance_wait > 4.0:
            _report(false,
                "solution for puzzle %d did not solve it" % (puzzle_index + 1))
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


func _report(ok: bool, reason: String) -> void:
    _done = true
    var fps := 0.0
    if _game_time > 0.5:
        fps = _frames / _game_time
    var data := {
        "player": "competent", "mode": "win", "ok": ok,
        "reason": reason, "time_s": snappedf(_game_time, 0.01),
        "deaths": 0, "fps_avg": snappedf(fps, 0.1),
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
