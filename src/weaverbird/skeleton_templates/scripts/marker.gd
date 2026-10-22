class_name Marker
extends Node2D

## A grey-box marker drawn in code: a disc for a target to collect,
## a square for a hazard to avoid. Hazards can patrol sideways.

var kind: String = "target"
var radius: float = 13.0
var colour: Color = {{TARGET_COLOR}}
var patrol_range: float = 0.0
var patrol_speed: float = 1.4

var _base: Vector2
var _phase: float = 0.0


func _ready() -> void:
	_base = position


func _process(delta: float) -> void:
	if patrol_range > 0.0:
		_phase += delta * patrol_speed
		position.x = _base.x + sin(_phase) * patrol_range


func _draw() -> void:
	if kind == "hazard":
		var rect := Rect2(-radius, -radius, radius * 2.0, radius * 2.0)
		draw_rect(rect, colour)
		draw_rect(rect, Color(1, 1, 1, 0.7), false, 2.0)
	else:
		draw_circle(Vector2.ZERO, radius, colour)
		draw_circle(Vector2.ZERO, radius * 0.5, Color(1, 1, 1, 0.85))
