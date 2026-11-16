# Test Mod  ->  Bedrock

* source: `testmod.jar`  (Fabric, id `testmod`, version `2.0`, 2 classes)
* mod: **events**  -> `java/mods/testmod/`
* add-on: **1 item(s), 1 block(s)**  -> `mods-in/converted/testmod/addon/` (zip both folders as a .mcaddon)

## Events
| Java source | Bedrock event | status | rewritten actions |
|---|---|---|---|
| TestMod: block broken | `block.break` | native hook | 1 |
| TestMod: HUD drawing | `hud` | HUD (drawn on the 1 s tick) | 1 |
| LevelMixin: Mixin into ServerLevel (level tick) | `level.tick` | native hook | 1 |
| TestMod: connection JOIN | `world.join` | native hook | 1 |
| TestMod: connection DISCONNECT | `world.leave` | native hook | 1 |

## Handlers read from bytecode
* `block.break` <- `TestMod.onBreak` (block broken (BlockEvent$BreakEvent))
  * rewritten: log "A block was broken"
* `hud` <- `TestMod.lambda$onInitialize$2` (HUD drawing)
  * rewritten: hud "FPS: " + Math.round(Bedrock.game().fps())
* `level.tick` <- `LevelMixin.onTick` (Mixin into ServerLevel.tick)
  * rewritten: log "tick!"
* `world.join` <- `TestMod.lambda$onInitialize$0` (connection JOIN)
  * rewritten: toast "Welcome to Test World!"
  * not rewritten (Java Edition calls with no Bedrock equivalent): `ServerPlayer.heal`
* `world.leave` <- `TestMod.lambda$onInitialize$1` (connection DISCONNECT)
  * rewritten: log "bye"

## Mixins mapped
* `LevelMixin` into `ServerLevel` -> `level.tick` (level tick)

## NOT converted
* Items/blocks have identity, name, texture and a plain full-block / stack-of-64 definition only. Their Java behaviour (use, break, tick, recipes, loot, world gen) is not converted.
* Handler lines that call `net.minecraft.*` for things other than messages, logging and drawn text (health, inventory, entities, blocks, sounds, particles) have no Bedrock-API equivalent and are listed above.
