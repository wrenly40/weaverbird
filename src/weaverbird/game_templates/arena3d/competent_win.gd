extends SceneTree

## Scripted player: COMPETENT (3D arena). Plays the 3D arena
## collector to a win through the game's real logic — it steers
## the player with the real input actions, reading only what a
## player could see via the game's bridge_state() (player,
## collectible and hazard positions as [x, y, z]; it steers on
## the XZ plane and ignores y), and never touches game state.
## Prints one WBTEST JSON line with the result, plus SMOKE PASS
## so this file also serves as the project's flow smoke.
##
## Run: godot --headless --path . --script tests/competent_win.gd

const TIMEOUT: float = 240.0

var _phase: int = 0
var _wait: float = 0.0
var _elapsed: float = 0.0
var _game_time: float = 0.0
var _frames: int = 0
var _start_ticks: int = 0
var _load_ms: int = 0
var _menu: Node
var _deaths: int = 0
var _done: bool = false
# Per-unit (per-round) records for the balance lab.
var _units: Array = []
var _unit_round: int = -1
var _unit_t0: float = 0.0
# Obstacle handling: veer around blocks, and if wedged against
# one anyway, detour sideways like a player would.
var _last_here := Vector2(1e9, 1e9)
var _stuck_for: float = 0.0
var _detour: float = 0.0
var _detour_dir := Vector2.ZERO
var _detour_sign: float = 1.0


func _initialize() -> void:
	_start_ticks = Time.get_ticks_msec()
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
		_release_all()
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
				_release_all()
				_close_unit(true, 0)
				print("SMOKE PASS: menu -> game -> clear every round -> win screen")
				_report(true, "")
				return true
			if _in_scene("game_over.tscn"):
				_release_all()
				_close_unit(false, 1)
				_report(false, "the competent player lost the run")
				return true
			if current_scene != null and _load_ms == 0:
				_load_ms = Time.get_ticks_msec() - _start_ticks
			_steer(delta)
	return false


func _in_scene(suffix: String) -> bool:
	return current_scene != null and current_scene.scene_file_path.ends_with(suffix)


func _facts() -> Dictionary:
	if current_scene != null and current_scene.has_method("bridge_state"):
		return current_scene.call("bridge_state")
	return {}


func _track_unit(facts: Dictionary) -> void:
	# Per-round records: a round opens when first seen; when the
	# round index moves on, the round just left was completed.
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


func _steer(delta: float) -> void:
	if not _in_scene("game.tscn"):
		return
	var facts := _facts()
	_track_unit(facts)
	var player_pos: Array = facts.get("player_pos", [])
	var targets: Array = facts.get("collectibles_pos", [])
	if player_pos.size() < 3 or targets.is_empty():
		_release_all()
		return
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
	var direction := Vector2.ZERO
	if to_goal.length() > 0.4:
		direction = to_goal.normalized()
	# Hazard awareness: veer away from a hazard that is close.
	for hazard in facts.get("hazards_pos", []):
		var spot := Vector2(float(hazard[0]), float(hazard[2]))
		var away := here - spot
		var dist := away.length()
		if dist < 3.6 and dist > 0.05:
			direction += away.normalized() * (3.6 - dist) / 3.6 * 1.6
	# Obstacle awareness: veer around the blocks (their centres
	# and half-size come from the bridge facts).
	for obstacle in facts.get("obstacles_pos", []):
		var spot := Vector2(float(obstacle[0]), float(obstacle[1]))
		var reach := float(obstacle[2]) + 1.1
		var away := here - spot
		var dist := away.length()
		if dist < reach + 1.6 and dist > 0.05:
			direction += away.normalized() \
				* (reach + 1.6 - dist) / (reach + 1.6) * 1.8
	# Stuck against a block anyway? Detour sideways for a beat,
	# alternating sides — what a player does without thinking.
	if direction.length() > 0.05 and here.distance_to(_last_here) < 0.03:
		_stuck_for += delta
	else:
		_stuck_for = 0.0
	_last_here = here
	if _detour > 0.0:
		_detour -= delta
		direction = _detour_dir
	elif _stuck_for > 0.6 and to_goal.length() > 0.4:
		_detour_sign = -_detour_sign
		var forward := to_goal.normalized()
		_detour_dir = Vector2(-forward.y, forward.x) * _detour_sign
		_detour = 0.8
		_stuck_for = 0.0
		direction = _detour_dir
	if direction.length() > 0.05:
		direction = direction.normalized()
		_apply_direction(direction)
	else:
		_release_all()


func _apply_direction(direction: Vector2) -> void:
	_set_action("move_left", direction.x < -0.35)
	_set_action("move_right", direction.x > 0.35)
	_set_action("move_up", direction.y < -0.35)
	_set_action("move_down", direction.y > 0.35)


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
		_close_unit(false, 0)  # still mid-round: not completed
	var fps := 0.0
	if _game_time > 0.5:
		fps = _frames / _game_time
	var data := {
		"player": "competent", "mode": "win", "ok": ok,
		"reason": reason, "time_s": snappedf(_game_time, 0.01),
		"deaths": _deaths, "fps_avg": snappedf(fps, 0.1),
		"load_ms": _load_ms, "findings": [], "units": _units,
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
