class_name UiTheme
extends RefCounted

## The grey-box look: plain dark panels, one accent colour, big clean
## text. Everything is built in code so the skeleton needs no scene
## layout work and no external assets.

const BACKGROUND := Color(0.09, 0.10, 0.14)
const TEXT := Color(0.92, 0.93, 0.96)
const TEXT_MUTED := Color(0.62, 0.66, 0.74)
const ACCENT: Color = Color(0.608, 0.549, 0.949)


static func background() -> ColorRect:
	var rect := ColorRect.new()
	rect.color = BACKGROUND
	rect.set_anchors_preset(Control.PRESET_FULL_RECT)
	return rect


static func make_label(text: String, size: int, colour: Color = TEXT) -> Label:
	var label := Label.new()
	label.text = text
	label.add_theme_font_size_override("font_size", size)
	label.add_theme_color_override("font_color", colour)
	label.horizontal_alignment = HORIZONTAL_ALIGNMENT_CENTER
	return label


static func make_button(text: String) -> Button:
	var button := Button.new()
	button.text = text
	button.custom_minimum_size = Vector2(240, 44)
	button.add_theme_font_size_override("font_size", 18)
	return button


static func centered_column(parent: Node) -> VBoxContainer:
	var center := CenterContainer.new()
	center.set_anchors_preset(Control.PRESET_FULL_RECT)
	parent.add_child(center)
	var column := VBoxContainer.new()
	column.add_theme_constant_override("separation", 14)
	center.add_child(column)
	return column
