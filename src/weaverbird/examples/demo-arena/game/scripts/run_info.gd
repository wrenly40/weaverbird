class_name RunInfo
extends RefCounted

## Small bits of state passed between scenes for one run: how the run
## ended, and where the settings screen should go back to. Static, so
## no autoload is needed. Saved, long-lived state lives in
## SettingsStore (user://), not here.

static var result: String = ""
static var deaths: int = 0
static var targets_collected: int = 0
static var targets_total: int = 0
static var targets_label: String = ""
static var message: String = ""
static var settings_return_scene: String = "res://scenes/main_menu.tscn"
