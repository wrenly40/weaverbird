class_name SettingsStore
extends RefCounted

## Settings and small saved game state, persisted as JSON under
## user:// so they survive between runs:
## - settings.json: the volume setting.
## - game_state.json: games played, wins, best targets collected.

const SETTINGS_PATH := "user://settings.json"
const STATE_PATH := "user://game_state.json"


static func load_json(path: String) -> Dictionary:
	if not FileAccess.file_exists(path):
		return {}
	var file := FileAccess.open(path, FileAccess.READ)
	if file == null:
		return {}
	var parsed: Variant = JSON.parse_string(file.get_as_text())
	if typeof(parsed) == TYPE_DICTIONARY:
		return parsed
	return {}


static func save_json(path: String, data: Dictionary) -> void:
	var file := FileAccess.open(path, FileAccess.WRITE)
	if file == null:
		return
	file.store_string(JSON.stringify(data))


static func get_volume() -> float:
	var data := load_json(SETTINGS_PATH)
	return clampf(float(data.get("music_volume", 0.8)), 0.0, 1.0)


static func set_volume(value: float) -> void:
	var data := load_json(SETTINGS_PATH)
	data["music_volume"] = value
	save_json(SETTINGS_PATH, data)
	apply_volume(value)


static func apply_volume(value: float) -> void:
	var db := -80.0
	if value > 0.01:
		db = linear_to_db(value)
	AudioServer.set_bus_volume_db(0, db)


static func load_state() -> Dictionary:
	var data := load_json(STATE_PATH)
	return {
		"games_played": int(data.get("games_played", 0)),
		"wins": int(data.get("wins", 0)),
		"best_targets": int(data.get("best_targets", 0)),
	}


static func record_run(won: bool, targets: int) -> Dictionary:
	var state := load_state()
	state["games_played"] = int(state["games_played"]) + 1
	if won:
		state["wins"] = int(state["wins"]) + 1
	state["best_targets"] = maxi(int(state["best_targets"]), targets)
	save_json(STATE_PATH, state)
	return state
