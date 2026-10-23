extends Control

## The game-over screen: why the run ended, and Play again / Main menu.

const GAME_SCENE := "res://scenes/game.tscn"
const MENU_SCENE := "res://scenes/main_menu.tscn"


func _ready() -> void:
	add_child(UiTheme.background())
	var column := UiTheme.centered_column(self)
	column.add_child(UiTheme.make_label("Game over", 40))
	var message := RunInfo.message
	if message == "":
		message = "The run ended."
	column.add_child(UiTheme.make_label(message, 18, UiTheme.TEXT_MUTED))
	var score := "Targets: %d / %d" % [RunInfo.targets_collected, RunInfo.targets_total]
	column.add_child(UiTheme.make_label(score, 18))
	var again := UiTheme.make_button("Play again")
	again.pressed.connect(func() -> void: get_tree().change_scene_to_file(GAME_SCENE))
	column.add_child(again)
	var menu := UiTheme.make_button("Main menu")
	menu.pressed.connect(func() -> void: get_tree().change_scene_to_file(MENU_SCENE))
	column.add_child(menu)
