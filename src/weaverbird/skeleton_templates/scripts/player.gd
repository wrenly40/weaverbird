class_name Player
extends Node2D

## The grey-box player: a coloured disc drawn in code (placeholder
## art — later stages replace the drawing, not the movement).
## Moves with the move_* input actions and stays inside the arena.

var arena: Rect2 = Rect2(24, 24, 1104, 600)
var speed: float = 330.0
var radius: float = 15.0
var colour: Color = {{ACCENT_COLOR}}


func _process(delta: float) -> void:
	var direction := Input.get_vector("move_left", "move_right", "move_up", "move_down")
	if direction != Vector2.ZERO:
		position += direction * speed * delta
		position.x = clampf(position.x, arena.position.x + radius, arena.end.x - radius)
		position.y = clampf(position.y, arena.position.y + radius, arena.end.y - radius)


func _draw() -> void:
	draw_circle(Vector2.ZERO, radius, colour)
	draw_circle(Vector2.ZERO, radius * 0.45, Color(1, 1, 1, 0.85))
