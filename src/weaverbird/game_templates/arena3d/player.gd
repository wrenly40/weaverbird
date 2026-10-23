extends CharacterBody3D
## The 3D arena player: a capsule steered by the standard
## input actions across the ground plane. Screen-relative: the
## camera watches from +Z, so "up" on the stick is -Z (away).

var speed := 11.0


func _ready() -> void:
	# The accent lives at the top of game_config.json (D9); a
	# nested "palette" object, if present, overrides it.
	var accent_hex := "#FF7A1A"
	if FileAccess.file_exists("res://game_config.json"):
		var parsed = JSON.parse_string(
			FileAccess.get_file_as_string("res://game_config.json"))
		if typeof(parsed) == TYPE_DICTIONARY:
			accent_hex = String(parsed.get("accent_hex", accent_hex))
			var nested = parsed.get("palette", {})
			if typeof(nested) == TYPE_DICTIONARY \
					and nested.has("accent_hex"):
				accent_hex = String(nested["accent_hex"])
	var mesh := MeshInstance3D.new()
	var capsule := CapsuleMesh.new()
	capsule.radius = 0.42
	capsule.height = 1.5
	var material := StandardMaterial3D.new()
	material.albedo_color = Color(accent_hex)
	material.roughness = 0.6
	capsule.material = material
	mesh.mesh = capsule
	mesh.position = Vector3(0, 0.75, 0)
	add_child(mesh)
	var shape := CollisionShape3D.new()
	var body := CapsuleShape3D.new()
	body.radius = 0.42
	body.height = 1.5
	shape.shape = body
	shape.position = Vector3(0, 0.75, 0)
	add_child(shape)


func _physics_process(delta: float) -> void:
	var input_dir := Vector2(
		Input.get_action_strength("move_right")
			- Input.get_action_strength("move_left"),
		Input.get_action_strength("move_down")
			- Input.get_action_strength("move_up"))
	if input_dir.length() > 1.0:
		input_dir = input_dir.normalized()
	velocity.x = input_dir.x * speed
	velocity.z = input_dir.y * speed
	if not is_on_floor():
		velocity.y -= 22.0 * delta
	else:
		velocity.y = -0.5
	move_and_slide()
