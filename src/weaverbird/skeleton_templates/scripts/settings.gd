extends Control

## The settings screen: one volume slider, saved the moment it moves
## (SettingsStore writes it to user://), and a way back.

var _slider: HSlider


func _ready() -> void:
	add_child(UiTheme.background())
	var column := UiTheme.centered_column(self)
	column.add_child(UiTheme.make_label("Settings", 36))
	column.add_child(UiTheme.make_label("Volume", 18, UiTheme.TEXT_MUTED))
	_slider = HSlider.new()
	_slider.min_value = 0.0
	_slider.max_value = 1.0
	_slider.step = 0.05
	_slider.custom_minimum_size = Vector2(280, 32)
	_slider.value = SettingsStore.get_volume()
	_slider.value_changed.connect(_on_volume_changed)
	column.add_child(_slider)
	var back := UiTheme.make_button("Back")
	back.pressed.connect(_on_back_pressed)
	column.add_child(back)


func _on_volume_changed(value: float) -> void:
	SettingsStore.set_volume(value)


func _on_back_pressed() -> void:
	get_tree().change_scene_to_file(RunInfo.settings_return_scene)
