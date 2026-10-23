class_name WbAssets
extends RefCounted

## Loads the assets Weaverbird generated into game/assets/ — sprites
## (SVG) and procedural audio (WAV) — or reports them absent, in
## which case the games fall back to code-drawn shapes and silence.
## Nothing in a game may depend on an asset existing.

const ASSET_DIR := "res://assets/"
const AUDIO_DIR := "res://assets/audio/"


static func texture_or_null(file_name: String) -> Texture2D:
    var path := ASSET_DIR + file_name
    if ResourceLoader.exists(path):
        var res: Resource = load(path)
        if res is Texture2D:
            return res as Texture2D
    return null


static func stream_or_null(file_name: String) -> AudioStream:
    var path := AUDIO_DIR + file_name
    if ResourceLoader.exists(path):
        var res: Resource = load(path)
        if res is AudioStream:
            return res as AudioStream
    return null


static func add_sprite(parent: Node, file_name: String, size_px: float) -> Sprite2D:
    var tex := texture_or_null(file_name)
    if tex == null:
        return null
    var sprite := Sprite2D.new()
    sprite.texture = tex
    var base := tex.get_size()
    var longest := maxf(base.x, base.y)
    if longest > 0.0:
        var scale_factor := size_px / longest
        sprite.scale = Vector2(scale_factor, scale_factor)
    parent.add_child(sprite)
    return sprite


static func make_sfx_player(parent: Node, file_name: String) -> AudioStreamPlayer:
    var stream := stream_or_null(file_name)
    if stream == null:
        return null
    var player := AudioStreamPlayer.new()
    player.stream = stream
    parent.add_child(player)
    return player


static func start_music(parent: Node) -> AudioStreamPlayer:
    var stream := stream_or_null("music.wav")
    if stream == null:
        return null
    if stream is AudioStreamWAV:
        (stream as AudioStreamWAV).loop_mode = AudioStreamWAV.LOOP_FORWARD
    var player := AudioStreamPlayer.new()
    player.stream = stream
    player.volume_db = -9.0
    parent.add_child(player)
    player.play()
    return player
