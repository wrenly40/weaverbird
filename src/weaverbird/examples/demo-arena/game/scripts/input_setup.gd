class_name InputSetup
extends RefCounted

## The input actions are defined in project.godot. This is a safety
## net: if an action is ever missing (for example after a project
## settings edit), it is recreated here with the same keys, so the
## grey-box controls always work.

static func ensure_actions() -> void:
	_add("move_left", [KEY_A, KEY_LEFT])
	_add("move_right", [KEY_D, KEY_RIGHT])
	_add("move_up", [KEY_W, KEY_UP])
	_add("move_down", [KEY_S, KEY_DOWN])
	_add("pause", [KEY_ESCAPE, KEY_P])
	_add("interact", [KEY_E, KEY_SPACE])


static func _add(action: StringName, keys: Array) -> void:
	if not InputMap.has_action(action):
		InputMap.add_action(action)
	for key in keys:
		var exists := false
		for event in InputMap.action_get_events(action):
			if event is InputEventKey and event.physical_keycode == key:
				exists = true
		if not exists:
			var event := InputEventKey.new()
			event.physical_keycode = key
			InputMap.action_add_event(action, event)
