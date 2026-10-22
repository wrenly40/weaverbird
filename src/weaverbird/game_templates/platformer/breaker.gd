extends SceneTree

## Scripted player: BREAKER (platformer). Adversarial fuzz through
## the real input actions and real key events: seeded random runs
## and jumps, pause spam, pause-menu restarts, and long pushes
## against walls and off ledges. A crash or ERROR line is caught by
## the test runner; a genuine stuck state (no movement, no deaths,
## no new stars for a long stretch) is reported as a finding.
## Respawn loops are play, not stuck.
##
## Run: godot --headless --path . --script tests/breaker.gd

const DURATION: float = 30.0
const TIMEOUT: float = 90.0

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
var _progress_mark: String = ""
var _no_progress_for: float = 0.0
var _stuck_recorded: bool = false


func _initialize() -> void:
    _rng.seed = 777
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
                _findings.append(
                    "fuzzing reached the %s screen"
                    % ("win" if _in_scene("win.tscn") else "game over"))
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
    var is_paused := bool(game.get("_is_paused"))
    _pause_in -= delta
    if _pause_in <= 0.0:
        _pause_in = _rng.randf_range(1.8, 3.4)
        _tap_key(KEY_ESCAPE)
        if not is_paused:
            _paused_seen = true
    if is_paused:
        if _rng.randf() < 0.012:
            _press_button_with_text(game, "Restart")
        elif _rng.randf() < 0.3:
            _tap_key(KEY_ESCAPE)
        return
    _change_in -= delta
    if _change_in <= 0.0:
        _change_in = _rng.randf_range(0.15, 0.4)
        _release_all()
        if _rng.randf() < 0.75:
            Input.action_press("move_right" if _rng.randf() < 0.7 else "move_left")
        if _rng.randf() < 0.45:
            _tap_key(KEY_SPACE)
    var mark := "%d|%d|%d|%d" % [
        int(game.get("deaths")), int(game.get("stars_collected")),
        int(game.get("level_index")), int(game.get("lives"))]
    if mark == _progress_mark:
        _no_progress_for += delta
    else:
        _progress_mark = mark
        _no_progress_for = 0.0
    if _no_progress_for > 8.0 and not _stuck_recorded:
        var player := game.get_node_or_null("Player") as CharacterBody2D
        if player != null and player.velocity.length() < 1.0:
            _findings.append("stuck: no movement or progress for 8 seconds under fuzz input")
            _stuck_recorded = true


func _press_button_with_text(node: Node, text: String) -> bool:
    if node is Button and (node as Button).text == text:
        (node as Button).pressed.emit()
        return true
    for child in node.get_children():
        if _press_button_with_text(child, text):
            return true
    return false


func _tap_key(keycode: int) -> void:
    var press := InputEventKey.new()
    press.physical_keycode = keycode
    press.pressed = true
    Input.parse_input_event(press)
    var release := InputEventKey.new()
    release.physical_keycode = keycode
    release.pressed = false
    Input.parse_input_event(release)


func _release_all() -> void:
    for action in ["move_left", "move_right", "move_up", "move_down"]:
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
