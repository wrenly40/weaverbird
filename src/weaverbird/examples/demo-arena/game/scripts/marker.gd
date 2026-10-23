class_name Marker
extends Node2D

## A marker drawn in code — a disc for a target, a square for a
## hazard — or the generated sprite for its kind when the asset
## stage has run. Hazards can patrol sideways.

var kind: String = "target"
var radius: float = 13.0
var colour: Color = Color(0.25, 0.80, 0.45)
var patrol_range: float = 0.0
var patrol_speed: float = 1.4

var _base: Vector2
var _phase: float = 0.0
var _sprite: Sprite2D


func _ready() -> void:
    _base = position
    var file_name := "target.svg" if kind == "target" else "hazard.svg"
    _sprite = WbAssets.add_sprite(self, file_name, radius * 2.3)


func _process(delta: float) -> void:
    if patrol_range > 0.0:
        _phase += delta * patrol_speed
        position.x = _base.x + sin(_phase) * patrol_range


func _draw() -> void:
    if _sprite != null:
        return
    if kind == "hazard":
        var rect := Rect2(-radius, -radius, radius * 2.0, radius * 2.0)
        draw_rect(rect, colour)
        draw_rect(rect, Color(1, 1, 1, 0.7), false, 2.0)
    else:
        draw_circle(Vector2.ZERO, radius, colour)
        draw_circle(Vector2.ZERO, radius * 0.5, Color(1, 1, 1, 0.85))
