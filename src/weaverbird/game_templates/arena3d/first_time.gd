extends SceneTree

## Scripted player: FIRST-TIME (3D arena). Starts at the main
## menu and presses the real Play button, then plays using only
## what the game teaches: the on-screen hint says to collect the
## glowing orbs, so it walks toward the nearest one it can see —
## positions from the game's bridge_state(), on the XZ plane —
## changing its mind at human-ish intervals and paying no
## attention to hazards (it has not learned them yet). Records
## how long the first collect took and whether it finished. Not
## finishing is an observation, not a failure.
##
## Run: godot --headless --path . --script tests/first_time.gd

const TIMEOUT: float = 150.0

var _phase: int = 0
var _wait: float = 0.0
var _elapsed: float = 0.0
var _game_time: float = 0.0
var _menu: Node
var _done: bool = false
var _decide_in: float = 0.0
var _direction := Vector2.ZERO
var _first_objective_s: float = -1.0
var _finished: bool = false
var _outcome: String = ""
# Per-unit (per-round) records for the balance lab.
var _units: Array = []
var _unit_round: int = -1
var _unit_t0: float = 0.0


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
				if _press_play_button():
					_phase = 1
				else:
					_report(false, "no Play button found on the main menu")
					return true
		1:
			if _in_scene("win.tscn"):
				_finished = true
				_outcome = "win"
				_release_all()
				_close_unit(true, 0)
				_report(true, "")
				return true
			if _in_scene("game_over.tscn"):
				_outcome = "lose"
				_release_all()
				_close_unit(false, 1)
				_report(true, "")
				return true
			_game_time += delta
			_play(delta)
	return false


func _in_scene(suffix: String) -> bool:
	return current_scene != null and current_scene.scene_file_path.ends_with(suffix)


func _press_play_button() -> bool:
	return _find_button(_menu, "Play")


func _find_button(node: Node, text: String) -> bool:
	if node is Button and (node as Button).text == text:
		(node as Button).pressed.emit()
		return true
	for child in node.get_children():
		if _find_button(child, text):
			return true
	return false


func _facts() -> Dictionary:
	if current_scene != null and current_scene.has_method("bridge_state"):
		return current_scene.call("bridge_state")
	return {}


func _track_unit(facts: Dictionary) -> void:
	# Per-round records, like the competent player's.
	if not _in_scene("game.tscn") or facts.is_empty():
		return
	var round_index := int(facts.get("round", 1)) - 1
	if _unit_round < 0:
		_unit_round = round_index
		_unit_t0 = _game_time
	elif round_index != _unit_round:
		_units.append({
			"unit": _unit_round + 1, "completed": true, "deaths": 0,
			"time_s": snappedf(_game_time - _unit_t0, 0.01),
		})
		_unit_round = round_index
		_unit_t0 = _game_time


func _close_unit(completed: bool, deaths: int) -> void:
	if _unit_round < 0:
		return
	_units.append({
		"unit": _unit_round + 1, "completed": completed,
		"deaths": deaths,
		"time_s": snappedf(_game_time - _unit_t0, 0.01),
	})
	_unit_round = -1


func _play(delta: float) -> void:
	if not _in_scene("game.tscn"):
		return
	var facts := _facts()
	_track_unit(facts)
	if _first_objective_s < 0.0 and int(facts.get("collected", 0)) > 0:
		_first_objective_s = _game_time
	var player_pos: Array = facts.get("player_pos", [])
	if player_pos.size() < 3:
		return
	_decide_in -= delta
	if _decide_in <= 0.0:
		_decide_in = 0.3
		var targets: Array = facts.get("collectibles_pos", [])
		if targets.is_empty():
			_direction = Vector2.ZERO
		else:
			var here := Vector2(float(player_pos[0]), float(player_pos[2]))
			var goal := Vector2.ZERO
			var best := 1e18
			for target in targets:
				var spot := Vector2(float(target[0]), float(target[2]))
				var dist := here.distance_squared_to(spot)
				if dist < best:
					best = dist
					goal = spot
			var to_goal := goal - here
			_direction = to_goal.normalized() if to_goal.length() > 0.4 else Vector2.ZERO
	_set_action("move_left", _direction.x < -0.35)
	_set_action("move_right", _direction.x > 0.35)
	_set_action("move_up", _direction.y < -0.35)
	_set_action("move_down", _direction.y > 0.35)


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
	if _unit_round >= 0:
		_close_unit(false, 1 if _outcome == "lose" else 0)
	var data := {
		"player": "first_time", "mode": "explore", "ok": ok,
		"reason": reason, "time_s": snappedf(_game_time, 0.01),
		"deaths": 1 if _outcome == "lose" else 0,
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
	# dummy audio driver, a playback left running is reported as
	# a leaked resource at exit, which would read as a game error.
	var stack: Array = [root]
	while not stack.is_empty():
		var node: Node = stack.pop_back()
		if node is AudioStreamPlayer:
			(node as AudioStreamPlayer).stop()
		for child in node.get_children():
			stack.append(child)
