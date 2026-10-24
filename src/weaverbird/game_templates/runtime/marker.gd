class_name Marker
extends Node2D

## A world entity drawn in code — or the generated sprite for its
## kind when the asset stage has run. One marker class serves
## every entity kind the generic runtime places in a top-down
## world; what it DOES is its behaviour, assigned by the plan and
## carried in the world data:
##
##   static   — stays where the world put it
##   patrol   — sweeps sideways around its base (hazards)
##   wander   — drifts between nearby points (any kind)
##   chase    — moves toward the player (hazards)
##   flee     — moves away when the player comes close (prey-like
##              collectibles)
##   ambient  — wanders; decoration, neither collectible nor hazard
##
## The game reads positions and groups ("targets", "hazards",
## "goal"); the marker only moves itself.

var kind: String = "target"
var behaviour: String = "static"
var radius: float = 13.0
var colour: Color = Color(0.25, 0.80, 0.45)
var patrol_range: float = 0.0
var patrol_speed: float = 1.4
var move_speed: float = 90.0
var bounds: Rect2 = Rect2(24, 24, 1104, 600)
var target_node: Node2D

var _base: Vector2
var _phase: float = 0.0
var _sprite: Sprite2D
var _wander_goal: Vector2
var _wander_wait: float = 0.0
var _rng := RandomNumberGenerator.new()


func _ready() -> void:
    _base = position
    _wander_goal = position
    _rng.seed = int(global_position.x * 131.0 + global_position.y * 17.0) + 7
    var file_name := "target.svg"
    if kind == "hazard":
        file_name = "hazard.svg"
    _sprite = WbAssets.add_sprite(self, file_name, radius * 2.3)


func _process(delta: float) -> void:
    match behaviour:
        "patrol":
            if patrol_range > 0.0:
                _phase += delta * patrol_speed
                position.x = _base.x + sin(_phase) * patrol_range
        "wander", "ambient":
            _wander(delta)
        "chase":
            if target_node != null:
                var to := target_node.global_position - global_position
                if to.length() > 4.0:
                    position += to.normalized() * move_speed * delta
                    _clamp_to_bounds()
        "flee":
            if target_node != null:
                var away := global_position - target_node.global_position
                if away.length() < 150.0 and away.length() > 0.5:
                    position += away.normalized() * move_speed * delta
                    _clamp_to_bounds()


func _wander(delta: float) -> void:
    if _wander_wait > 0.0:
        _wander_wait -= delta
        return
    var to := _wander_goal - position
    if to.length() < 6.0:
        _wander_wait = _rng.randf_range(0.4, 1.6)
        _wander_goal = _base + Vector2(
            _rng.randf_range(-90.0, 90.0), _rng.randf_range(-70.0, 70.0))
        _wander_goal.x = clampf(_wander_goal.x,
                                bounds.position.x + radius,
                                bounds.end.x - radius)
        _wander_goal.y = clampf(_wander_goal.y,
                                bounds.position.y + radius,
                                bounds.end.y - radius)
        return
    position += to.normalized() * move_speed * delta


func _clamp_to_bounds() -> void:
    position.x = clampf(position.x, bounds.position.x + radius,
                        bounds.end.x - radius)
    position.y = clampf(position.y, bounds.position.y + radius,
                        bounds.end.y - radius)


func _draw() -> void:
    if _sprite != null:
        return
    if kind == "hazard":
        var rect := Rect2(-radius, -radius, radius * 2.0, radius * 2.0)
        draw_rect(rect, colour)
        draw_rect(rect, Color(1, 1, 1, 0.7), false, 2.0)
    elif kind == "goal":
        # A small flag: pole and pennant in the entity's colour.
        draw_line(Vector2(0, radius), Vector2(0, -radius - 8.0),
                  Color(0.92, 0.93, 0.96), 3.0)
        draw_colored_polygon(PackedVector2Array([
            Vector2(0, -radius - 8.0), Vector2(radius + 6.0, -radius),
            Vector2(0, -radius + 4.0)]), colour)
        draw_circle(Vector2.ZERO, 5.0, Color(1, 1, 1, 0.85))
    elif kind == "ambient":
        draw_circle(Vector2.ZERO, radius * 0.55,
                    Color(colour, 0.55))
    else:
        draw_circle(Vector2.ZERO, radius, colour)
        draw_circle(Vector2.ZERO, radius * 0.5, Color(1, 1, 1, 0.85))
