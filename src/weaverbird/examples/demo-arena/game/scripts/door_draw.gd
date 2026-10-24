extends Node2D

## Code-drawn exit door, used only when the asset stage has not
## generated an exit door sprite.

var colour: Color = Color(0.42, 0.30, 0.95)


func _draw() -> void:
    var rect := Rect2(-18, -24, 36, 48)
    draw_rect(rect, colour)
    draw_rect(rect, Color(1, 1, 1, 0.75), false, 2.0)
    draw_circle(Vector2(10, 0), 3.5, Color(1, 1, 1, 0.9))
