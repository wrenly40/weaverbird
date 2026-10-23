extends SceneTree

## Scripted player: COMPETENT, losing on purpose (3D arena).
## Steers straight into the nearest hazard — read from the
## game's bridge_state() facts, on the XZ plane — through the
## real input actions, to prove the lose path works end to end:
## hazard -> game over. Prints one WBTEST JSON line.
##
## Run: godot --headless --path . --script tests/competent_lose.gd

const TIMEOUT: float = 60.0

var _phase: int = 0
var _wait: float = 0.0
var _elapsed: float = 0.0
var _game_time: float = 0.0
var _menu: Node
var _done: bool = false


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
				_release_all()
				_report(true, "")
				return true
			if _in_scene("win.tscn"):
				_release_all()
				_report(false, "reached the win screen on a losing run")
				return true
			_steer_to_hazard()
	return false


func _in_scene(suffix: String) -> bool:
	return current_scene != null and current_scene.scene_file_path.ends_with(suffix)


func _steer_to_hazard() -> void:
	if not _in_scene("game.tscn"):
		return
	if current_scene == null or not current_scene.has_method("bridge_state"):
		return
	var facts: Dictionary = current_scene.call("bridge_state")
	var player_pos: Array = facts.get("player_pos", [])
	var hazards: Array = facts.get("hazards_pos", [])
	if player_pos.size() < 3 or hazards.is_empty():
		_release_all()
		return
	var here := Vector2(float(player_pos[0]), float(player_pos[2]))
	var goal := Vector2.ZERO
	var best := 1e18
	for hazard in hazards:
		var spot := Vector2(float(hazard[0]), float(hazard[2]))
		var dist := here.distance_squared_to(spot)
		if dist < best:
			best = dist
			goal = spot
	var direction := goal - here
	if direction.length() > 0.25:
		direction = direction.normalized()
		_set_action("move_left", direction.x < -0.3)
		_set_action("move_right", direction.x > 0.3)
		_set_action("move_up", direction.y < -0.3)
		_set_action("move_down", direction.y > 0.3)
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
		"player": "competent", "mode": "lose", "ok": ok,
		"reason": reason, "time_s": snappedf(_game_time, 0.01),
		"deaths": 1 if ok else 0, "findings": [],
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
