#!/usr/bin/env python3
"""
mods-in/convert.py - rewrite a Java Edition mod (.jar) as a Bedrock mod that uses the ModHost Bedrock API.

    python3 mods-in/convert.py                    # every *.jar in mods-in/
    python3 mods-in/convert.py my-mod.jar         # one jar
    python3 mods-in/convert.py --install          # also copy the result to java/mods/<id>/ (built by scripts/build-java.sh)

Nothing from the jar is executed or loaded. The converter READS it (class files, fabric.mod.json, mods.toml, mixin
configs) and WRITES new Java source that targets com.modhost.bedrock:

  1. Known mods (Lithium, Sodium, FPS counters, ...)   -> a Bedrock port of what the mod does for the player.
  2. Game events the mod listens to (Fabric/NeoForge/Forge events and Mixin injection points)
                                                       -> Events.on("<bedrock event>") for the 17 native hooks.
  3. Chat/toast messages the event code sends          -> Bedrock.hud().toast(...) on the same event.
  4. Everything else (rendering, world generation, new blocks/items/entities, Mixins into code that Bedrock does not
     expose)                                           -> listed in REPORT.md as NOT converted, with the reason.

It does not translate arbitrary Java logic. REPORT.md says exactly what was and was not carried over.
"""
import json, pathlib, re, shutil, struct, sys, zipfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
HERE = pathlib.Path(__file__).resolve().parent

# ----------------------------------------------------------------------------------------------------------------
# 1. class-file reader (constant pool only: class names, strings, member names)
# ----------------------------------------------------------------------------------------------------------------
OPLEN = {}
for _op in range(256): OPLEN[_op] = 1
for _op in (0x10, 0x12, 0x15, 0x16, 0x17, 0x18, 0x19, 0x36, 0x37, 0x38, 0x39, 0x3a, 0xa9, 0xbc): OPLEN[_op] = 2
for _op in (0x11, 0x13, 0x14, 0x84, 0xb2, 0xb3, 0xb4, 0xb5, 0xb6, 0xb7, 0xb8, 0xbb, 0xbd, 0xc0, 0xc1, 0xc6, 0xc7): OPLEN[_op] = 3
for _op in range(0x99, 0xa9): OPLEN[_op] = 3
OPLEN[0xc5] = 4; OPLEN[0xb9] = 5; OPLEN[0xba] = 5; OPLEN[0xc8] = 5; OPLEN[0xc9] = 5


class Method:
    def __init__(self, name, desc):
        self.name, self.desc, self.code, self.annotations = name, desc, b"", {}   # annotations: type -> {elem: value}


class ClassInfo:
    """Constant pool, methods (bytecode + annotations) and bootstrap methods of one class file."""
    def __init__(self, data):
        if data[:4] != b"\xca\xfe\xba\xbe": raise ValueError("not a class file")
        n, = struct.unpack_from(">H", data, 8)
        self.cp = [None] * n
        pos, i = 10, 1
        while i < n:
            tag = data[pos]
            if tag == 1:
                ln, = struct.unpack_from(">H", data, pos + 1); self.cp[i] = (1, data[pos + 3: pos + 3 + ln].decode("utf-8", "replace")); pos += 3 + ln
            elif tag in (3, 4): self.cp[i] = (tag, struct.unpack_from(">i" if tag == 3 else ">f", data, pos + 1)[0]); pos += 5
            elif tag in (5, 6): self.cp[i] = (tag, 0); pos += 9; i += 1
            elif tag in (7, 8, 16, 19, 20): self.cp[i] = (tag, struct.unpack_from(">H", data, pos + 1)[0]); pos += 3
            elif tag in (9, 10, 11, 12, 17, 18): self.cp[i] = (tag,) + struct.unpack_from(">HH", data, pos + 1); pos += 5
            elif tag == 15: self.cp[i] = (15, data[pos + 1], struct.unpack_from(">H", data, pos + 2)[0]); pos += 4
            else: raise ValueError("bad constant pool tag %d" % tag)
            i += 1
        self.utf8 = [e[1] for e in self.cp if e and e[0] == 1]
        self.classes = {self.u(e[1]) for e in self.cp if e and e[0] == 7}
        self.strings = [self.u(e[1]) for e in self.cp if e and e[0] == 8]
        this, = struct.unpack_from(">H", data, pos + 2)
        self.name = self.u(self.cp[this][1])
        pos += 6
        # interfaces
        ic, = struct.unpack_from(">H", data, pos); pos += 2 + 2 * ic
        self.methods, self.bsm = [], []
        for kind in ("fields", "methods"):
            cnt, = struct.unpack_from(">H", data, pos); pos += 2
            for _ in range(cnt):
                acc, ni, di, ac = struct.unpack_from(">HHHH", data, pos); pos += 8
                m = Method(self.u(ni), self.u(di)) if kind == "methods" else None
                for _ in range(ac):
                    an, al = struct.unpack_from(">HI", data, pos); pos += 6
                    body = data[pos: pos + al]; pos += al
                    if m is None: continue
                    aname = self.u(an)
                    if aname == "Code":
                        cl, = struct.unpack_from(">I", body, 4); m.code = body[8: 8 + cl]
                    elif aname == "RuntimeVisibleAnnotations":
                        m.annotations = self._annotations(body)
                if m: self.methods.append(m)
        ac, = struct.unpack_from(">H", data, pos); pos += 2
        for _ in range(ac):
            an, al = struct.unpack_from(">HI", data, pos); pos += 6
            if self.u(an) == "BootstrapMethods": self._bootstrap(data[pos: pos + al])
            pos += al

    def u(self, i): return self.cp[i][1] if i and self.cp[i] and self.cp[i][0] == 1 else ""

    def _bootstrap(self, b):
        n, = struct.unpack_from(">H", b, 0); p = 2
        for _ in range(n):
            ref, na = struct.unpack_from(">HH", b, p); p += 4
            args = list(struct.unpack_from(">%dH" % na, b, p)); p += 2 * na
            self.bsm.append((ref, args))

    def _annotations(self, b):
        out, p = {}, 2
        n, = struct.unpack_from(">H", b, 0)
        def value(p):
            t = chr(b[p]); p += 1
            if t in "BCDFIJSZs": return self.u(struct.unpack_from(">H", b, p)[0]) if t == "s" else None, p + 2
            if t == "e": return self.u(struct.unpack_from(">H", b, p + 2)[0]), p + 4
            if t == "c": return self.u(struct.unpack_from(">H", b, p)[0]), p + 2
            if t == "@":
                _, p = ann(p); return None, p
            n2, = struct.unpack_from(">H", b, p); p += 2; vals = []
            for _ in range(n2):
                v, p = value(p); vals.append(v)
            return vals, p
        def ann(p):
            ti, np_ = struct.unpack_from(">HH", b, p); p += 4; el = {}
            for _ in range(np_):
                ni, = struct.unpack_from(">H", b, p); v, p = value(p + 2); el[self.u(ni)] = v
            return (self.u(ti), el), p
        for _ in range(n):
            (t, el), p = ann(p); out[t] = el
        return out

    # ---- constant helpers
    def member(self, idx):
        """(owner, name, desc) of a Fieldref/Methodref/InterfaceMethodref"""
        e = self.cp[idx]; owner = self.u(self.cp[e[1]][1]); nat = self.cp[e[2]]
        return owner, self.u(nat[1]), self.u(nat[2])

    def indy(self, idx):
        """(impl_owner, impl_name, impl_desc, recipe, interface_desc) of an invokedynamic"""
        e = self.cp[idx]; ref, args = self.bsm[e[1]]; nat = self.cp[e[2]]
        impl, recipe = None, None
        for a in args:
            c = self.cp[a]
            if c and c[0] == 15: impl = self.member(c[2])
            elif c and c[0] == 8: recipe = self.u(c[1])
        return impl, recipe, self.u(nat[2]), self.u(nat[1])

    def instructions(self, m):
        code, pc = m.code, 0
        while pc < len(code):
            op = code[pc]
            if op == 0xaa:   # tableswitch
                p = (pc + 4) & ~3; lo, hi = struct.unpack_from(">ii", code, p + 4); ln = p + 12 + 4 * (hi - lo + 1) - pc
            elif op == 0xab:
                p = (pc + 4) & ~3; npairs, = struct.unpack_from(">i", code, p + 4); ln = p + 8 + 8 * npairs - pc
            elif op == 0xc4: ln = 6 if code[pc + 1] == 0x84 else 4
            else: ln = OPLEN[op]
            yield op, code[pc + 1: pc + ln], pc
            pc += ln


# ----------------------------------------------------------------------------------------------------------------
# 2. mapping tables: Java API  ->  Bedrock event
# ----------------------------------------------------------------------------------------------------------------
# (regex on a referenced class name or a constant such as a field name, bedrock event, what it means)
EVENT_RULES = [
    # Fabric API
    (r"fabric/api/event/lifecycle/v1/ServerTickEvents|fabric/api/event/lifecycle/v1/ServerWorldEvents", "level.tick", "server/world tick"),
    (r"fabric/api/client/event/lifecycle/v1/ClientTickEvents|fabric/api/event/client/ClientTickEvents", "tick", "client tick"),
    (r"fabric/api/networking/v1/ServerPlayConnectionEvents|fabric/api/client/networking/v1/ClientPlayConnectionEvents", "world.join", "connection JOIN"),
    (r"fabric/api/event/player/PlayerBlockBreakEvents|fabric/api/event/player/AttackBlockCallback", "block.break", "player breaks a block"),
    (r"fabric/api/event/player/UseBlockCallback", "block.place", "player uses/places on a block"),
    (r"fabric/api/event/player/UseItemCallback", "item.use", "player uses an item"),
    (r"fabric/api/event/player/UseEntityCallback", "mob.interact", "player interacts with an entity"),
    (r"fabric/api/event/player/AttackEntityCallback", "player.attack", "player attacks an entity"),
    (r"fabric/api/entity/event/v1/ServerLivingEntityEvents|fabric/api/entity/event/v1/ServerEntityCombatEvents", "mob.kill", "entity dies / is killed"),
    (r"fabric/api/message/v1/ServerMessageEvents|fabric/api/client/message/v1/ClientSendMessageEvents", "chat.send", "chat message sent"),
    (r"fabric/api/client/message/v1/ClientReceiveMessageEvents", "chat.display", "chat message received"),
    (r"fabric/api/client/rendering/v1/HudRenderCallback|fabric/api/client/rendering/v1/hud/", "hud", "HUD drawing"),
    (r"fabric/api/entity/event/v1/ServerPlayerEvents", "player.die", "player respawn/copy (death)"),
    # NeoForge / Forge
    (r"(neoforged|minecraftforge)/(event|common)/.*(TickEvent\$PlayerTickEvent|PlayerTickEvent)", "player.tick", "player tick"),
    (r"(neoforged|minecraftforge)/.*(LevelTickEvent|WorldTickEvent|ServerTickEvent)", "level.tick", "level/server tick"),
    (r"(neoforged|minecraftforge)/.*(ClientTickEvent|RenderTickEvent)", "tick", "client tick"),
    (r"(neoforged|minecraftforge)/.*PlayerLoggedInEvent|(neoforged|minecraftforge)/.*ClientPlayerNetworkEvent\$LoggingIn", "world.join", "player logged in"),
    (r"(neoforged|minecraftforge)/.*PlayerLoggedOutEvent|(neoforged|minecraftforge)/.*ClientPlayerNetworkEvent\$LoggingOut", "world.leave", "player logged out"),
    (r"(neoforged|minecraftforge)/.*BlockEvent\$BreakEvent", "block.break", "block broken"),
    (r"(neoforged|minecraftforge)/.*BlockEvent\$(EntityPlaceEvent|BlockToolModificationEvent)", "block.place", "block placed"),
    (r"(neoforged|minecraftforge)/.*(LivingDeathEvent|LivingDropsEvent)", "mob.kill", "living entity died"),
    (r"(neoforged|minecraftforge)/.*(AttackEntityEvent|LivingAttackEvent|LivingHurtEvent|LivingIncomingDamageEvent)", "player.attack", "entity attacked / hurt"),
    (r"(neoforged|minecraftforge)/.*(ServerChatEvent|ClientChatEvent)", "chat.send", "chat sent"),
    (r"(neoforged|minecraftforge)/.*ClientChatReceivedEvent", "chat.display", "chat received"),
    (r"(neoforged|minecraftforge)/.*ItemTossEvent", "item.drop", "item dropped"),
    (r"(neoforged|minecraftforge)/.*(ItemCraftedEvent|PlayerEvent\$ItemCraftedEvent)", "item.craft", "item crafted"),
    (r"(neoforged|minecraftforge)/.*(PlayerInteractEvent\$RightClickItem|LivingEntityUseItemEvent)", "item.use", "item used"),
    (r"(neoforged|minecraftforge)/.*PlayerInteractEvent\$EntityInteract", "mob.interact", "entity interact"),
    (r"(neoforged|minecraftforge)/.*(RenderGuiEvent|RenderGameOverlayEvent|CustomizeGuiOverlayEvent)", "hud", "HUD drawing"),
    (r"(neoforged|minecraftforge)/.*PlayerEvent\$PlayerRespawnEvent|(neoforged|minecraftforge)/.*PlayerRespawnEvent", "player.die", "player respawn"),
]
# Mixin injection points: (regex on target class, regex on method name, event, meaning)
MIXIN_RULES = [
    (r"net/minecraft/(server/level/ServerLevel|world/level/Level|server/world/ServerWorld|world/World)$", r"^tick$", "level.tick", "level tick"),
    (r"net/minecraft/(world/entity/player/Player|entity/player/PlayerEntity|server/level/ServerPlayer|server/network/ServerPlayerEntity|client/player/LocalPlayer|client/network/ClientPlayerEntity)$", r"^(tick|baseTick|aiStep|tickMovement)$", "player.tick", "player tick"),
    (r"net/minecraft/(world/entity/LivingEntity|entity/LivingEntity|world/entity/Mob|entity/mob/MobEntity)$", r"^(die|onDeath)$", "mob.kill", "entity death"),
    (r"net/minecraft/(world/entity/player/Player|entity/player/PlayerEntity)$", r"^(attack)$", "player.attack", "player attack"),
    (r"net/minecraft/(world/entity/LivingEntity|entity/LivingEntity)$", r"^(hurt|damage)$", "player.attack", "entity hurt"),
    (r"net/minecraft/(client/gui/Gui|client/gui/hud/InGameHud)$", r"^(render|renderHotbar|renderStatusBars)$", "hud", "HUD drawing"),
    (r"net/minecraft/(client/gui/components/ChatComponent|client/gui/hud/ChatHud)$", r"^(addMessage|addMessageToQueue)$", "chat.display", "chat shown"),
    (r"net/minecraft/(server/network/ServerGamePacketListenerImpl|server/network/ServerPlayNetworkHandler)$", r"^(handleChat|onChatMessage)$", "chat.send", "chat sent"),
    (r"net/minecraft/(client/multiplayer/MultiPlayerGameMode|client/network/ClientPlayerInteractionManager)$", r"^(destroyBlock|breakBlock)$", "block.break", "block break"),
    (r"net/minecraft/(world/item/BlockItem|item/BlockItem)$", r"^(place|useOnBlock)$", "block.place", "block place"),
    (r"net/minecraft/(client/Minecraft|client/MinecraftClient)$", r"^(tick|runTick|render)$", "game.update", "game tick/frame"),
    (r"net/minecraft/(client/multiplayer/ClientLevel|client/world/ClientWorld)$", r"^(tick)$", "level.tick", "client level tick"),
    # wider coverage: the Bedrock moment that is closest to the Java injection point
    (r"net/minecraft/(client/Camera|client/renderer/GameRenderer|client/renderer/LevelRenderer|client/render/Camera|client/render/GameRenderer|client/render/WorldRenderer)$",
     r"^(setup|update|render|renderLevel|tick|getFov|bobView|pick|setRotation|setPosition|renderItemInHand)$", "game.update", "per-frame camera/render update"),
    (r"net/minecraft/(server/level/ChunkMap|server/level/ServerChunkCache|server/world/ServerChunkManager)$", r"^(tick|tickChunk|tickChunks|tickSpawningChunk)$", "level.tick", "chunk/level tick"),
    (r"net/minecraft/(world/item/Item|world/item/ItemStack|item/Item|item/ItemStack)$", r"^(use|useOn|finishUsingItem|releaseUsing|useOnBlock|finishUsing)$", "item.use", "item used"),
    (r"net/minecraft/(world/entity/player/Player|entity/player/PlayerEntity)$", r"^(drop|dropItem)$", "item.drop", "item dropped"),
    (r"net/minecraft/(world/inventory/ResultSlot|screen/slot/CraftingResultSlot)$", r"^(onTake|checkTakeAchievements|onTakeItem|onCrafted)$", "item.craft", "item crafted"),
    (r"net/minecraft/(world/entity/Mob|world/entity/player/Player|world/entity/LivingEntity|entity/mob/MobEntity|entity/player/PlayerEntity)$", r"^(mobInteract|interact|interactOn|interactMob)$", "mob.interact", "entity interact"),
    (r"net/minecraft/(server/level/ServerPlayer|server/network/ServerPlayerEntity|world/entity/player/Player|entity/player/PlayerEntity)$", r"^(die|onDeath)$", "player.die", "player death"),
    (r"net/minecraft/(world/entity/Entity|entity/Entity)$", r"^(teleportTo|teleport|changeDimension|moveTo|requestTeleport)$", "player.teleport", "entity teleported"),
    (r"net/minecraft/(world/level/block/Block|block/Block)$", r"^(playerDestroy|destroy|playerWillDestroy|onBreak|afterBreak|onDestroyedByPlayer)$", "block.break", "block broken"),
    (r"net/minecraft/(world/level/block/Block|block/Block)$", r"^(setPlacedBy|onPlaced)$", "block.place", "block placed"),
]

# Unmapped mixins: (category, regex on class + target names, closest Bedrock mechanism)
MIXIN_TRIAGE = [
    ("Entity models / animation", r"Model|EntityRenderer|AvatarRenderer|Skin|Pose|Humanoid|Animation|Armor.*Layer|RenderState|Layer",
     "Bedrock client entity JSON in the resource pack (geometry, textures, animations, render controllers). Generated for entities the mod registers (see Entities); vanilla models need a resource-pack override, which has no runtime hook."),
    ("Camera / rendering / atmosphere", r"Camera|Fov|Render|Shader|Sky|Cloud|Sunrise|Fog|Light|Glow|Particle|Vignette|Atmosphere|Mirror|Bob",
     "Resource-pack data (fog, sky, particles, materials) and the options Bedrock.options() can write to options.txt (field of view, view distance, view bobbing). No per-frame renderer hook exists."),
    ("Screens / GUI", r"Screen|Gui|Menu|Hud|Tooltip|Emi|Jei|Rei|Widget|Slot|Container|Overlay|Keybind|KeyMapping|Option",
     "Text through Bedrock.hud() lines and toasts (already used for rewritten handlers). Bedrock form / UI JSON screens are separate resource-pack work."),
    ("Entity AI / movement / vehicles", r"Goal|Brain|Mob|Animal|Villager|Boat|Minecart|Vehicle|Riding|Passenger|Movement|Physics|Navigation|Path|LivingEntity|Entity",
     "Behaviour-pack entity components and behaviours (data-driven). Generated for entities the mod registers; vanilla entity changes would need a behaviour-pack override and are not generated."),
    ("Blocks / block entities / items / recipes", r"Block|Furnace|Campfire|Fire|Fluid|Item|Inventory|Recipe|Crafting|Loot|Enchant|Hopper|Chest|Smelt",
     "Behaviour-pack block/item JSON, recipes and loot tables. Identity, name, texture and a plain definition are generated; per-tick behaviour needs Bedrock block components or custom components."),
    ("World / chunks / networking / other mods", r"Chunk|Level|World|Biome|Feature|Structure|Dimension|Packet|Network|Connection|Server|Sable|Ftb|Team|Command|Data|Persistent|Saved",
     "Feature rules / dimension JSON for generation. Server networking and other mods' APIs (Sable, FTB, EMI ...) have no Bedrock counterpart."),
]

def triage_mixin(cname, targets):
    key = cname.split("/")[-1] + " " + " ".join(targets)
    for cat, rx, eq in MIXIN_TRIAGE:
        if re.search(rx, key): return cat, eq
    return "Other", "Not classifiable from its name; read the handler in the jar if it matters."

# Known mods: id regex -> port template. These mods do work Bedrock can partly do through its options and HUD.
KNOWN = [
    (r"^(lithium|sodium|embeddium|rubidium|starlight|ferritecore|modernfix|krypton|immediatelyfast|c2me.*|vmp.*|entityculling|moreculling|betterfps|sodium-extra|sodiumextra|reeses-sodium-options|iris|oculus)$", "perf",
     "Performance mod. Bedrock's engine is native, so the port tunes the game's own options from the measured frame rate (same goal: keep fps up)."),
    (r".*fps.*", "fps", "FPS counter. Ported to the Bedrock HUD using the real measured frame rate."),
    (r"^(modmenu|mod-menu)$", "skip", "Mod Menu is replaced by the built-in Mods panel."),
]

TICKY = {"tick", "level.tick", "player.tick", "game.update", "hud"}
MSG_PRIORITY = ["world.join", "world.leave", "player.die", "mob.kill", "chat.send", "chat.display", "block.break", "block.place", "item.use", "item.craft", "item.drop", "mob.interact"]
PROPER = {"hud": None}  # features that map to the HUD, not to a native event
DORMANT = {"player.attack": "wired, but the native attack hook was not located in the supplied libminecraftpe.so (docs/HOOKS.md), so it does not fire yet. It starts working as soon as signatures/player.attack.txt exists; mob.kill / player.die fire today."}
SKIP_STRING = re.compile(r"^(net|com|org|java|javax|io|dev)[./]|\.class$|^[a-z_]+:[a-z_/]+$|^\W+$|%[sd]|^L[\w/$]+;$")

# ----------------------------------------------------------------------------------------------------------------
# 2b. handler translator: bytecode of an event handler -> Bedrock actions
# ----------------------------------------------------------------------------------------------------------------
MSG_NAMES = {"sendSystemMessage", "displayClientMessage", "sendMessage", "sendFeedback", "sendOverlayMessage", "addChatMessage",
             "sendMessageToClient", "sendSuccess", "sendFailure", "m_213846_", "m_5661_"}
TEXT_PASS = {"literal", "translatable", "of", "text", "nullToEmpty", "valueOf", "toString", "empty"}
LOG_NAMES = {"info", "warn", "error", "debug"}
DRAW_NAMES = {"drawString", "drawCenteredString", "drawText", "drawTextWithShadow", "renderText", "drawShadowedText", "draw", "drawWithShadow"}
FPS_NAMES = re.compile(r"^(getFps|getCurrentFps|fps|currentFps|getFramerate|getFrameRate)$", re.I)
# stack effects: opcode -> (pops, pushes) for the opcodes whose values we do not track
SIMPLE = {0x00: (0, 0), 0x01: (0, 1), 0x57: (1, 0), 0x58: (2, 0), 0x59: None, 0xb1: (0, 0), 0xc0: (1, 1), 0xc1: (1, 1), 0xbe: (1, 1),
          0x2a: (0, 1), 0x2b: (0, 1), 0x2c: (0, 1), 0x2d: (0, 1), 0x1a: (0, 1), 0x1b: (0, 1), 0x1c: (0, 1), 0x1d: (0, 1)}


def _java_name(owner):
    return owner.replace("/", ".")


def _arg_count(desc):
    d = desc[1:desc.index(")")]; n = 0; i = 0
    while i < len(d):
        while d[i] == "[": i += 1
        if d[i] == "L": i = d.index(";", i)
        i += 1; n += 1
    return n


def _ret_void(desc): return desc.endswith(")V")


class Handler:
    """What one handler method does, in Bedrock terms."""
    def __init__(self, cname, mname):
        self.cname, self.mname = cname, mname
        self.actions = []        # ("toast", tmpl) | ("log", tmpl) | ("hud", tmpl)
        self.untranslated = {}   # "Class.method" -> count


def translate(ci, m, classes_by_name):
    """Walk the method's bytecode with a small symbolic stack. Constants and string concatenation are tracked, so
    `sendSystemMessage(Component.literal("Hi " + name))` and `drawString(font, "FPS: " + getFps(), x, y)` come out as actions."""
    h = Handler(ci.name, m.name)
    st = []
    def pop(n=1):
        out = []
        for _ in range(n): out.append(st.pop() if st else ("?",))
        return list(reversed(out))
    def text_of(v):
        """value -> list of template parts: str literal | ("fps",) | ("?",)"""
        if v[0] == "str": return [v[1]]
        if v[0] == "tmpl": return v[1]
        if v[0] == "fps": return [("fps",)]
        return None
    for op, arg, pc in ci.instructions(m):
        if op == 0x12 or op == 0x13 or op == 0x14:
            idx = arg[0] if op == 0x12 else struct.unpack(">H", arg[:2])[0]
            e = ci.cp[idx]
            st.append(("str", ci.u(e[1])) if e and e[0] == 8 else ("const",))
        elif op in (0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08): st.append(("int", op - 3))
        elif op == 0x10: st.append(("int", struct.unpack("b", arg)[0]))
        elif op == 0x11: st.append(("int", struct.unpack(">h", arg)[0]))
        elif op == 0xbb:
            cls = ci.u(ci.cp[struct.unpack(">H", arg)[0]][1]); st.append(("sb", []) if cls == "java/lang/StringBuilder" else ("new", cls))
        elif op == 0x59: st.append(st[-1] if st else ("?",))
        elif op in (0xb2,):   # getstatic
            owner, name, desc = ci.member(struct.unpack(">H", arg)[0]); st.append(("fps",) if FPS_NAMES.match(name) else ("static", owner, name))
        elif op == 0xb4:      # getfield
            pop(1); owner, name, desc = ci.member(struct.unpack(">H", arg)[0]); st.append(("fps",) if FPS_NAMES.match(name) else ("?",))
        elif op in (0xb6, 0xb7, 0xb8, 0xb9):
            owner, name, desc = ci.member(struct.unpack(">H", arg[:2])[0])
            n = _arg_count(desc); args = pop(n); recv = pop(1)[0] if op != 0xb8 else None
            ret = ("?",)
            if owner == "java/lang/StringBuilder" and recv and recv[0] == "sb":
                if name == "append":
                    part = text_of(args[0]) if args else None
                    ret = ("sb", recv[1] + (part if part is not None else [("?",)]))
                elif name == "toString": ret = ("tmpl", recv[1])
                else: ret = recv
            elif name == "<init>":
                if recv and recv[0] == "sb": pass
            elif name in TEXT_PASS and args and text_of(args[0]) is not None: ret = ("tmpl", text_of(args[0]))
            elif FPS_NAMES.match(name): ret = ("fps",)
            elif name in MSG_NAMES:
                t = next((text_of(a) for a in args if text_of(a) is not None), None)
                if t: h.actions.append(("toast", t))
                else: h.untranslated[_java_name(owner).split(".")[-1] + "." + name + " (dynamic text)"] = 1
            elif (name in LOG_NAMES and "Logger" in owner or owner == "java/io/PrintStream" and name in ("println", "print")) and args and text_of(args[0]) is not None:
                h.actions.append(("log", text_of(args[0])))
            elif name in ("getInstance", "instance", "getMinecraft", "get", "getCurrent"):
                pass
            elif name in DRAW_NAMES:
                t = next((text_of(a) for a in args if text_of(a) is not None), None)
                if t: h.actions.append(("hud", t))
                else: h.untranslated[_java_name(owner).split(".")[-1] + "." + name + " (dynamic text)"] = 1
            elif owner.startswith("net/minecraft/") or owner.startswith("com/mojang/"):
                key = owner.split("/")[-1] + "." + name
                h.untranslated[key] = h.untranslated.get(key, 0) + 1
            if not _ret_void(desc): st.append(ret)
        elif op == 0xba:      # invokedynamic
            impl, recipe, idesc, iname = ci.indy(struct.unpack(">H", arg[:2])[0])
            n = _arg_count(idesc); args = pop(n)
            if recipe is not None and iname == "makeConcatWithConstants":
                parts, it = [], iter(args); buf = ""
                for ch in recipe:
                    if ch == "\u0001":
                        if buf: parts.append(buf); buf = ""
                        v = next(it, ("?",)); t = text_of(v); parts += t if t is not None else [("?",)]
                    elif ch == "\u0002": buf += ""
                    else: buf += ch
                if buf: parts.append(buf)
                st.append(("tmpl", parts))
            else: st.append(("?",))
        elif op in (0x57,): pop(1)
        elif op in (0x58,): pop(2)
        elif op in (0xc0, 0xc1, 0xbe): pass
        elif 0x15 <= op <= 0x19 or 0x1a <= op <= 0x2d: st.append(("?",))
        elif 0x36 <= op <= 0x3a or 0x3b <= op <= 0x4e: pop(1)
        elif op == 0xb1 or op == 0xac or op == 0xb0: st.clear()
        else: st.clear()
    # drop duplicate actions
    seen, uniq = set(), []
    for a in h.actions:
        k = (a[0], repr(a[1]))
        if k not in seen: seen.add(k); uniq.append(a)
    h.actions = uniq
    return h


def event_for_param_types(desc):
    """Bedrock event for the first parameter class of a handler method (Forge/NeoForge/Fabric callback lambdas)."""
    for cls in re.findall(r"L([^;]+);", desc[1:desc.index(")")]):
        for rx, ev, why in EVENT_RULES:
            if re.search(rx, cls): return ev, why, cls
    return None


def bind_handlers(classes):
    """event -> [Handler]. Finds handlers three ways: Fabric `EVENT.register(lambda)`, Forge/NeoForge listener methods
    (`@SubscribeEvent` or `addListener(this::on)`), and Mixin `@Inject` methods."""
    bound = {}
    def add(ev, why, ci, m):
        h = translate(ci, m, classes); h.why = why
        if h.actions or h.untranslated: bound.setdefault(ev, []).append(h)
    by_member = {(c.name, m.name, m.desc): (c, m) for c in classes.values() for m in c.methods}
    for ci in classes.values():
        for m in ci.methods:
            cur = None
            for op, arg, pc in ci.instructions(m):
                if op == 0xb2:
                    owner, name, desc = ci.member(struct.unpack(">H", arg)[0]); cur = None
                    for rx, ev, why in EVENT_RULES:
                        if re.search(rx, owner):
                            cur = (ev, why, name)
                            if ev == "world.join" and re.search(r"DISCONNECT", name): cur = ("world.leave", "connection DISCONNECT", name)
                            if ev == "mob.kill" and re.search(r"DEATH|KILL", name) is None: cur = None
                            break
                elif op == 0xba:
                    impl = ci.indy(struct.unpack(">H", arg[:2])[0])[0]
                    if not impl: continue
                    target = by_member.get(impl)
                    if not target: continue
                    pe = event_for_param_types(impl[2])
                    if pe: add(pe[0], pe[1] + " (" + pe[2].split("/")[-1] + ")", *target)
                    elif cur: add(cur[0], cur[1], *target)
            # annotated listener methods
            if "Lnet/neoforged/bus/api/SubscribeEvent;" in m.annotations or "Lnet/minecraftforge/eventbus/api/SubscribeEvent;" in m.annotations:
                pe = event_for_param_types(m.desc)
                if pe: add(pe[0], pe[1] + " (" + pe[2].split("/")[-1] + ")", ci, m)
    return bound


def bind_mixins(classes, mixin_classes):
    bound = {}
    for cname in mixin_classes:
        ci = classes.get(cname)
        if not ci: continue
        targets, _ = mixin_methods(ci)
        for m in ci.methods:
            inj = next((a for t, a in m.annotations.items() if re.search(r"/(Inject|Redirect|ModifyArg|ModifyVariable|WrapOperation|ModifyExpressionValue)", t)), None)
            if inj is None: continue
            hints = inj.get("method"); hints = hints if isinstance(hints, list) else [hints]
            hints = [re.sub(r"\(.*$", "", x or "") for x in hints]
            for t in targets:
                for trx, mrx, ev, why in MIXIN_RULES:
                    if re.search(trx, t) and any(re.search(mrx, x) for x in hints):
                        h = translate(ci, m, classes); h.why = "Mixin into " + t.split("/")[-1] + "." + hints[0]
                        bound.setdefault(ev, []).append(h)
    return bound


# ----------------------------------------------------------------------------------------------------------------
# 2c. content: blocks and items -> Bedrock add-on (behavior pack + resource pack JSON)
# ----------------------------------------------------------------------------------------------------------------
REG_FIELD = re.compile(r"^(ITEM|ITEMS|BLOCK|BLOCKS|ENTITY_TYPE|ENTITY_TYPES)$")
REG_CALL = {"register", "registerItem", "registerBlock", "registerSimpleItem", "registerSimpleBlock", "registerBlockItem", "create"}
ID_RX = re.compile(r"^[a-z][a-z0-9_]{1,40}$")


def find_content(classes, meta_id):
    """[(kind, id)] for blocks / items / entities registered with Registry.register or DeferredRegister.register."""
    found = {}
    sized_by_class, ent_by_class = {}, {}
    find_content.sizes = {}
    for ci in classes.values():
        for m in ci.methods:
            reg, st = None, []
            for op, arg, pc in ci.instructions(m):
                if op == 0xb2:
                    owner, name, desc = ci.member(struct.unpack(">H", arg)[0])
                    if REG_FIELD.match(name): reg = name.rstrip("S").lower().replace("_type", "")
                    st.append(("fld", name))
                elif op in (0x0b, 0x0c, 0x0d):
                    st.append(("f", float(op - 0x0b)))
                elif op in (0x12, 0x13):
                    e = ci.cp[arg[0] if op == 0x12 else struct.unpack(">H", arg[:2])[0]]
                    st.append(("str", ci.u(e[1])) if e and e[0] == 8 else (("f", float(e[1])) if e and e[0] == 4 else ("c",)))
                elif op in (0xb6, 0xb7, 0xb8, 0xb9):
                    owner, name, desc = ci.member(struct.unpack(">H", arg[:2])[0])
                    n = _arg_count(desc); args = st[-n:] if n else []; st = st[:-n] if n else st
                    if name == "sized" and n == 2 and all(a[0] == "f" for a in args):
                        sized_by_class.setdefault(ci.name, []).append((args[0][1], args[1][1]))
                    if name in REG_CALL and reg:
                        for a in args:
                            if a[0] == "str" and ID_RX.match(a[1]):
                                found[(reg, a[1])] = 1
                                if reg == "entity": ent_by_class.setdefault(ci.name, []).append(a[1])
                    if not _ret_void(desc): st.append(("r",))
                else:
                    if len(st) > 8: st = st[-8:]
    for cn, ids in ent_by_class.items():           # registrations and .sized(w, h) calls appear in the same order
        sz = sized_by_class.get(cn, [])
        if sz and len(sz) == len(ids):
            for i, wh in zip(ids, sz): find_content.sizes[i] = wh
    return sorted((k, i) for (k, i) in found)



def _make_tex_resolver(z, names, ns):
    """Resolve a block/item texture through blockstates -> models -> parent chain, then by name patterns."""
    nameset = set(names)
    cache = {}
    def jload(path):
        if path not in cache:
            try: cache[path] = json.loads(z.read(path).decode("utf-8", "replace")) if path in nameset else None
            except Exception: cache[path] = None
        return cache[path]
    def model_path(ref, default_ns):
        m, _, p = ref.rpartition(":")
        return f"assets/{m or default_ns}/models/{p}.json"
    def tex_path(ref, default_ns):
        m, _, p = ref.rpartition(":")
        return f"assets/{m or default_ns}/textures/{p}.png"
    PREF = ("all", "layer0", "particle", "end", "top", "side", "front", "north", "texture", "cross", "0")
    def from_model(ref, default_ns, depth=0):
        if depth > 6 or not ref: return None
        mp = model_path(ref, default_ns); md = jload(mp)
        if not md: return None
        tx = dict(md.get("textures") or {})
        par = md.get("parent")
        if par and not par.startswith("minecraft:") and not par.startswith("builtin"):
            # merge parent textures (child wins)
            pt = _collect(par, default_ns, depth + 1)
            for k, v in pt.items(): tx.setdefault(k, v)
        def deref(v, hop=0):
            while isinstance(v, str) and v.startswith("#") and hop < 8: v = tx.get(v[1:]); hop += 1
            return v
        for k in PREF:
            v = deref(tx.get(k))
            if isinstance(v, str) and tex_path(v, default_ns) in nameset: return tex_path(v, default_ns)
        for v in tx.values():
            v = deref(v)
            if isinstance(v, str) and tex_path(v, default_ns) in nameset: return tex_path(v, default_ns)
        return None
    def _collect(ref, default_ns, depth):
        md = jload(model_path(ref, default_ns)) or {}
        tx = dict(md.get("textures") or {})
        par = md.get("parent")
        if par and depth < 6 and not par.startswith("minecraft:") and not par.startswith("builtin"):
            for k, v in _collect(par, default_ns, depth + 1).items(): tx.setdefault(k, v)
        return tx
    def by_name(kind, i):
        cands = [f"assets/{ns}/textures/{kind}/{i}.png"] + [f"assets/{ns}/textures/{kind}/{i}{suf}.png" for suf in ("_top", "_side", "_front", "_0", "_off", "_on", "_active", "_base")]
        for c in cands:
            if c in nameset: return c
        rx = re.compile(r"assets/[^/]+/textures/%s/%s(_[^/]*)?\.png$" % (kind, re.escape(i)))
        return next((n for n in sorted(names) if rx.match(n)), None)
    def block(b):
        for nsx in {ns} | {n.split("/")[1] for n in names if n.startswith("assets/") and n.count("/") > 1}:
            bs = jload(f"assets/{nsx}/blockstates/{b}.json")
            if not bs: continue
            refs = []
            if "variants" in bs:
                v = bs["variants"]; v = list(v.values())[0] if isinstance(v, dict) and v else None
                v = v[0] if isinstance(v, list) and v else v
                if isinstance(v, dict) and v.get("model"): refs.append(v["model"])
            for part in bs.get("multipart", []) or []:
                a = part.get("apply"); a = a[0] if isinstance(a, list) and a else a
                if isinstance(a, dict) and a.get("model"): refs.append(a["model"])
            for r in refs:
                t = from_model(r, nsx)
                if t: return t
        t = from_model(f"{ns}:block/{b}", ns)
        return t or by_name("block", b)
    def item(i):
        t = from_model(f"{ns}:item/{i}", ns)
        return t or by_name("item", i) or block(i)
    def evidence(kind, i, lang):
        k = "block" if kind == "block" else "item"
        return (f"{k}.{ns}.{i}" in lang or f"assets/{ns}/blockstates/{i}.json" in nameset or f"assets/{ns}/models/{k}/{i}.json" in nameset
                or f"assets/{ns}/textures/{k}/{i}.png" in nameset or f"assets/{ns}/items/{i}.json" in nameset)
    return block, item, evidence

def build_addon(z, meta, content, out):
    """Write <out>/addon/<id>_BP and <id>_RP for the blocks and items found. Returns (items, blocks, skipped, notes)."""
    import uuid
    ns = meta["id"]
    names = z.namelist()
    lang = {}
    for n in names:
        if re.match(r"assets/[^/]+/lang/en_us\.json$", n):
            try: lang.update(json.loads(z.read(n).decode("utf-8", "replace")))
            except Exception: pass
    items = [i for k, i in content if k == "item"]; blocks = [i for k, i in content if k == "block"]
    entities = [i for k, i in content if k == "entity"]
    if not (items or blocks): return [], [], entities, []
    tex_block, tex_item, has_evidence = _make_tex_resolver(z, names, ns)
    has_assets = any(re.match(r"assets/[^/]+/(blockstates|models|lang)/", n) for n in names)
    dropped = []
    if has_assets:   # strings such as "east", "north" or the mod id are property/enum names, not registry entries
        keep_b = [b for b in blocks if b != ns and has_evidence("block", b, lang)]
        keep_i = [i for i in items if i != ns and (has_evidence("item", i, lang) or i in keep_b)]
        dropped = sorted((set(blocks) - set(keep_b)) | (set(items) - set(keep_i)))
        blocks, items = keep_b, keep_i
    if not (items or blocks): return [], [], entities, []
    bp, rp = out / "addon" / (ns + "_BP"), out / "addon" / (ns + "_RP")
    for d in ("items", "blocks"): (bp / d).mkdir(parents=True, exist_ok=True)
    (rp / "textures/items").mkdir(parents=True, exist_ok=True); (rp / "textures/blocks").mkdir(parents=True, exist_ok=True); (rp / "texts").mkdir(parents=True, exist_ok=True)
    def manifest(kind, mod_type):
        return {"format_version": 2, "header": {"name": meta["name"] + (" Behavior" if kind == "bp" else " Resources"), "description": "Converted by ModHost mods-in/convert.py",
                "uuid": str(uuid.uuid5(uuid.NAMESPACE_DNS, ns + kind + "h")), "version": [1, 0, 0], "min_engine_version": [1, 21, 0]},
                "modules": [{"type": mod_type, "uuid": str(uuid.uuid5(uuid.NAMESPACE_DNS, ns + kind + "m")), "version": [1, 0, 0]}],
                "dependencies": ([{"uuid": str(uuid.uuid5(uuid.NAMESPACE_DNS, ns + "rph")), "version": [1, 0, 0]}] if kind == "bp" else [])}
    rpm = manifest("rp", "resources"); rpm["header"]["uuid"] = str(uuid.uuid5(uuid.NAMESPACE_DNS, ns + "rph"))
    (bp / "manifest.json").write_text(json.dumps(manifest("bp", "data"), indent=2))
    (rp / "manifest.json").write_text(json.dumps(rpm, indent=2))
    lines, item_tex, terrain, notes = [], {}, {}, []
    def title(i): return i.replace("_", " ").title()
    for i in items:
        tex = tex_item(i)
        comp = {"minecraft:max_stack_size": 64}
        if tex:
            (rp / "textures/items" / (i + ".png")).write_bytes(z.read(tex)); item_tex[f"{ns}_{i}"] = {"textures": f"textures/items/{i}"}
            comp["minecraft:icon"] = {"textures": {"default": f"{ns}_{i}"}}
        (bp / "items" / (i + ".json")).write_text(json.dumps({"format_version": "1.21.0", "minecraft:item": {
            "description": {"identifier": f"{ns}:{i}", "menu_category": {"category": "items"}}, "components": comp}}, indent=2))
        lines.append(f"item.{ns}:{i}={lang.get(f'item.{ns}.{i}', title(i))}")
    for b in blocks:
        tex = tex_block(b)
        inst = {"*": {"texture": f"{ns}_{b}", "render_method": "opaque"}}
        if tex:
            (rp / "textures/blocks" / (b + ".png")).write_bytes(z.read(tex)); terrain[f"{ns}_{b}"] = {"textures": f"textures/blocks/{b}"}
        else: notes.append(f"block `{b}`: no texture found in the jar; add textures/blocks/{b}.png")
        (bp / "blocks" / (b + ".json")).write_text(json.dumps({"format_version": "1.21.0", "minecraft:block": {
            "description": {"identifier": f"{ns}:{b}", "menu_category": {"category": "construction"}},
            "components": {"minecraft:material_instances": inst, "minecraft:geometry": "minecraft:geometry.full_block"}}}, indent=2))
        lines.append(f"tile.{ns}:{b}.name={lang.get(f'block.{ns}.{b}', title(b))}")
    if dropped: notes.append(f"{len(dropped)} name(s) skipped because they have no model, blockstate, texture or language entry in the jar (property/enum strings, not registry entries): " + ", ".join(f"`{d}`" for d in dropped[:12]) + (" ..." if len(dropped) > 12 else ""))
    if item_tex: (rp / "textures/item_texture.json").write_text(json.dumps({"resource_pack_name": ns, "texture_name": "atlas.items", "texture_data": item_tex}, indent=2))
    if terrain: (rp / "textures/terrain_texture.json").write_text(json.dumps({"resource_pack_name": ns, "texture_name": "atlas.terrain", "padding": 8, "num_mip_levels": 4, "texture_data": terrain}, indent=2))
    (rp / "texts/en_US.lang").write_text("\n".join(lines) + "\n")
    return items, blocks, entities, notes


ENTITY_ARCH = [
    ("seat", r"seat|chair|cockpit|saddle|pilot"),
    ("projectile", r"grenade|bolt|shot|projectile|missile|bullet|arrow|laser|plasma|bomb|dart|fireball|beam|orb"),
    ("vehicle", r"rocket|ship|shuttle|craft|lander|vehicle|car|cart|boat|rover|hover|bike|train|mech|plane|jet|drone|cargo|capsule|pod"),
]
FLYERS = r"rocket|ship|shuttle|lander|plane|jet|drone|hover|capsule|pod|craft"


def _png(w, h, rgb):
    import zlib
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))
    def ch(t, d): return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    return b"\x89PNG\r\n\x1a\n" + ch(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + ch(b"IDAT", zlib.compress(raw)) + ch(b"IEND", b"")


def entity_components(arch, i, w, h):
    base = {"minecraft:type_family": {"family": [i]}, "minecraft:collision_box": {"width": w, "height": h}, "minecraft:physics": {}, "minecraft:nameable": {}}
    if arch == "seat":
        base.update({"minecraft:health": {"value": 1, "max": 1}, "minecraft:damage_sensor": {"triggers": {"deals_damage": "no"}},
                     "minecraft:knockback_resistance": {"value": 1.0}, "minecraft:pushable": {"is_pushable": False, "is_pushable_by_piston": False},
                     "minecraft:rideable": {"seat_count": 1, "family_types": ["player"], "interact_text": "action.interact.ride.horse",
                                            "seats": {"position": [0, round(h * 0.5, 2), 0]}}})
    elif arch == "projectile":
        base.update({"minecraft:health": {"value": 1, "max": 1}, "minecraft:pushable": {"is_pushable": False, "is_pushable_by_piston": True},
                     "minecraft:projectile": {"on_hit": {"impact_damage": {"damage": 4, "knockback": True, "semi_random_diff_damage": False}, "remove_on_hit": {}},
                                              "power": 1.6, "gravity": 0.03, "inertia": 0.99, "liquid_inertia": 0.6, "uncertainty_base": 1,
                                              "uncertainty_multiplier": 0, "anchor": 1, "offset": [0, -0.1, 0]}})
    elif arch == "vehicle":
        base.update({"minecraft:health": {"value": 40, "max": 40}, "minecraft:movement": {"value": 0.3}, "minecraft:movement.basic": {}, "minecraft:jump.static": {},
                     "minecraft:rideable": {"seat_count": 1, "family_types": ["player"], "interact_text": "action.interact.ride.horse",
                                            "seats": {"position": [0, round(h * 0.5, 2), 0]}}})
        if re.search(FLYERS, i):
            base.update({"minecraft:can_fly": {}, "minecraft:flying_speed": {"value": 0.5}, "minecraft:movement.fly": {}, "minecraft:input_air_controlled": {}})
        else:
            base.update({"minecraft:input_ground_controlled": {}})
    else:
        base.update({"minecraft:health": {"value": 20, "max": 20}, "minecraft:movement": {"value": 0.25}, "minecraft:movement.basic": {}, "minecraft:jump.static": {},
                     "minecraft:navigation.walk": {"can_path_over_water": True, "avoid_water": True}, "minecraft:behavior.float": {"priority": 0},
                     "minecraft:behavior.random_stroll": {"priority": 6}, "minecraft:behavior.look_at_player": {"priority": 7, "look_distance": 6},
                     "minecraft:behavior.random_look_around": {"priority": 9}})
    return base


def build_entities(z, meta, entities, out):
    """Bedrock entity (behaviour + client definition + geometry + texture) for each registered entity. Returns (generated, notes)."""
    import uuid, hashlib
    ns = meta["id"]; names = z.namelist(); nameset = set(names)
    if not entities: return [], []
    lang = {}
    for n in names:
        if re.match(r"assets/[^/]+/lang/en_us\.json$", n):
            try: lang.update(json.loads(z.read(n).decode("utf-8", "replace")))
            except Exception: pass
    sizes = getattr(find_content, "sizes", {})
    def tex_for(i):
        rx = re.compile(r"assets/[^/]+/textures/entity/(?:[^/]+/)*%s(?:_[^/]*)?\.png$" % re.escape(i))
        return next((n for n in sorted(names) if rx.match(n)), None)
    def geo_for(i):
        rx = re.compile(r"assets/[^/]+/(?:geo|models/entity|geo/entity)/(?:[^/]+/)*%s(?:\.geo)?\.json$" % re.escape(i))
        for n in sorted(names):
            if rx.match(n):
                try:
                    j = json.loads(z.read(n).decode("utf-8", "replace"))
                    if "minecraft:geometry" in j or any(k.startswith("geometry.") for k in j): return j
                except Exception: pass
        return None
    gen, notes, skipped = [], [], []
    bp, rp = out / "addon" / (ns + "_BP"), out / "addon" / (ns + "_RP")
    def manifest(kind, mod_type):
        return {"format_version": 2, "header": {"name": meta["name"] + (" Behavior" if kind == "bp" else " Resources"), "description": "Converted by ModHost mods-in/convert.py",
                "uuid": str(uuid.uuid5(uuid.NAMESPACE_DNS, ns + kind + ("rph" if kind == "rp" else "h"))), "version": [1, 0, 0], "min_engine_version": [1, 21, 0]},
                "modules": [{"type": mod_type, "uuid": str(uuid.uuid5(uuid.NAMESPACE_DNS, ns + kind + "m")), "version": [1, 0, 0]}],
                "dependencies": ([{"uuid": str(uuid.uuid5(uuid.NAMESPACE_DNS, ns + "rph")), "version": [1, 0, 0]}] if kind == "bp" else [])}
    for d in (bp / "entities", rp / "entity", rp / "models/entity", rp / "textures/entity", rp / "texts"): d.mkdir(parents=True, exist_ok=True)
    for pk, kind, mt in ((bp, "bp", "data"), (rp, "rp", "resources")):
        if not (pk / "manifest.json").exists(): (pk / "manifest.json").write_text(json.dumps(manifest(kind, mt), indent=2))
    lines = []
    for i in entities:
        arch = next((a for a, rx in ENTITY_ARCH if re.search(rx, i)), "mob")
        geo, tex = geo_for(i), tex_for(i)
        evidence = geo or tex or f"entity.{ns}.{i}" in lang or arch != "mob"
        if not evidence or (i == ns and not (geo or tex)): skipped.append(i); continue
        dw, dh = {"seat": (0.8, 0.8), "projectile": (0.4, 0.4), "vehicle": (1.8, 2.0), "mob": (0.6, 1.8)}[arch]
        w, h = sizes.get(i, (dw, dh)); w = min(max(w, 0.1), 16); h = min(max(h, 0.1), 16)
        gid = f"geometry.{ns}_{i}"
        if geo:
            if "minecraft:geometry" in geo:
                geo["minecraft:geometry"][0].setdefault("description", {})["identifier"] = gid
            else:
                k = next(k for k in geo if k.startswith("geometry.")); geo[gid] = geo.pop(k)
            gkind = "model from the jar"
        else:
            geo = {"format_version": "1.12.0", "minecraft:geometry": [{"description": {"identifier": gid, "texture_width": 64, "texture_height": 64,
                   "visible_bounds_width": max(2, round(w * 2)), "visible_bounds_height": max(2, round(h * 2)), "visible_bounds_offset": [0, round(h / 2, 2), 0]},
                   "bones": [{"name": "body", "pivot": [0, 0, 0], "cubes": [{"origin": [round(-w * 8, 2), 0, round(-w * 8, 2)], "size": [round(w * 16, 2), round(h * 16, 2), round(w * 16, 2)], "uv": [0, 0]}]}]}]}
            gkind = "placeholder box"
        (rp / "models/entity" / (i + ".geo.json")).write_text(json.dumps(geo, indent=2))
        if tex: (rp / "textures/entity" / (i + ".png")).write_bytes(z.read(tex)); tkind = "texture from the jar"
        else:
            hx = hashlib.md5(i.encode()).digest(); (rp / "textures/entity" / (i + ".png")).write_bytes(_png(64, 64, (80 + hx[0] // 2, 80 + hx[1] // 2, 80 + hx[2] // 2))); tkind = "placeholder colour"
        (bp / "entities" / (i + ".json")).write_text(json.dumps({"format_version": "1.21.0", "minecraft:entity": {
            "description": {"identifier": f"{ns}:{i}", "is_spawnable": True, "is_summonable": True, "is_experimental": False},
            "components": entity_components(arch, i, round(w, 2), round(h, 2))}}, indent=2))
        hx = hashlib.md5((i + "egg").encode()).digest()
        (rp / "entity" / (i + ".entity.json")).write_text(json.dumps({"format_version": "1.10.0", "minecraft:client_entity": {"description": {
            "identifier": f"{ns}:{i}", "materials": {"default": "entity_alphatest"}, "textures": {"default": f"textures/entity/{i}"}, "geometry": {"default": gid},
            "render_controllers": ["controller.render.default"], "spawn_egg": {"base_color": "#%02x%02x%02x" % tuple(hx[:3]), "overlay_color": "#%02x%02x%02x" % tuple(hx[3:6])}}}}, indent=2))
        nm = lang.get(f"entity.{ns}.{i}", i.replace("_", " ").title())
        lines += [f"entity.{ns}:{i}.name={nm}", f"item.spawn_egg.entity.{ns}:{i}.name={nm} Spawn Egg"]
        gen.append((i, arch, gkind, tkind, (w, h) if i in sizes else None))
    lf = rp / "texts/en_US.lang"
    old = lf.read_text() if lf.exists() else ""
    lf.write_text(old + ("\n" if old and not old.endswith("\n") else "") + "\n".join(lines) + ("\n" if lines else ""))
    if skipped: notes.append("registered names with no model, texture, language entry or recognisable role (probably property strings): " + ", ".join(f"`{x}`" for x in skipped))
    for i, arch, gk, tk, sz in gen:
        if gk == "placeholder box" or tk == "placeholder colour":
            notes.append(f"entity `{i}` ({arch}): {gk}, {tk}; replace models/entity/{i}.geo.json and textures/entity/{i}.png with real art")
    return gen, notes


# ----------------------------------------------------------------------------------------------------------------
# 3. read the jar
# ----------------------------------------------------------------------------------------------------------------
def slug(s):
    return re.sub(r"[^a-z0-9_]+", "_", s.lower()).strip("_") or "mod"

def camel(s):
    return "".join(p.capitalize() for p in re.split(r"[^A-Za-z0-9]+", s) if p) or "Mod"

def read_meta(z):
    names = z.namelist()
    meta = {"id": "", "name": "", "version": "", "author": "", "loader": "unknown"}
    if "fabric.mod.json" in names:
        j = json.loads(z.read("fabric.mod.json").decode("utf-8", "replace"))
        a = j.get("authors") or []
        a = [x if isinstance(x, str) else x.get("name", "") for x in a]
        meta.update(id=j.get("id", ""), name=j.get("name", ""), version=str(j.get("version", "")), author=", ".join(a), loader="Fabric")
    elif "quilt.mod.json" in names:
        j = json.loads(z.read("quilt.mod.json").decode("utf-8", "replace")).get("quilt_loader", {})
        meta.update(id=j.get("id", ""), name=j.get("metadata", {}).get("name", ""), version=str(j.get("version", "")), loader="Quilt")
    else:
        for f, loader in (("META-INF/neoforge.mods.toml", "NeoForge"), ("META-INF/mods.toml", "Forge")):
            if f in names:
                t = z.read(f).decode("utf-8", "replace")
                g = lambda k: (re.search(r'^\s*%s\s*=\s*"([^"]*)"' % k, t, re.M) or [None, ""])[1]
                meta.update(id=g("modId"), name=g("displayName"), version=g("version"), author=g("authors"), loader=loader)
                break
    if not meta["id"]:
        meta["id"] = slug(pathlib.Path(z.filename).stem)
    meta["id"] = slug(meta["id"])
    meta["name"] = meta["name"] or meta["id"]
    return meta

def mixin_methods(ci):
    """(target classes, hinted method names) of one Mixin class, from its constant pool."""
    targets = [u[1:-1] for u in ci.utf8 if u.startswith("Lnet/minecraft/") and u.endswith(";")]
    targets += [u for u in ci.utf8 if u.startswith("net/minecraft/") and "(" not in u and " " not in u]
    methods = set()
    for u in ci.utf8:
        m = re.match(r"^([A-Za-z_]\w*)(\(.*)?$", u)
        if m: methods.add(m.group(1))
    return targets, methods

def analyse(jar):
    z = zipfile.ZipFile(jar)
    meta = read_meta(z)
    classes = {}
    for n in z.namelist():
        if n.endswith(".class") and not n.startswith("META-INF/"):
            try: classes[n[:-6]] = ClassInfo(z.read(n))
            except Exception: pass
    mixin_classes = set()
    for n in z.namelist():
        if re.search(r"(^|/)[^/]*mixins?[^/]*\.json$", n):
            try:
                j = json.loads(z.read(n).decode("utf-8", "replace"))
                pkg = j.get("package", "").replace(".", "/")
                for k in ("mixins", "client", "server"):
                    for c in j.get(k, []): mixin_classes.add(pkg + "/" + c.replace(".", "/"))
            except Exception: pass
    events, mixin_hits, unmapped_mixins = {}, [], []
    for cname, ci in classes.items():
        refs = set(ci.classes) | set(ci.utf8)
        if cname not in mixin_classes:
            for rx, ev, why in EVENT_RULES:
                hit = next((r for r in refs if re.search(rx, r)), None)
                if hit: events.setdefault(ev, []).append((cname, why, hit))
        else:
            targets, methods = mixin_methods(ci)
            mapped = False
            for t in targets:
                for trx, mrx, ev, why in MIXIN_RULES:
                    if re.search(trx, t) and any(re.search(mrx, m) for m in methods):
                        mixin_hits.append((cname, t, ev, why)); events.setdefault(ev, []).append((cname, "Mixin into " + t.split("/")[-1] + " (" + why + ")", t)); mapped = True
            if not mapped: unmapped_mixins.append((cname, sorted({t.split("/")[-1] for t in targets})[:4]))
    handlers = bind_handlers(classes)
    for ev, hs in bind_mixins(classes, mixin_classes).items(): handlers.setdefault(ev, []).extend(hs)
    for ev in handlers:
        events.setdefault(ev, [(handlers[ev][0].cname, handlers[ev][0].why, "")])
    content = find_content(classes, meta["id"])
    return meta, events, handlers, mixin_hits, unmapped_mixins, content, len(classes), z

# ----------------------------------------------------------------------------------------------------------------
# 4. generate the Bedrock mod
# ----------------------------------------------------------------------------------------------------------------
def java_str(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'

def tmpl_expr(parts):
    """template parts -> Java string expression"""
    out = []
    for x in parts:
        if isinstance(x, str): out.append(java_str(x))
        elif x[0] == "fps": out.append("Math.round(Bedrock.game().fps())")
        else: out.append('"?"')
    return " + ".join(out) if out else '""'

def action_code(act, k):
    kind, parts = act
    e = tmpl_expr(parts)
    if kind == "toast": return f"toast({e});"
    if kind == "log": return f"log({e});"
    return f'Bedrock.hud().line(ID + ".{k}", {e});'

def gen_generic(meta, pkg, cls, events, handlers):
    handled, blocks, cfg = [], [], []
    has_hud = any(a[0] == "hud" for hs in handlers.values() for h in hs for a in h.actions)
    for ev in sorted(events):
        if ev == "hud" and not has_hud: continue
        key = "on_" + ev.replace(".", "_")
        cfg.append(f'.toggle({java_str(key)}, {java_str("React to " + ev)}, true)')
        acts = []
        for hi, h in enumerate(handlers.get(ev, [])):
            for ai, a in enumerate(h.actions):
                acts.append(action_code(a, f"h{hi}_{ai}"))
        body = "\n                ".join(acts)
        src = events[ev][0]
        reg_ev = "fps" if ev == "hud" else ev
        if ev == "tick":
            blocks.append(f"\n        // {src[0].split('/')[-1]}: {src[1]}\n        Events.onTick(new Runnable() {{ public void run() {{\n            if (!ctx.config.getBoolean({java_str(key)})) return;\n            count(\"tick\");\n            {body}\n        }} }});")
        else:
            blocks.append(f"\n        // {src[0].split('/')[-1]}: {src[1]}\n        Events.on({java_str(reg_ev)}, new EventHandler() {{\n            public void handle(String name, String data) {{\n                if (!ctx.config.getBoolean({java_str(key)})) return;\n                count({java_str(ev)});\n                {body}\n            }}\n        }});")
        handled.append(ev)
    cfgchain = "ctx.config" + "".join("\n                " + c for c in cfg) + ";" if cfg else ""
    return f"""package {pkg};

import com.modhost.api.EventHandler;
import com.modhost.api.Events;
import com.modhost.api.ModContext;
import com.modhost.bedrock.Bedrock;
import com.modhost.bedrock.BedrockMod;

import java.util.HashMap;
import java.util.Map;
import java.util.TreeMap;

/**
 * Bedrock version of "{meta["name"]}" ({meta["loader"]}), generated by mods-in/convert.py.
 * Each handler of the original was read from its bytecode and rewritten as the Bedrock calls it amounts to
 * (messages -> toasts, log lines, text drawn on screen). See REPORT.md for what could not be rewritten.
 */
public class {cls} implements BedrockMod {{
    private static final String ID = {java_str(meta["id"])};
    private static final String NAME = {java_str(meta["name"])};
    private ModContext ctx;
    private final Map<String, Integer> seen = new TreeMap<String, Integer>();
    private final Map<String, Long> last = new HashMap<String, Long>();

    public void onLoad(ModContext c) {{
        ctx = c;
        {cfgchain}
{"".join(blocks)}
    }}

    /** At most one toast every 2 seconds. */
    private void toast(String text) {{
        if (!allow("toast")) return;
        Bedrock.hud().toast(text);
    }}

    /** Same text at most once every 2 seconds. */
    private void log(String text) {{
        if (allow("log:" + text)) ctx.logger.info(text);
    }}

    private boolean allow(String key) {{
        long now = System.currentTimeMillis();
        synchronized (last) {{
            Long t = last.get(key);
            if (t != null && now - t < 2000) return false;
            last.put(key, now);
            return true;
        }}
    }}

    /** Counts how often each converted event fired (add your own Bedrock behaviour here if you want more). */
    protected void count(String event) {{
        synchronized (seen) {{ Integer n = seen.get(event); seen.put(event, n == null ? 1 : n + 1); }}
    }}

    public void onEnable()  {{ ctx.logger.info("enabled; events: {", ".join(handled) or "none"}"); }}
    public void onDisable() {{ Bedrock.hud().clear(ID); }}
}}
"""

def gen_perf(meta, pkg, cls):
    src = (ROOT / "java/mods/lithium-bedrock/dev/modhost/lithium/LithiumBedrock.java").read_text()
    src = src.replace("package dev.modhost.lithium;", f"package {pkg};").replace("class LithiumBedrock", f"class {cls}")
    src = src.replace('"lithium"', java_str(meta["id"])).replace('"Lithium: "', java_str(meta["name"] + ": "))
    src = src.replace('"Lithium "', java_str(meta["name"] + " "))
    return src

def gen_fps(meta, pkg, cls):
    src = (ROOT / "java/mods/fps-hud/dev/modhost/fpshud/FpsHud.java").read_text()
    src = src.replace("package dev.modhost.fpshud;", f"package {pkg};").replace("class FpsHud", f"class {cls}")
    return src.replace('"fps_hud"', java_str(meta["id"]))

def convert(jar, install):
    meta, events, handlers, mixin_hits, unmapped, content, nclasses, z = analyse(jar)
    mid, name = meta["id"], meta["name"]
    pkg = "dev.modhost.converted." + mid
    cls = camel(mid)
    out = HERE / "converted" / mid
    if out.exists(): shutil.rmtree(out)
    srcdir = out / ("dev/modhost/converted/" + mid)
    srcdir.mkdir(parents=True)

    port = next(((kind, why) for rx, kind, why in KNOWN if re.match(rx, mid)), None)
    native = set(events)
    if port and port[0] == "skip": mode, code = "skipped", None
    elif port and port[0] == "perf": mode, code = "port:performance", gen_perf(meta, pkg, cls)
    elif port and port[0] == "fps": mode, code = "port:fps-hud", gen_fps(meta, pkg, cls)
    elif native: mode, code = "events", gen_generic(meta, pkg, cls, events, handlers)
    else: mode, code = "nothing to convert", None
    if code:
        (srcdir / (cls + ".java")).write_text(code)
        (out / (mid + ".json")).write_text(json.dumps({
            "id": mid, "name": name, "version": meta["version"] or "converted", "author": meta["author"] or "converted by ModHost",
            "bedrock": pkg + "." + cls}, indent=2) + "\n")
    items, blocks, entities, addon_notes = build_addon(z, meta, content, out)
    ent_gen, ent_notes = build_entities(z, meta, entities, out)
    addon_notes = addon_notes + ent_notes

    # ---- report
    R = [f"# {name}  ->  Bedrock", "",
         f"* source: `{pathlib.Path(jar).name}`  ({meta['loader']}, id `{mid}`, version `{meta['version']}`, {nclasses} classes)",
         f"* mod: **{mode}**" + (f"  -> `java/mods/{mid}/`" if code else "  (no mod generated)"),
         f"* add-on: **{len(items)} item(s), {len(blocks)} block(s), {len(ent_gen)} entity(ies)**  -> `mods-in/converted/{mid}/addon/` (also zipped as `{mid}.mcaddon`)" if (items or blocks or ent_gen) else "* add-on: none", ""]
    if port: R += ["## Known mod", port[1], ""]
    if events:
        R += ["## Events", "| Java source | Bedrock event | status | rewritten actions |", "|---|---|---|---|"]
        for ev in sorted(events):
            w = events[ev][0]
            st = "DORMANT: " + DORMANT[ev] if ev in DORMANT else ("HUD (drawn on the 1 s tick)" if ev == "hud" else ("Events.onTick" if ev == "tick" else "native hook"))
            n = sum(len(h.actions) for h in handlers.get(ev, []))
            R.append(f"| {w[0].split('/')[-1]}: {w[1]} | `{ev}` | {st} | {n} |")
        R.append("")
    if handlers:
        R += ["## Handlers read from bytecode"]
        for ev, hs in sorted(handlers.items()):
            seen_h = set()
            for h in hs:
                if (h.cname, h.mname, h.why) in seen_h: continue
                seen_h.add((h.cname, h.mname, h.why))
                R.append(f"* `{ev}` <- `{h.cname.split('/')[-1]}.{h.mname}` ({h.why})")
                for kind, parts in h.actions: R.append(f"  * rewritten: {kind} {tmpl_expr(parts)}")
                if h.untranslated:
                    top = sorted(h.untranslated.items(), key=lambda kv: -kv[1])[:10]
                    R.append("  * not rewritten (Java Edition calls with no Bedrock equivalent): " + ", ".join(f"`{k}`" for k, _ in top))
        R.append("")
    mixin_hits = list(dict.fromkeys(mixin_hits))
    if mixin_hits:
        R += ["## Mixins mapped"] + [f"* `{c.split('/')[-1]}` into `{t.split('/')[-1]}` -> `{ev}` ({why})" for c, t, ev, why in mixin_hits] + [""]
    R += ["## NOT converted"]
    if unmapped:
        groups = {}
        for c, tg in unmapped:
            cat, eq = triage_mixin(c, tg); groups.setdefault((cat, eq), []).append(c.split("/")[-1])
        R.append(f"* {len(unmapped)} Mixin class(es) have no native hook. Closest Bedrock mechanism for each group (none of these can run Java Edition code):")
        for (cat, eq), cl in sorted(groups.items()):
            R.append(f"  * **{cat}** ({len(cl)}): " + ", ".join(f"`{x}`" for x in cl))
            R.append(f"    * Bedrock equivalent: {eq}")
        (out / "mixins.json").write_text(json.dumps([{"mixin": c.split("/")[-1], "targets": tg, "category": triage_mixin(c, tg)[0]} for c, tg in unmapped], indent=2))
    if ent_gen:
        R.append("* Entities generated as Bedrock JSON (behaviour + client definition + geometry + texture + spawn egg): " + ", ".join(f"`{i}` ({a})" for i, a, *_ in ent_gen) +
                 ". Behaviour is a template for the entity's role (seat / vehicle / projectile / mob) chosen from its name; the Java AI, custom controls and animations are not converted.")
    elif entities: R.append("* Entities registered (" + ", ".join(entities[:8]) + "): no model, texture or role could be recognised, so none were generated.")
    if items or blocks: R.append("* Items/blocks have identity, name, texture and a plain full-block / stack-of-64 definition only. Their Java behaviour (use, break, tick, recipes, loot, world gen) is not converted.")
    for n in addon_notes: R.append("* " + n)
    for e in events:
        if e in DORMANT: R.append(f"* `{e}`: {DORMANT[e]}")
    any_untr = any(h.untranslated for hs in handlers.values() for h in hs)
    if any_untr: R.append("* Handler lines that call `net.minecraft.*` for things other than messages, logging and drawn text (health, inventory, entities, blocks, sounds, particles) have no Bedrock-API equivalent and are listed above.")
    if mode == "skipped": R.append("* Skipped: " + port[1])
    if mode == "nothing to convert": R.append("* No game event, known-mod behaviour or hookable Mixin was found, so no Bedrock mod was generated.")
    (out / "REPORT.md").write_text("\n".join(R) + "\n")

    if items or blocks or ent_gen:   # a ready-to-import pack
        import zipfile as zf
        with zf.ZipFile(out / (mid + ".mcaddon"), "w", zf.ZIP_DEFLATED) as mc:
            for f in (out / "addon").rglob("*"):
                if f.is_file(): mc.write(f, f.relative_to(out / "addon"))
    if install and code:
        dest = ROOT / "java/mods" / mid
        if dest.exists(): shutil.rmtree(dest)
        shutil.copytree(out, dest, ignore=shutil.ignore_patterns("REPORT.md", "addon", "*.mcaddon"))
    return mid, mode, sorted(events), len(unmapped), len(items), len(blocks)

def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    install = "--install" in sys.argv
    jars = [pathlib.Path(a) for a in args] or sorted(HERE.glob("*.jar"))
    if not jars:
        print("no .jar files in mods-in/ - put Java mods there, then run this again"); return 1
    (HERE / "converted").mkdir(exist_ok=True)
    rows = []
    for j in jars:
        try:
            mid, mode, evs, nm, ni, nb = convert(j, install)
            rows.append(f"{j.name:40} -> {mid:22} {mode:20} events={','.join(evs) or '-'}  items={ni} blocks={nb}  unmapped mixins={nm}")
        except Exception as e:
            rows.append(f"{j.name:40} FAILED: {e}")
    print("\n".join(rows))
    (HERE / "converted" / "SUMMARY.txt").write_text("\n".join(rows) + "\n")
    print(f"\nreports + sources: mods-in/converted/<id>/   {'installed into java/mods/' if install else 'add --install to copy into java/mods/'}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
