class_name Player
extends CharacterBody2D

## The generic runtime's player controller. One class, two
## movement modes — the plan decides which:
##
##   "top"  — free top-down movement inside `bounds`
##            (top_down_2d plans; the 3D movement has its own
##            player3d.gd, and grid_step games move in game.gd)
##   "side" — side-view physics: gravity, run, jump with a
##            short coyote window (side_view plans)
##
## Speeds and physics come from game_config.json, written by the
## factory — tuning is data, not template surgery.

var mode: String = "top"
var bounds: Rect2 = Rect2(24, 24, 1104, 600)
var speed: float = 330.0
var radius: float = 15.0
var jump_strength: float = 545.0
var gravity: float = 1400.0
var colour: Color = Color(0.42, 0.30, 0.95)

var _coyote: float = 0.0
var _sprite: Sprite2D
var _facing: float = 1.0


func _ready() -> void:
    var shape := CollisionShape2D.new()
    if mode == "side":
        var rect := RectangleShape2D.new()
        rect.size = Vector2(26, 36)
        shape.shape = rect
    else:
        var circle := CircleShape2D.new()
        circle.radius = radius
        shape.shape = circle
    add_child(shape)
    _sprite = WbAssets.add_sprite(self, "player.svg",
                                  40.0 if mode == "side" else radius * 2.4)


func _physics_process(delta: float) -> void:
    if mode == "side":
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
                or Input.is_action_just_pressed("move_up")) \
                and _coyote > 0.0:
            velocity.y = -jump_strength
            _coyote = 0.0
        move_and_slide()
    else:
        var direction := Input.get_vector(
            "move_left", "move_right", "move_up", "move_down")
        velocity = direction * speed
        move_and_slide()
        position.x = clampf(position.x, bounds.position.x + radius,
                            bounds.end.x - radius)
        position.y = clampf(position.y, bounds.position.y + radius,
                            bounds.end.y - radius)


func _draw() -> void:
    if _sprite != null:
        return
    if mode == "side":
        var body := Rect2(-13, -18, 26, 36)
        draw_rect(body, colour)
        draw_circle(Vector2(4 * _facing, -6), 4.0, Color(1, 1, 1, 0.9))
    else:
        draw_circle(Vector2.ZERO, radius, colour)
        draw_circle(Vector2.ZERO, radius * 0.45, Color(1, 1, 1, 0.85))
