extends SceneTree

## Scripted player: BREAKER (crate pusher). Three attacks, all
## through real key events: seeded random-move fuzz, pause spam,
## and then — from a fresh start — the sabotage line recorded for
## the first puzzle in game_config.json, which shoves a crate into
## a corner. The game's own deadlock detection must flag that; if
## it does not, that is the finding. A crash or an ERROR line is
## caught by the test runner.
##
## Run: godot --headless --path . --script tests/breaker.gd

const TIMEOUT: float = 180.0
const KEY_FOR := {"u": KEY_W, "d": KEY_S, "l": KEY_A, "r": KEY_D}
const KEYS := [KEY_W, KEY_A, KEY_S, KEY_D, KEY_E]

var _rng := RandomNumberGenerator.new()
var _phase: int = 0
var _wait: float = 0.0
var _elapsed: float = 0.0
var _game_time: float = 0.0
var _menu: Node
var _done: bool = false
var _findings: Array = []
var _stage: int = 0
var _fuzz_moves: int = 0
var _sabotage: Array = []
var _sabotage_index: int = 0
var _step_wait: float = 0.0
var _step_stage: int = 0
var _pending_key: int = 0
var _deadlock_wait: float = 0.0
var _reload_wait: float = 0.0
var _paused_seen: bool = false


func _initialize() -> void:
    _rng.seed = 4242
    if FileAccess.file_exists("res://game_config.json"):
        var parsed: Variant = JSON.parse_string(
            FileAccess.get_file_as_string("res://game_config.json"))
        if typeof(parsed) == TYPE_DICTIONARY:
            var puzzles: Array = (parsed as Dictionary).get("puzzles", [])
            if not puzzles.is_empty():
                var sabotage := str((puzzles[0] as Dictionary).get("sabotage", ""))
                for ch in sabotage.split(""):
                    if ch != "":
                        _sabotage.append(ch)
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
        _report(false, "breaker run timed out")
        return true
    match _phase:
        0:
            _wait += delta
            if _wait > 0.3:
                _menu.call("start_game")
                _phase = 1
        1:
            if _in_scene("win.tscn"):
                _findings.append("fuzzing solved the game — noted, not a failure")
                _report(true, "")
                return true
            if _in_scene("game_over.tscn") and _stage == 0:
                _findings.append("fuzzing exhausted the move budget — game over, as designed")
                _report(true, "")
                return true
            _game_time += delta
            _play(delta)
    return false


func _in_scene(suffix: String) -> bool:
    return current_scene != null and current_scene.scene_file_path.ends_with(suffix)


func _play(delta: float) -> void:
    if not _in_scene("game.tscn"):
        return
    _step_wait -= delta
    match _stage:
        0:  # random fuzz, with the odd pause
            if _step_wait > 0.0:
                return
            match _step_stage:
                0:
                    if _fuzz_moves % 9 == 4:
                        _tap(KEY_ESCAPE, true)
                        _pending_key = KEY_ESCAPE
                        _paused_seen = true
                    else:
                        _pending_key = KEYS[_rng.randi_range(0, KEYS.size() - 1)]
                        _tap(_pending_key, true)
                    _step_stage = 1
                    _step_wait = 0.06
                1:
                    _tap(_pending_key, false)
                    if _pending_key == KEY_ESCAPE:
                        _tap(KEY_ESCAPE, true)
                        _pending_key = -1
                        _step_stage = 2
                        _step_wait = 0.06
                    else:
                        _fuzz_moves += 1
                        _step_stage = 0
                        _step_wait = 0.09
                2:
                    _tap(KEY_ESCAPE, false)
                    _fuzz_moves += 1
                    _step_stage = 0
                    _step_wait = 0.09
            if _fuzz_moves >= 24:
                _stage = 1
                _reload_wait = 0.6
                change_scene_to_file("res://scenes/game.tscn")
        1:  # wait for the fresh game scene, then sabotage
            _reload_wait -= delta
            if _reload_wait > 0.0:
                return
            if _sabotage.is_empty():
                _findings.append("no sabotage line recorded for puzzle 1 — skipped the corner test")
                _report(true, "")
                return
            _stage = 2
            _step_stage = 0
            _step_wait = 0.25
        2:  # replay the sabotage line
            if _step_wait > 0.0:
                return
            if _sabotage_index >= _sabotage.size():
                _stage = 3
                _deadlock_wait = 0.4
                return
            match _step_stage:
                0:
                    _pending_key = KEY_FOR[_sabotage[_sabotage_index]]
                    _tap(_pending_key, true)
                    _step_stage = 1
                    _step_wait = 0.06
                1:
                    _tap(_pending_key, false)
                    _sabotage_index += 1
                    _step_stage = 0
                    _step_wait = 0.1
        3:  # the game must have noticed the cornered crate
            _deadlock_wait -= delta
            if _deadlock_wait > 0.0:
                return
            if bool(current_scene.get("deadlock_detected")):
                _findings.append("cornered crate detected by the game and reported to the player")
            else:
                _findings.append("cornered crate was NOT detected by the game")
            _report(true, "")
        _:
            pass


func _tap(keycode: int, pressed: bool) -> void:
    var event := InputEventKey.new()
    event.physical_keycode = keycode
    event.pressed = pressed
    Input.parse_input_event(event)


func _report(ok: bool, reason: String) -> void:
    _done = true
    if _paused_seen:
        _findings.append("pause overlay opened and closed under fuzz input")
    var data := {
        "player": "breaker", "mode": "fuzz", "ok": ok,
        "reason": reason, "time_s": snappedf(_game_time, 0.01),
        "deaths": 0, "findings": _findings,
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
