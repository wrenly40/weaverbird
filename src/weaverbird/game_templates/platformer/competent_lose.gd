extends SceneTree

## Scripted player: COMPETENT, losing on purpose (platformer).
## Holds "move right" and never jumps: the first pit drops it onto
## the spikes below, costing a life; respawn, repeat, until the
## lives run out and the game-over screen proves the lose path.
## Prints one WBTEST JSON line.
##
## Run: godot --headless --path . --script tests/competent_lose.gd

const TIMEOUT: float = 120.0

var _phase: int = 0
var _wait: float = 0.0
var _elapsed: float = 0.0
var _game_time: float = 0.0
var _menu: Node
var _done: bool = false
var _deaths: int = 0


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
        Input.action_release("move_right")
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
                Input.action_release("move_right")
                _report(true, "")
                return true
            if _in_scene("win.tscn"):
                Input.action_release("move_right")
                _report(false, "reached the win screen on a losing run")
                return true
            if _in_scene("game.tscn"):
                _deaths = int(current_scene.get("deaths"))
                Input.action_press("move_right")
    return false


func _in_scene(suffix: String) -> bool:
    return current_scene != null and current_scene.scene_file_path.ends_with(suffix)


func _report(ok: bool, reason: String) -> void:
    _done = true
    var data := {
        "player": "competent", "mode": "lose", "ok": ok,
        "reason": reason, "time_s": snappedf(_game_time, 0.01),
        "deaths": _deaths, "findings": [],
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
