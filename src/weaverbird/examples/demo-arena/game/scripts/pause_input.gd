extends Node

## Listens for the pause action on behalf of the game scene. This
## node is set to process always, so the pause key still works while
## the tree is paused (the game's own input stops with the pause).

var on_pause: Callable


func _input(event: InputEvent) -> void:
	if event.is_action_pressed("pause") and on_pause.is_valid():
		on_pause.call()
