class_name Star
extends Area2D

## A collectible star. Uses the generated target sprite when the
## asset stage has run, else draws a star in code. Emits `collected`
## (through the game's handler) when the player touches it.

var colour: Color = Color(0.25, 0.80, 0.45)
var radius: float = 12.0

var _sprite: Sprite2D
var _bob: float = 0.0
var _base_y: float = 0.0


func _ready() -> void:
    _base_y = position.y
    var shape := CollisionShape2D.new()
    var circle := CircleShape2D.new()
    circle.radius = radius + 4.0
    shape.shape = circle
    add_child(shape)
    _sprite = WbAssets.add_sprite(self, "target.svg", radius * 2.4)
    add_to_group("collectibles")
    add_to_group("targets")


func _process(delta: float) -> void:
    _bob += delta * 2.2
    position.y = _base_y + sin(_bob) * 3.0


func _draw() -> void:
    if _sprite != null:
        return
    var points := PackedVector2Array()
    for i in 10:
        var angle := -PI / 2.0 + i * PI / 5.0
        var r := radius if i % 2 == 0 else radius * 0.45
        points.append(Vector2(cos(angle), sin(angle)) * r)
    draw_colored_polygon(points, colour)
