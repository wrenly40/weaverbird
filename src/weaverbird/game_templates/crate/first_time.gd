extends SceneTree

## Scripted player: FIRST-TIME (crate pusher). Starts at the main
## menu and presses the real Play button, then plays from the
## on-screen hint alone — "push every crate onto a glowing pad" —
## with a simple greedy strategy a new player might use: pick the
## crate nearest a pad, walk behind it (breadth-first over the
## grid it can see), push it toward the pad, repeat. It does not
## know about corners, undo, or the recorded solutions. Records
## time-to-first-crate-on-a-pad and whether it finished.
##
## Run: godot --headless --path . --script tests/first_time.gd

const TIMEOUT: float = 300.0
const MAX_PUSHES: int = 90
const KEY_FOR := {"u": KEY_W, "d": KEY_S, "l": KEY_A, "r": KEY_D}
const DIRS := {
    "u": Vector2i(0, -1), "d": Vector2i(0, 1),
    "l": Vector2i(-1, 0), "r": Vector2i(1, 0),
}

var _phase: int = 0
var _wait: float = 0.0
var _elapsed: float = 0.0
var _game_time: float = 0.0
var _menu: Node
var _done: bool = false
var _move_queue: Array = []
var _step_wait: float = 0.0
var _step_stage: int = 0
var _pending_key: int = 0
var _pushes: int = 0
var _plan_wait: float = 0.0
var _first_objective_s: float = -1.0
var _finished: bool = false
var _outcome: String = ""
var _gave_up: String = ""


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
    if _elapsed > TIMEOUT:
        _report(true, "")
        return true
    match _phase:
        0:
            _wait += delta
            if _wait > 0.4:
                if _find_button(_menu, "Play"):
                    _phase = 1
                else:
                    _report(false, "no Play button found on the main menu")
                    return true
        1:
            if _in_scene("win.tscn"):
                _finished = true
                _outcome = "win"
                _report(true, "")
                return true
            if _in_scene("game_over.tscn"):
                _outcome = "lose"
                _report(true, "")
                return true
            _game_time += delta
            _play(delta)
    return false


func _in_scene(suffix: String) -> bool:
    return current_scene != null and current_scene.scene_file_path.ends_with(suffix)


func _find_button(node: Node, text: String) -> bool:
    if node is Button and (node as Button).text == text:
        (node as Button).pressed.emit()
        return true
    for child in node.get_children():
        if _find_button(child, text):
            return true
    return false


func _snapshot() -> Dictionary:
    var game := current_scene
    var walls: Dictionary = {}
    for cell in game.get("_walls"):
        walls[cell] = true
    var crates: Array = (game.get("_crates") as Array).duplicate()
    var pads: Array = (game.get("_pads") as Array).duplicate()
    return {
        "player": game.get("_player_cell"), "walls": walls,
        "crates": crates, "pads": pads,
    }


func _play(delta: float) -> void:
    if not _in_scene("game.tscn"):
        return
    if not _move_queue.is_empty():
        _step_wait -= delta
        if _step_wait > 0.0:
            return
        match _step_stage:
            0:
                _pending_key = KEY_FOR[_move_queue[0]]
                _tap(_pending_key, true)
                _step_stage = 1
                _step_wait = 0.06
            1:
                _tap(_pending_key, false)
                _move_queue.pop_front()
                _step_stage = 0
                _step_wait = 0.09
        return
    _plan_wait -= delta
    if _plan_wait > 0.0:
        return
    _plan_wait = 0.25
    _plan()


func _plan() -> void:
    var snap := _snapshot()
    var crates: Array = snap["crates"]
    var pads: Array = snap["pads"]
    if _first_objective_s < 0.0:
        for crate in crates:
            if pads.has(crate):
                _first_objective_s = _game_time
                break
    var all_home := true
    for crate in crates:
        if not pads.has(crate):
            all_home = false
    if all_home:
        return  # the game advances by itself
    if _pushes >= MAX_PUSHES:
        _gave_up = "stopped after %d pushes without finishing" % MAX_PUSHES
        _report(true, "")
        return
    # Greedy: the crate closest to any pad, pushed toward its pad.
    var best_plan: Array = []
    var best_score := 1e9
    for crate in crates:
        if pads.has(crate):
            continue
        for pad in pads:
            if crates.has(pad) and pad != crate:
                continue
            var delta: Vector2i = pad - crate
            var axes: Array = []
            if delta.x != 0:
                axes.append(["r" if delta.x > 0 else "l", abs(delta.x)])
            if delta.y != 0:
                axes.append(["d" if delta.y > 0 else "u", abs(delta.y)])
            if axes.size() == 2 and axes[1][1] > axes[0][1]:
                axes = [axes[1], axes[0]]
            for axis in axes:
                var step_name: String = axis[0]
                var step: Vector2i = DIRS[step_name]
                var stand: Vector2i = crate - step
                var beyond: Vector2i = crate + step
                if snap["walls"].has(beyond) or crates.has(beyond):
                    continue
                var path := _path_to(snap, stand, crate)
                if path.is_empty() and snap["player"] != stand:
                    continue
                var score: int = abs(delta.x) + abs(delta.y)
                if score < best_score:
                    best_score = score
                    best_plan = path + [step_name]
    if best_plan.is_empty():
        _gave_up = "no reachable push found — likely stuck, as a new player would be"
        _report(true, "")
        return
    _move_queue = best_plan
    _pushes += 1


func _path_to(snap: Dictionary, goal: Vector2i, crate_cell: Vector2i) -> Array:
    var start: Vector2i = snap["player"]
    if start == goal:
        return []
    var walls: Dictionary = snap["walls"]
    var crates: Array = snap["crates"]
    var came_from: Dictionary = {start: start}
    var frontier: Array = [start]
    while not frontier.is_empty():
        var cell: Vector2i = frontier.pop_front()
        for name in DIRS.keys():
            var next: Vector2i = cell + DIRS[name]
            if came_from.has(next) or walls.has(next):
                continue
            if crates.has(next) and next != crate_cell:
                continue
            if next == crate_cell:
                continue  # the player cannot walk through the crate
            came_from[next] = cell
            if next == goal:
                frontier.clear()
                break
            frontier.append(next)
    if not came_from.has(goal):
        return []
    var names := {Vector2i(0, -1): "u", Vector2i(0, 1): "d",
                  Vector2i(-1, 0): "l", Vector2i(1, 0): "r"}
    var path: Array = []
    var cursor := goal
    while cursor != start:
        var previous: Vector2i = came_from[cursor]
        path.push_front(names[cursor - previous])
        cursor = previous
    return path


func _tap(keycode: int, pressed: bool) -> void:
    var event := InputEventKey.new()
    event.physical_keycode = keycode
    event.pressed = pressed
    Input.parse_input_event(event)


func _report(ok: bool, reason: String) -> void:
    if _done:
        return
    _done = true
    var findings: Array = []
    if _gave_up != "":
        findings.append(_gave_up)
    elif _outcome == "":
        findings.append("did not finish within the run")
    var data := {
        "player": "first_time", "mode": "explore", "ok": ok,
        "reason": reason, "time_s": snappedf(_game_time, 0.01),
        "deaths": 1 if _outcome == "lose" else 0,
        "first_objective_s": snappedf(_first_objective_s, 0.01),
        "finished": _finished, "outcome": _outcome,
        "findings": findings,
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
