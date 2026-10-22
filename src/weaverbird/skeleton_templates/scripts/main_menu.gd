extends Control

## The main menu: title and pitch from the scope, saved stats from
## previous runs, and Play / Settings / Quit.

const GAME_SCENE := "res://scenes/game.tscn"
const SETTINGS_SCENE := "res://scenes/settings.tscn"


func _ready() -> void:
	InputSetup.ensure_actions()
	SettingsStore.apply_volume(SettingsStore.get_volume())
	add_child(UiTheme.background())
	var column := UiTheme.centered_column(self)
	column.add_child(UiTheme.make_label({{TITLE_JSON}}, 44))
	var pitch := UiTheme.make_label({{PITCH_JSON}}, 18, UiTheme.TEXT_MUTED)
	pitch.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	column.add_child(pitch)
	var stats := SettingsStore.load_state()
	var stats_text := "Games played: %d · Wins: %d · Best targets: %d" % [
		int(stats["games_played"]), int(stats["wins"]), int(stats["best_targets"])
	]
	column.add_child(UiTheme.make_label(stats_text, 16, UiTheme.TEXT_MUTED))
	var play := UiTheme.make_button("Play")
	play.pressed.connect(start_game)
	column.add_child(play)
	var settings_button := UiTheme.make_button("Settings")
	settings_button.pressed.connect(_on_settings_pressed)
	column.add_child(settings_button)
	var quit_button := UiTheme.make_button("Quit")
	quit_button.pressed.connect(func() -> void: get_tree().quit())
	column.add_child(quit_button)


func start_game() -> void:
	get_tree().change_scene_to_file(GAME_SCENE)


func _on_settings_pressed() -> void:
	RunInfo.settings_return_scene = "res://scenes/main_menu.tscn"
	get_tree().change_scene_to_file(SETTINGS_SCENE)
