# Native hooks: what was found, how, and what is still open

Analysed: the `libminecraftpe.so` you supplied (arm64-v8a, stripped, ~342 MB). Patterns match **that build**; on another
build a missing pattern shows `name=missing` and the hook is skipped (re-run `tools/find_hooks.py` on the new library).

## Method (`tools/find_hooks.py`, helpers in `tools/binxref.py`)
The library exports no game symbols, but it embeds readable names. Three anchors are used:
* **fn**: code that loads a function-name string (profiler/assert text such as `void ClientInstance::requestLeaveGame(bool, bool)`).
* **slot**: the same kind of string reached through a data-table pointer (relocation); the function using that slot.
* **evt**: telemetry event names (`BlockBroken`, `MobKilled`, ...). The *virtual* function that references the name is the
  game's `EventingProducer::fireEvent*` call, which the game runs when the thing happens.
The function start comes from `.eh_frame_hdr`; its first 12-16 instructions become a pattern (branches/ADRP wildcarded),
kept only if it matches exactly one place in `.text`. Virtual candidates were cross-checked: each appears in a vtable.

## Hooks (all 17 located, unique, installed through `pl::memory::hook`)
| event | game function (from its anchor) |
|---|---|
| `world.join` | `ClientInstance::_joinWorldInProgressSubclient` |
| `world.leave` | `ClientInstance::requestLeaveGame(bool,bool)` |
| `player.login` | `ServerNetworkHandler::_onSubClientAuthenticated` |
| `player.tick` | `Player::tickWorld(const Tick&)` (virtual; Player, ServerPlayer, LocalPlayer) |
| `level.tick` | `Level::tick()` (virtual; Level, ServerLevel, MultiPlayerLevel) |
| `game.update` | `MinecraftGame` update wrapper, passed to Java at most 20/s |
| `block.break` `block.place` `mob.kill` `player.die` `mob.interact` `item.use` `item.drop` `item.craft` `player.teleport` | telemetry `fireEvent*` for BlockBroken, BlockPlaced, MobKilled, PlayerDied, MobInteracted, ItemUsed, ItemDropped, ItemCrafted, PlayerTeleported |
| `chat.send` | the function that fires `PlayerMessage` |
| `chat.display` | the formatter that uses `chat.type.text` |

## Argument layout: no longer a risk
Every hook uses one hand-written arm64 thunk that saves all argument registers (x0-x8, q0-q7), raises the event, restores
them and jumps into the original. It never reads an argument, so the real signature does not matter. (The thunk was
assembled with clang for aarch64 and the offsets checked in the disassembly.) Events fire just *before* the game function.

## player.attack: tried, not located
Tried: string anchors (no readable name, telemetry event or schema leads to the attack function), the script-API event names
(`EntityHitEntityAfterEvent` etc. only reach the module's registration code, which runs once), and RTTI/vtable lookup for
`Player`, `LocalPlayer`, `ServerPlayer` (found, but without symbols the slot of `attack` cannot be identified, and the
inventory-transaction classes have no RTTI name in this build). The event name `player.attack` is wired end to end: drop a
pattern in `<dataDir>/signatures/player.attack.txt` (found in a disassembler, same format as the other files) and the hook
installs on next start; converted mods that listen to attacks are generated against it. `mob.kill` and `player.die` fire today.
`tools/binxref.py` has the helpers used (vtable_of, refs_to, fn_strings).

## player.attack: second attempt (this libminecraftpe.so, 342 MB)
Re-ran `tools/find_hooks.py` on this exact library: all 17 patterns are found, unique, and identical to `Signatures.inc`.
Searched again for the attack function: the script-API names (`PlayerItemAttackEntityBeforeEvent`, `EntityHitEntityAfterEvent`,
`EntityHitEventSignal`, `playerItemAttackEntity`) only resolve to script-module registration code (run once at startup, not on a hit).
No readable function name or telemetry event exists for the attack. A pattern was NOT guessed: a detour on the wrong function crashes the game.
To finish it, find `Player::attack(Actor&, ActorDamageCause const&)` in a disassembler (it is a virtual in the Player vtable, next to
`tickWorld`, which `tools/binxref.py vtable_of` can list) and save its pattern as `signatures/player.attack.txt`.

## Notes
* **Single-player join:** `world.join` uses `_joinWorldInProgressSubclient`, whose name suggests it may not run for an ordinary
  local join. If `hook.first` never prints `world.join`, use `player.tick` (first tick) or `block.*` events as the signal.
* **Not tested on a device.** Located + unique + assembled is verified in the file; runtime behaviour is not. Telemetry
  `fireEvent*` functions may be skipped by the game in some modes (the event is then simply not raised).
