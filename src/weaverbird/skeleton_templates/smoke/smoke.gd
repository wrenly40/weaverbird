extends SceneTree

## Headless smoke run for the grey-box skeleton. It plays the game
## the way the scenes are wired: main menu -> start the game -> move
## the player onto each target (the game's own collection check does
## the rest) -> win screen. Prints "SMOKE PASS" and exits 0 on
## success; prints "SMOKE FAIL: ..." and exits 1 otherwise.
##
## Run it from this project folder with:
##   godot --headless --path . --script smoke/smoke.gd
## Weaverbird's skeleton builder runs it for you after generating
## the project.

var _phase: int = 0
var _wait: float = 0.0
var _phase_time: float = 0.0
var _menu: Node


func _initialize() -> void:
	var packed: PackedScene = load("res://scenes/main_menu.tscn")
	if packed == null:
		_fail("main_menu.tscn failed to load")
		return
	_menu = packed.instantiate()
	root.add_child(_menu)


func _process(delta: float) -> bool:
	_wait += delta
	_phase_time += delta
	match _phase:
		0:
			if _wait > 0.3:
				_menu.call("start_game")
				_phase = 1
				_phase_time = 0.0
		1:
			if _in_scene("game.tscn"):
				_phase = 2
				_phase_time = 0.0
				_wait = 0.0
			elif _phase_time > 10.0:
				_fail("the game scene did not start within 10 seconds")
		2:
			if _in_scene("win.tscn"):
				print("SMOKE PASS: menu -> game -> collect all targets -> win screen")
				quit(0)
				return true
			if _in_scene("game_over.tscn"):
				_fail("the run ended on the game-over screen instead of the win screen")
			elif _phase_time > 20.0:
				_fail("the win screen was not reached within 20 seconds")
			elif _wait > 0.2:
				_wait = 0.0
				_step_to_next_target()
	return false


func _in_scene(suffix: String) -> bool:
	return current_scene != null and current_scene.scene_file_path.ends_with(suffix)


func _step_to_next_target() -> void:
	if current_scene == null:
		return
	var player := current_scene.get_node_or_null("Player") as Node2D
	if player == null:
		_fail("the game scene has no Player node")
		return
	var targets := get_nodes_in_group("targets")
	if targets.is_empty():
		return
	var target := targets[0] as Node2D
	player.global_position = target.global_position


func _fail(reason: String) -> void:
	print("SMOKE FAIL: " + reason)
	quit(1)
