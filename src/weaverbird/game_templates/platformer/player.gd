class_name Player
extends CharacterBody2D

## The platformer player: real platform physics — gravity, run,
## jump with a short coyote window. Drawn as the generated player
## sprite when the asset stage has run, else a rounded body in code.

var speed: float = 280.0
var jump_strength: float = 540.0
var gravity: float = 1400.0
var colour: Color = Color(0.42, 0.30, 0.95)

var _coyote: float = 0.0
var _was_on_floor: bool = false
var _sprite: Sprite2D
var _facing: float = 1.0


func _ready() -> void:
    var shape := CollisionShape2D.new()
    var rect := RectangleShape2D.new()
    rect.size = Vector2(26, 36)
    shape.shape = rect
    add_child(shape)
    _sprite = WbAssets.add_sprite(self, "player.svg", 40.0)


func _physics_process(delta: float) -> void:
    if not is_on_floor():
        velocity.y += gravity * delta
    if is_on_floor():
        _coyote = 0.09
    else:
        _coyote -= delta
    var direction := Input.get_axis("move_left", "move_right")
    velocity.x = direction * speed
    if direction != 0.0:
        _facing = sign(direction)
        if _sprite != null:
            _sprite.flip_h = _facing < 0.0
    if (Input.is_action_just_pressed("interact")
            or Input.is_action_just_pressed("move_up")) and _coyote > 0.0:
        velocity.y = -jump_strength
        _coyote = 0.0
    move_and_slide()


func _draw() -> void:
    if _sprite != null:
        return
    var body := Rect2(-13, -18, 26, 36)
    draw_rect(body, colour)
    draw_circle(Vector2(4 * _facing, -6), 4.0, Color(1, 1, 1, 0.9))
