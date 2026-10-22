extends SceneTree

## Save-compatibility check — shipped by Weaverbird in every built
## game. After a patch, the factory places a save written by the
## PREVIOUS build into user:// and runs this script: it must parse
## the saved run stats through the game's own SettingsStore and
## boot the main menu without errors.
##
## Prints one SAVECHECK JSON line: {"ok": true, "games_played": N,
## "wins": N, "best_targets": N} and quits 0; quits 1 otherwise.
##
## Run: godot --headless --path . --script tests/savecheck.gd

var _frames: int = 0
var _booted: bool = false


func _initialize() -> void:
    var store = load("res://scripts/settings_store.gd")
    if store == null:
        print("SAVECHECK ", JSON.stringify({"ok": false, "reason": "settings_store.gd failed to load"}))
        quit(1)
        return
    var state: Dictionary = store.load_state()
    var packed: PackedScene = load("res://scenes/main_menu.tscn")
    if packed == null:
        print("SAVECHECK ", JSON.stringify({"ok": false, "reason": "main_menu.tscn failed to load", "games_played": int(state.get("games_played", 0))}))
        quit(1)
        return
    var menu: Node = packed.instantiate()
    root.add_child(menu)
    _booted = true
    print("SAVECHECK ", JSON.stringify({
        "ok": true,
        "games_played": int(state.get("games_played", 0)),
        "wins": int(state.get("wins", 0)),
        "best_targets": int(state.get("best_targets", 0)),
    }))


func _process(_delta: float) -> bool:
    _frames += 1
    if _booted and _frames > 20:
        quit(0)
        return true
    return false
