extends SceneTree

## Scripted player: BREAKER (generic runtime). Adversarial fuzz
## through the real input actions, whatever the plan: seeded
## random movement, pause spam (real key events, so the real
## pause path runs), restarts through the pause menu's real
## Restart button, and constant attempts to leave the play area.
## In a grid_step game it also plays the first puzzle's sabotage
## line — a legal move sequence the world data records as
## cornering a pushable — to prove the game notices a deadlock.
## A crash or an ERROR line is caught by the test runner; a
## stuck state is reported here as a finding. Winning or losing
## while fuzzing is fine — both are reported, neither is a
## failure.
##
## Run: godot --headless --path . --script tests/breaker.gd

const DURATION: float = 30.0
const TIMEOUT: float = 90.0
const KEY_FOR := {"u": KEY_W, "d": KEY_S, "l": KEY_A, "r": KEY_D}

var _rng := RandomNumberGenerator.new()
var _phase: int = 0
var _wait: float = 0.0
var _elapsed: float = 0.0
var _game_time: float = 0.0
var _menu: Node
var _done: bool = false
var _findings: Array = []
var _change_in: float = 0.2
var _pause_in: float = 2.5
var _paused_seen: bool = false
var _last_pos := Vector2(-1, -1)
var _still_for: float = 0.0
var _stuck_recorded: bool = false
var _ended_as: String = ""
var _movement: String = "top_down_2d"
var _sabotage: Array = []
var _sabotage_index: int = -1
var _step_wait: float = 0.0
var _step_stage: int = 0


func _initialize() -> void:
    _rng.seed = 20240
    if FileAccess.file_exists("res://game_config.json"):
        var parsed: Variant = JSON.parse_string(
            FileAccess.get_file_as_string("res://game_config.json"))
        if typeof(parsed) == TYPE_DICTIONARY:
            var plan: Dictionary = (parsed as Dictionary).get("plan", {})
            if typeof(plan) == TYPE_DICTIONARY:
                _movement = str(plan.get("movement", _movement))
    if _movement == "grid_step" \
            and FileAccess.file_exists("res://world/world.json"):
        var wparsed: Variant = JSON.parse_string(
            FileAccess.get_file_as_string("res://world/world.json"))
        if typeof(wparsed) == TYPE_DICTIONARY:
            var units: Array = (wparsed as Dictionary).get("units", [])
            if not units.is_empty():
                var line := str((units[0] as Dictionary).get("sabotage", ""))
                for ch in line.split(""):
                    if ch != "":
                        _sabotage.append(ch)
                if not _sabotage.is_empty():
                    _sabotage_index = 0
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
        _report(false, "breaker run timed out")
        return true
    match _phase:
        0:
            _wait += delta
            if _wait > 0.3:
                _menu.call("start_game")
                _phase = 1
        1:
            if _in_scene("win.tscn") or _in_scene("game_over.tscn"):
                _ended_as = "win" if _in_scene("win.tscn") else "game over"
                _findings.append("fuzzing reached the %s screen" % _ended_as)
                _release_all()
                _report(true, "")
                return true
            _game_time += delta
            if _game_time >= DURATION:
                _release_all()
                _report(true, "")
                return true
            _fuzz(delta)
    return false


func _in_scene(suffix: String) -> bool:
    return current_scene != null and current_scene.scene_file_path.ends_with(suffix)


func _fuzz(delta: float) -> void:
    if not _in_scene("game.tscn"):
        return
    var game := current_scene
    if _sabotage_index >= 0:
        _play_sabotage(delta, game)
        return
    var is_paused: bool = game.get("_is_paused") == true
    _pause_in -= delta
    if _pause_in <= 0.0:
        _pause_in = _rng.randf_range(1.6, 3.2)
        _tap_key(KEY_ESCAPE)
        if not is_paused:
            _paused_seen = true
    if is_paused:
        # Use the pause menu like a player would: mostly resume,
        # sometimes hit Restart through the real button.
        if _rng.randf() < 0.012:
            _press_button_with_text(game, "Restart")
        elif _rng.randf() < 0.3:
            _tap_key(KEY_ESCAPE)
        return
    _change_in -= delta
    if _change_in <= 0.0:
        _change_in = _rng.randf_range(0.12, 0.35)
        _release_all()
        var choices := ["move_left", "move_right", "move_up",
                        "move_down", "interact"]
        Input.action_press(choices[_rng.randi_range(0, 4)])
        if _rng.randf() < 0.4:
            Input.action_press(choices[_rng.randi_range(0, 4)])
    var player := game.find_child("Player", true, false)
    if player != null:
        var spot := Vector2.ZERO
        if player is Node3D:
            var p := (player as Node3D).global_position
            spot = Vector2(p.x, p.z)
        elif player is Node2D:
            spot = (player as Node2D).global_position
        if spot.distance_to(_last_pos) < 1.0:
            _still_for += delta
        else:
            _still_for = 0.0
            _last_pos = spot
        if _still_for > 6.0 and not _stuck_recorded:
            _findings.append("stuck: the player did not move for 6 seconds under fuzz input")
            _stuck_recorded = true
            _still_for = 0.0


func _play_sabotage(delta: float, game: Node) -> void:
    # Play the recorded sabotage line as timed key taps, then
    # check the game noticed the deadlock it creates.
    _step_wait -= delta
    if _step_wait > 0.0:
        return
    match _step_stage:
        0:
            _key_event(KEY_FOR[_sabotage[_sabotage_index]], true)
            _step_stage = 1
            _step_wait = 0.07
        1:
            _key_event(KEY_FOR[_sabotage[_sabotage_index]], false)
            _sabotage_index += 1
            _step_stage = 0
            _step_wait = 0.11
            if _sabotage_index >= _sabotage.size():
                _sabotage_index = -1
                if game.get("deadlock_detected") == true:
                    _findings.append(
                        "sabotage line cornered a pushable and the game reported the deadlock")
                else:
                    _findings.append(
                        "sabotage line played but no deadlock was reported")


func _press_button_with_text(node: Node, text: String) -> bool:
    if node is Button and (node as Button).text == text:
        (node as Button).pressed.emit()
        return true
    for child in node.get_children():
        if _press_button_with_text(child, text):
            return true
    return false


func _key_event(keycode: int, pressed: bool) -> void:
    var event := InputEventKey.new()
    event.physical_keycode = keycode
    event.pressed = pressed
    Input.parse_input_event(event)


func _tap_key(keycode: int) -> void:
    _key_event(keycode, true)
    _key_event(keycode, false)


func _release_all() -> void:
    for action in ["move_left", "move_right", "move_up", "move_down",
                 "interact"]:
        Input.action_release(action)


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
