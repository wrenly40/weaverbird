extends Node

## The model bridge — how a model playtester plays this game.
##
## Weaverbird's games include this node in tests/model_bridge.gd.
## It does nothing unless the game is launched with --model-bridge
## (a user argument, after "--"), in which case the game's scenes
## add it to the tree root, where it survives scene changes.
##
## Every decision tick (0.75s of game time) it:
##   1. writes bridge/state.json — the scene name plus the facts
##      the current scene honestly exposes via bridge_state()
##      (counts, lives, timer, player position, the hint),
##   2. saves the viewport to bridge/shot.png,
##   3. pauses the tree and waits for bridge/action.json with one
##      action from the scene's bridge_actions() set,
##   4. applies it as real input (Input.action_press, exactly what
##      a keyboard does to the game's polling) and resumes.
##
## The bridge files live in the game's user:// folder, under
## wb_bridge/, where the factory's driver can read and write them.

const TICK := 0.75
const RELEASE_AFTER := 0.35

var _dir := ""
var _accum := 0.0
var _decision := 0
var _waiting := false
var _capturing := false
var _release_in := -1.0
var _held: Array = []

const INPUT_MAP := {
    "move_left": "move_left", "move_right": "move_right",
    "move_up": "move_up", "move_down": "move_down",
    "jump": "interact", "interact": "interact",
}


func _ready() -> void:
    process_mode = Node.PROCESS_MODE_ALWAYS
    _dir = ProjectSettings.localize_path("user://wb_bridge")
    DirAccess.make_dir_recursive_absolute(_dir)


func _state_path() -> String:
    return _dir.path_join("state.json")


func _shot_path() -> String:
    return _dir.path_join("shot.png")


func _action_path() -> String:
    return _dir.path_join("action.json")


func _process(delta: float) -> void:
    if _release_in > 0.0 and not get_tree().paused:
        _release_in -= delta
        if _release_in <= 0.0:
            for action in _held:
                Input.action_release(str(action))
            _held.clear()
    if _waiting:
        if FileAccess.file_exists(_action_path()):
            _apply_action()
        return
    if _capturing:
        return
    _accum += delta
    if _accum >= TICK:
        _accum = 0.0
        _capture()


func _facts() -> Dictionary:
    var facts: Dictionary = {"decision": _decision}
    var scene := get_tree().current_scene
    if scene != null:
        facts["scene"] = scene.scene_file_path
        if scene.has_method("bridge_actions"):
            facts["actions"] = scene.call("bridge_actions")
        else:
            facts["actions"] = ["start", "wait"]
        if scene.has_method("bridge_state"):
            var extra: Variant = scene.call("bridge_state")
            if typeof(extra) == TYPE_DICTIONARY:
                for key in extra:
                    facts[key] = extra[key]
        var player := scene.get_node_or_null("Player") as Node2D
        if player != null and not facts.has("player_pos"):
            facts["player_pos"] = [
                round(player.global_position.x),
                round(player.global_position.y)]
    else:
        facts["scene"] = ""
        facts["actions"] = ["wait"]
    return facts


func _capture() -> void:
    _capturing = true
    _decision += 1
    var facts := _facts()
    await RenderingServer.frame_post_draw
    var image := get_viewport().get_texture().get_image()
    if image != null:
        image.save_png(_shot_path() + ".tmp")
        DirAccess.rename_absolute(_shot_path() + ".tmp", _shot_path())
    var tmp := _state_path() + ".tmp"
    var file := FileAccess.open(tmp, FileAccess.WRITE)
    if file != null:
        file.store_string(JSON.stringify(facts))
        file.close()
        DirAccess.rename_absolute(tmp, _state_path())
    get_tree().paused = true
    _waiting = true
    _capturing = false


func _apply_action() -> void:
    var text := FileAccess.get_file_as_string(_action_path())
    DirAccess.remove_absolute(_action_path())
    var parsed: Variant = JSON.parse_string(text)
    var action := "wait"
    if typeof(parsed) == TYPE_DICTIONARY:
        action = str(parsed.get("action", "wait"))
    _waiting = false
    get_tree().paused = false
    if action == "start":
        var scene := get_tree().current_scene
        if scene != null and scene.has_method("start_game"):
            scene.call("start_game")
        return
    if action == "pause":
        var pressed := InputEventAction.new()
        pressed.action = "pause"
        pressed.pressed = true
        Input.parse_input_event(pressed)
        var released := InputEventAction.new()
        released.action = "pause"
        released.pressed = false
        Input.parse_input_event(released)
        return
    var mapped: String = str(INPUT_MAP.get(action, ""))
    if mapped != "":
        Input.action_press(mapped)
        _held.append(mapped)
        _release_in = RELEASE_AFTER
