extends SceneTree

## Scripted player: FIRST-TIME (bespoke). Starts from the menu
## and follows only what the module teaches: the on-screen hint
## plus the objective position its bridge_state() exposes. It
## records when it first reached the objective. Its results are
## observations, not gates. Prints one WBTEST JSON line.
##
## Run: godot --headless --path . --script tests/first_time.gd

const TIMEOUT: float = 120.0

var _phase: int = 0
var _wait: float = 0.0
var _elapsed: float = 0.0
var _game_time: float = 0.0
var _menu: Node
var _done: bool = false
var _first_objective_s: float = -1.0
var _finished: bool = false


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
        _release_all()
        _report(false, "timed out")
        return true
    match _phase:
        0:
            _wait += delta
            if _wait > 0.3:
                _press_play()
                _phase = 1
        1:
            if _in_scene("win.tscn"):
                _release_all()
                _finished = true
                _report(true, "")
                return true
            if _in_scene("game_over.tscn"):
                _release_all()
                _report(true, "")
                return true
            _follow_hint()
    return false


func _press_play() -> void:
    # The menu's real Play button, not a state shortcut.
    var buttons: Array = []
    _collect_buttons(_menu, buttons)
    for button in buttons:
        if str(button.text).strip_edges().to_lower().begins_with("play"):
            button.emit_signal("pressed")
            return
    _menu.call("start_game")


func _collect_buttons(node: Node, out: Array) -> void:
    if node is Button:
        out.append(node)
    for child in node.get_children():
        _collect_buttons(child, out)


func _in_scene(suffix: String) -> bool:
    return current_scene != null and current_scene.scene_file_path.ends_with(suffix)


func _follow_hint() -> void:
    if not _in_scene("game.tscn"):
        return
    if not current_scene.has_method("bridge_state"):
        return
    var facts: Dictionary = current_scene.call("bridge_state")
    var player_pos: Variant = facts.get("player_pos")
    var goal_pos: Variant = facts.get("objective_pos")
    if typeof(player_pos) != TYPE_ARRAY or typeof(goal_pos) != TYPE_ARRAY:
        return
    var delta_pos := Vector2(float(goal_pos[0]), float(goal_pos[1])) \
        - Vector2(float(player_pos[0]), float(player_pos[1]))
    if delta_pos.length() <= 40.0 and _first_objective_s < 0.0:
        _first_objective_s = snappedf(_game_time, 0.01)
    if delta_pos.length() > 8.0:
        var direction := delta_pos.normalized()
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
    var data := {
        "player": "first_time", "mode": "hint_only", "ok": ok,
        "reason": reason, "time_s": snappedf(_game_time, 0.01),
        "deaths": 0, "first_objective_s": _first_objective_s,
        "finished": _finished, "findings": [], "units": [],
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
