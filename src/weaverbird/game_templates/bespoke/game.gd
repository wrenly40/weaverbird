extends Node2D

## Bespoke host — the factory-written shell for a game whose
## mechanics were written as ONE module behind the module
## contract (docs/MODULE_CONTRACT.md). The host owns the shell:
## config, the HUD hint, pause, and the win/lose scene changes
## (via RunInfo, exactly like every other Weaverbird game). The
## module (scripts/bespoke_mechanics.gd) owns the game itself:
## setup(game, config) builds it, bridge_state() exposes its
## facts to the testers, and it ends the run by calling
## signal_win / signal_lose on this host.

var cfg: Dictionary = {}
var _module: Node
var _finished: bool = false
var _hint_label: Label
var _pause_layer: CanvasLayer


func _ready() -> void:
    _maybe_add_model_bridge()
    InputSetup.ensure_actions()
    _load_config()
    var script: GDScript = load("res://scripts/bespoke_mechanics.gd")
    if script == null:
        signal_lose("The bespoke mechanics module failed to load.")
        return
    _module = script.new()
    _module.name = "BespokeMechanics"
    add_child(_module)
    _build_hud()
    var pause_input: Node = preload("res://scripts/pause_input.gd").new()
    pause_input.process_mode = Node.PROCESS_MODE_ALWAYS
    pause_input.set("on_pause", Callable(self, "_toggle_pause"))
    add_child(pause_input)
    if _module.has_method("setup"):
        _module.call("setup", self, cfg)
    _refresh_hint()


func _maybe_add_model_bridge() -> void:
    if OS.get_cmdline_user_args().has("--model-bridge"):
        var bridge: Node = preload("res://tests/model_bridge.gd").new()
        bridge.name = "ModelBridge"
        get_tree().root.call_deferred("add_child", bridge)


func _load_config() -> void:
    var path := "res://game_config.json"
    if FileAccess.file_exists(path):
        var parsed: Variant = JSON.parse_string(
            FileAccess.get_file_as_string(path))
        if typeof(parsed) == TYPE_DICTIONARY:
            cfg = parsed


func _build_hud() -> void:
    _hint_label = Label.new()
    _hint_label.position = Vector2(24, 16)
    _hint_label.add_theme_font_size_override("font_size", 20)
    add_child(_hint_label)
    _pause_layer = CanvasLayer.new()
    _pause_layer.layer = 10
    _pause_layer.visible = false
    var label := Label.new()
    label.text = "Paused"
    label.position = Vector2(520, 300)
    label.add_theme_font_size_override("font_size", 34)
    _pause_layer.add_child(label)
    add_child(_pause_layer)


func _refresh_hint() -> void:
    if _hint_label == null:
        return
    var hint := str(cfg.get("objective_hint", ""))
    if _module != null and _module.has_method("bridge_state"):
        var state: Variant = _module.call("bridge_state")
        if typeof(state) == TYPE_DICTIONARY and state.has("hint"):
            hint = str(state["hint"])
    _hint_label.text = hint


func _toggle_pause() -> void:
    if _finished:
        return
    get_tree().paused = not get_tree().paused
    _pause_layer.visible = get_tree().paused


func signal_win(collected: int, total: int, message: String) -> void:
    if _finished:
        return
    _finished = true
    RunInfo.result = "win"
    RunInfo.targets_collected = collected
    RunInfo.targets_total = total
    RunInfo.message = message
    get_tree().change_scene_to_file("res://scenes/win.tscn")


func signal_lose(reason: String) -> void:
    if _finished:
        return
    _finished = true
    RunInfo.result = "lose"
    RunInfo.targets_collected = 0
    RunInfo.targets_total = 1
    RunInfo.message = reason
    get_tree().change_scene_to_file("res://scenes/game_over.tscn")


func bridge_state() -> Dictionary:
    var facts: Dictionary = {"bespoke": true}
    if _module != null and _module.has_method("bridge_state"):
        var state: Variant = _module.call("bridge_state")
        if typeof(state) == TYPE_DICTIONARY:
            for key in state:
                facts[key] = state[key]
    if not facts.has("hint"):
        facts["hint"] = str(cfg.get("objective_hint", ""))
    return facts


func bridge_actions() -> Array:
    if _module != null and _module.has_method("bridge_actions"):
        var actions: Variant = _module.call("bridge_actions")
        if typeof(actions) == TYPE_ARRAY:
            return actions
    return ["start", "wait", "move_left", "move_right", "move_up",
            "move_down", "jump", "interact", "pause"]
