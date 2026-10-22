extends Node2D

## The grey-box game. One bounded arena, one player, targets to
## collect and hazards to avoid, against the clock:
## - Collect every target before the time runs out to win.
## - Touch a hazard, or let the timer reach zero, and the run is lost.
## The numbers below (round length, target count, colours) were filled
## in from the approved scope when Weaverbird generated this skeleton.

const ACCENT: Color = {{ACCENT_COLOR}}
const TARGET_COLOR: Color = {{TARGET_COLOR}}
const ROUND_SECONDS: float = {{ROUND_SECONDS}}
const TARGET_COUNT: int = {{TARGET_COUNT}}
const ARENA := Rect2(24, 24, 1104, 600)
const BACKGROUND_COLOR := Color(0.07, 0.08, 0.11)
const FLOOR_COLOR := Color(0.13, 0.15, 0.20)

const TARGET_SPOTS: Array = [
	Vector2(120, 110), Vector2(340, 110), Vector2(576, 115), Vector2(810, 110),
	Vector2(1030, 110), Vector2(200, 120), Vector2(120, 540), Vector2(340, 545),
	Vector2(576, 540), Vector2(810, 545), Vector2(1030, 540), Vector2(950, 535),
]

var _player: Player
var _targets: Array[Marker] = []
var _hazards: Array[Marker] = []
var _collected: int = 0
var _time_left: float = ROUND_SECONDS
var _finished: bool = false
var _is_paused: bool = false
var _hud: Label
var _pause_layer: CanvasLayer


func _ready() -> void:
	InputSetup.ensure_actions()
	_player = Player.new()
	_player.name = "Player"
	_player.arena = ARENA
	_player.colour = ACCENT
	_player.position = Vector2(576, 470)
	add_child(_player)
	_spawn_targets()
	_spawn_hazards()
	_build_hud()
	_build_pause_overlay()
	var pause_input: Node = preload("res://scripts/pause_input.gd").new()
	pause_input.process_mode = Node.PROCESS_MODE_ALWAYS
	pause_input.set("on_pause", Callable(self, "_toggle_pause"))
	add_child(pause_input)


func _spawn_targets() -> void:
	var count := mini(TARGET_COUNT, TARGET_SPOTS.size())
	for i in count:
		var marker := Marker.new()
		marker.kind = "target"
		marker.colour = TARGET_COLOR
		var spot: Vector2 = TARGET_SPOTS[i]
		marker.position = spot
		add_child(marker)
		marker.add_to_group("targets")
		_targets.append(marker)


func _spawn_hazards() -> void:
	var defs: Array = [
		[Vector2(400, 330), 170.0],
		[Vector2(740, 452), 140.0],
	]
	for definition in defs:
		var marker := Marker.new()
		marker.kind = "hazard"
		marker.colour = Color(0.88, 0.30, 0.30)
		marker.radius = 14.0
		var spot: Vector2 = definition[0]
		marker.position = spot
		marker.patrol_range = float(definition[1])
		add_child(marker)
		_hazards.append(marker)


func _build_hud() -> void:
	var layer := CanvasLayer.new()
	add_child(layer)
	_hud = Label.new()
	_hud.position = Vector2(36, 30)
	_hud.add_theme_font_size_override("font_size", 18)
	_hud.add_theme_color_override("font_color", Color(0.92, 0.93, 0.96))
	layer.add_child(_hud)
	_update_hud()


func _build_pause_overlay() -> void:
	_pause_layer = CanvasLayer.new()
	_pause_layer.layer = 5
	_pause_layer.process_mode = Node.PROCESS_MODE_ALWAYS
	_pause_layer.visible = false
	add_child(_pause_layer)
	var dim := ColorRect.new()
	dim.color = Color(0, 0, 0, 0.55)
	dim.set_anchors_preset(Control.PRESET_FULL_RECT)
	_pause_layer.add_child(dim)
	var column := UiTheme.centered_column(_pause_layer)
	column.add_child(UiTheme.make_label("Paused", 32))
	var resume := UiTheme.make_button("Resume")
	resume.pressed.connect(_toggle_pause)
	column.add_child(resume)
	var restart := UiTheme.make_button("Restart")
	restart.pressed.connect(_on_restart_pressed)
	column.add_child(restart)
	var menu := UiTheme.make_button("Main menu")
	menu.pressed.connect(_on_menu_pressed)
	column.add_child(menu)


func _process(delta: float) -> void:
	if _finished or _is_paused:
		return
	_time_left -= delta
	if _time_left <= 0.0:
		_time_left = 0.0
		_lose("Time ran out.")
		return
	_check_markers()
	_update_hud()


func _check_markers() -> void:
	for target in _targets.duplicate():
		var reach: float = _player.radius + target.radius
		if _player.global_position.distance_to(target.global_position) <= reach:
			_targets.erase(target)
			target.queue_free()
			_collected += 1
			if _collected >= TARGET_COUNT:
				_win()
				return
	for hazard in _hazards:
		var reach: float = _player.radius + hazard.radius - 2.0
		if _player.global_position.distance_to(hazard.global_position) <= reach:
			_lose("You touched a hazard.")
			return


func _update_hud() -> void:
	_hud.text = "Targets: %d / %d    Time: %d" % [_collected, TARGET_COUNT, int(ceil(_time_left))]


func _toggle_pause() -> void:
	if _finished:
		return
	_is_paused = not _is_paused
	get_tree().paused = _is_paused
	_pause_layer.visible = _is_paused


func _on_restart_pressed() -> void:
	get_tree().paused = false
	get_tree().reload_current_scene()


func _on_menu_pressed() -> void:
	get_tree().paused = false
	get_tree().change_scene_to_file("res://scenes/main_menu.tscn")


func _win() -> void:
	_finished = true
	SettingsStore.record_run(true, _collected)
	RunInfo.result = "win"
	RunInfo.targets_collected = _collected
	RunInfo.targets_total = TARGET_COUNT
	RunInfo.message = "Every target collected with %d seconds to spare." % int(ceil(_time_left))
	get_tree().change_scene_to_file("res://scenes/win.tscn")


func _lose(reason: String) -> void:
	if _finished:
		return
	_finished = true
	SettingsStore.record_run(false, _collected)
	RunInfo.result = "lose"
	RunInfo.targets_collected = _collected
	RunInfo.targets_total = TARGET_COUNT
	RunInfo.message = reason
	get_tree().change_scene_to_file("res://scenes/game_over.tscn")


func _draw() -> void:
	draw_rect(Rect2(0, 0, 1152, 648), BACKGROUND_COLOR)
	draw_rect(ARENA, FLOOR_COLOR)
	draw_rect(ARENA, Color(1, 1, 1, 0.25), false, 2.0)
