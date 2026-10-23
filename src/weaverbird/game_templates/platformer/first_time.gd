extends SceneTree

## Scripted player: FIRST-TIME (platformer). Starts at the main
## menu and presses the real Play button, then plays using only
## what the game teaches: the hint says gather the stars and reach
## the door, so it heads for stars and the door, decides at
## human-ish intervals, and jumps only when its goal is plainly
## higher or the ground runs out — it does not know about spikes.
## Records time-to-first-star and whether it finished. Not
## finishing is an observation, not a failure.
##
## Run: godot --headless --path . --script tests/first_time.gd

const TIMEOUT: float = 180.0

var _phase: int = 0
var _wait: float = 0.0
var _elapsed: float = 0.0
var _game_time: float = 0.0
var _menu: Node
var _done: bool = false
var _decide_in: float = 0.0
var _move_dir: float = 0.0
var _want_jump: bool = false
var _jump_cooldown: float = 0.0
var _first_objective_s: float = -1.0
var _finished: bool = false
var _outcome: String = ""
var _deaths: int = 0
var _goal_id: int = 0
var _goal_since: float = 0.0
var _ignored_until: Dictionary = {}
# Per-unit (per-level) records for the balance lab.
var _units: Array = []
var _unit_level: int = -1
var _unit_t0: float = 0.0
var _unit_deaths0: int = 0


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
        _release_all()
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
                _release_all()
                _close_unit(true)
                _report(true, "")
                return true
            if _in_scene("game_over.tscn"):
                _outcome = "lose"
                _release_all()
                _close_unit(false)
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


func _play(delta: float) -> void:
    if not _in_scene("game.tscn"):
        return
    var game := current_scene
    _deaths = int(game.get("deaths"))
    _track_unit()
    if _first_objective_s < 0.0 and int(game.get("stars_collected")) > 0:
        _first_objective_s = _game_time
    var player := game.get_node_or_null("Player") as CharacterBody2D
    if player == null:
        return
    _jump_cooldown = maxf(_jump_cooldown - delta, 0.0)
    _decide_in -= delta
    if _decide_in <= 0.0:
        _decide_in = 0.3
        _decide(player)
    _set_action("move_left", _move_dir < 0.0)
    _set_action("move_right", _move_dir > 0.0)
    if _want_jump and player.is_on_floor() and _jump_cooldown <= 0.0:
        _tap_jump()
        _jump_cooldown = 0.35
        _want_jump = false


func _track_unit() -> void:
    # Per-level records, like the competent player's: a level
    # opens when first seen; moving on means it was completed.
    if not _in_scene("game.tscn"):
        return
    var level := int(current_scene.get("level_index"))
    if _unit_level < 0:
        _unit_level = level
        _unit_t0 = _game_time
        _unit_deaths0 = _deaths
    elif level != _unit_level:
        _units.append({
            "unit": _unit_level + 1, "completed": true,
            "deaths": _deaths - _unit_deaths0,
            "time_s": snappedf(_game_time - _unit_t0, 0.01),
        })
        _unit_level = level
        _unit_t0 = _game_time
        _unit_deaths0 = _deaths


func _close_unit(completed: bool) -> void:
    if _unit_level < 0:
        return
    _units.append({
        "unit": _unit_level + 1, "completed": completed,
        "deaths": _deaths - _unit_deaths0,
        "time_s": snappedf(_game_time - _unit_t0, 0.01),
    })
    _unit_level = -1


func _decide(player: CharacterBody2D) -> void:
    var goal := Vector2.ZERO
    var have_goal := false
    var nearest := 1e18
    var chosen: Node2D = null
    for star in get_nodes_in_group("collectibles"):
        if _ignored_until.get(star.get_instance_id(), 0.0) > _game_time:
            continue
        var spot := (star as Node2D).global_position
        var dist := player.global_position.distance_squared_to(spot)
        if dist < nearest:
            nearest = dist
            goal = spot
            chosen = star
            have_goal = true
    if chosen != null:
        if chosen.get_instance_id() != _goal_id:
            _goal_id = chosen.get_instance_id()
            _goal_since = _game_time
        elif _game_time - _goal_since > 18.0:
            # A real newcomer gives up on a goal that is not working
            # and tries another one for a while.
            _ignored_until[_goal_id] = _game_time + 30.0
            _goal_id = 0
    if not have_goal:
        var doors := get_nodes_in_group("exit_door")
        if not doors.is_empty():
            goal = (doors[0] as Node2D).global_position
            have_goal = true
    if not have_goal:
        _move_dir = 0.0
        _want_jump = false
        return
    var dx := goal.x - player.global_position.x
    _move_dir = signf(dx) if absf(dx) > 12.0 else 0.0
    _want_jump = false
    if player.is_on_floor():
        if goal.y < player.global_position.y - 26.0:
            _want_jump = true
        elif _move_dir != 0.0 and not _ground_ahead(player, _move_dir):
            _want_jump = true


func _ground_ahead(player: CharacterBody2D, facing: float) -> bool:
    var space := player.get_world_2d().direct_space_state
    var from := player.global_position + Vector2(facing * 30.0, 4.0)
    var to := from + Vector2(0, 54.0)
    var query := PhysicsRayQueryParameters2D.create(from, to)
    query.exclude = [player.get_rid()]
    return not space.intersect_ray(query).is_empty()


func _tap_jump() -> void:
    var press := InputEventKey.new()
    press.physical_keycode = KEY_SPACE
    press.pressed = true
    Input.parse_input_event(press)
    var release := InputEventKey.new()
    release.physical_keycode = KEY_SPACE
    release.pressed = false
    Input.parse_input_event(release)


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
    if _unit_level >= 0:
        _close_unit(false)  # still mid-level: it was not completed
    var data := {
        "player": "first_time", "mode": "explore", "ok": ok,
        "reason": reason, "time_s": snappedf(_game_time, 0.01),
        "deaths": _deaths,
        "first_objective_s": snappedf(_first_objective_s, 0.01),
        "finished": _finished, "outcome": _outcome, "units": _units,
        "findings": [] if _outcome != "" else ["did not finish within the run"],
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
