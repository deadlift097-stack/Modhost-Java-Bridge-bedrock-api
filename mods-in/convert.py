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
]
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
    for ci in classes.values():
        for m in ci.methods:
            reg, st = None, []
            for op, arg, pc in ci.instructions(m):
                if op == 0xb2:
                    owner, name, desc = ci.member(struct.unpack(">H", arg)[0])
                    if REG_FIELD.match(name): reg = name.rstrip("S").lower().replace("_type", "")
                    st.append(("fld", name))
                elif op in (0x12, 0x13):
                    e = ci.cp[arg[0] if op == 0x12 else struct.unpack(">H", arg[:2])[0]]
                    st.append(("str", ci.u(e[1])) if e and e[0] == 8 else ("c",))
                elif op in (0xb6, 0xb7, 0xb8, 0xb9):
                    owner, name, desc = ci.member(struct.unpack(">H", arg[:2])[0])
                    n = _arg_count(desc); args = st[-n:] if n else []; st = st[:-n] if n else st
                    if name in REG_CALL and reg:
                        for a in args:
                            if a[0] == "str" and ID_RX.match(a[1]):
                                found[(reg, a[1])] = 1
                    if not _ret_void(desc): st.append(("r",))
                else:
                    if len(st) > 8: st = st[-8:]
    return sorted((k, i) for (k, i) in found)


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
        tex = next((n for n in names if re.match(r"assets/[^/]+/textures/item/%s\.png$" % re.escape(i), n)), None)
        comp = {"minecraft:max_stack_size": 64}
        if tex:
            (rp / "textures/items" / (i + ".png")).write_bytes(z.read(tex)); item_tex[f"{ns}_{i}"] = {"textures": f"textures/items/{i}"}
            comp["minecraft:icon"] = {"textures": {"default": f"{ns}_{i}"}}
        (bp / "items" / (i + ".json")).write_text(json.dumps({"format_version": "1.21.0", "minecraft:item": {
            "description": {"identifier": f"{ns}:{i}", "menu_category": {"category": "items"}}, "components": comp}}, indent=2))
        lines.append(f"item.{ns}:{i}={lang.get(f'item.{ns}.{i}', title(i))}")
    for b in blocks:
        tex = next((n for n in names if re.match(r"assets/[^/]+/textures/block/%s\.png$" % re.escape(b), n)), None)
        inst = {"*": {"texture": f"{ns}_{b}", "render_method": "opaque"}}
        if tex:
            (rp / "textures/blocks" / (b + ".png")).write_bytes(z.read(tex)); terrain[f"{ns}_{b}"] = {"textures": f"textures/blocks/{b}"}
        else: notes.append(f"block `{b}`: no texture found in the jar; add textures/blocks/{b}.png")
        (bp / "blocks" / (b + ".json")).write_text(json.dumps({"format_version": "1.21.0", "minecraft:block": {
            "description": {"identifier": f"{ns}:{b}", "menu_category": {"category": "construction"}},
            "components": {"minecraft:material_instances": inst, "minecraft:geometry": "minecraft:geometry.full_block"}}}, indent=2))
        lines.append(f"tile.{ns}:{b}.name={lang.get(f'block.{ns}.{b}', title(b))}")
    if item_tex: (rp / "textures/item_texture.json").write_text(json.dumps({"resource_pack_name": ns, "texture_name": "atlas.items", "texture_data": item_tex}, indent=2))
    if terrain: (rp / "textures/terrain_texture.json").write_text(json.dumps({"resource_pack_name": ns, "texture_name": "atlas.terrain", "padding": 8, "num_mip_levels": 4, "texture_data": terrain}, indent=2))
    (rp / "texts/en_US.lang").write_text("\n".join(lines) + "\n")
    return items, blocks, entities, notes


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

    # ---- report
    R = [f"# {name}  ->  Bedrock", "",
         f"* source: `{pathlib.Path(jar).name}`  ({meta['loader']}, id `{mid}`, version `{meta['version']}`, {nclasses} classes)",
         f"* mod: **{mode}**" + (f"  -> `java/mods/{mid}/`" if code else "  (no mod generated)"),
         f"* add-on: **{len(items)} item(s), {len(blocks)} block(s)**  -> `mods-in/converted/{mid}/addon/` (zip both folders as a .mcaddon)" if (items or blocks) else "* add-on: none", ""]
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
            for h in hs:
                R.append(f"* `{ev}` <- `{h.cname.split('/')[-1]}.{h.mname}` ({h.why})")
                for kind, parts in h.actions: R.append(f"  * rewritten: {kind} {tmpl_expr(parts)}")
                if h.untranslated:
                    top = sorted(h.untranslated.items(), key=lambda kv: -kv[1])[:10]
                    R.append("  * not rewritten (Java Edition calls with no Bedrock equivalent): " + ", ".join(f"`{k}`" for k, _ in top))
        R.append("")
    if mixin_hits:
        R += ["## Mixins mapped"] + [f"* `{c.split('/')[-1]}` into `{t.split('/')[-1]}` -> `{ev}` ({why})" for c, t, ev, why in mixin_hits] + [""]
    R += ["## NOT converted"]
    if unmapped:
        R.append(f"* {len(unmapped)} Mixin class(es) patch Java Edition code Bedrock does not expose (rendering, AI, world generation, packets): " +
                 ", ".join(f"`{c.split('/')[-1]}`" for c, _ in unmapped[:12]) + (" ..." if len(unmapped) > 12 else ""))
    if entities: R.append("* Entities registered (" + ", ".join(entities[:8]) + "): need models, animations and AI in Bedrock JSON; not generated.")
    if items or blocks: R.append("* Items/blocks have identity, name, texture and a plain full-block / stack-of-64 definition only. Their Java behaviour (use, break, tick, recipes, loot, world gen) is not converted.")
    for n in addon_notes: R.append("* " + n)
    for e in events:
        if e in DORMANT: R.append(f"* `{e}`: {DORMANT[e]}")
    any_untr = any(h.untranslated for hs in handlers.values() for h in hs)
    if any_untr: R.append("* Handler lines that call `net.minecraft.*` for things other than messages, logging and drawn text (health, inventory, entities, blocks, sounds, particles) have no Bedrock-API equivalent and are listed above.")
    if mode == "skipped": R.append("* Skipped: " + port[1])
    if mode == "nothing to convert": R.append("* No game event, known-mod behaviour or hookable Mixin was found, so no Bedrock mod was generated.")
    (out / "REPORT.md").write_text("\n".join(R) + "\n")

    if items or blocks:   # a ready-to-import pack
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
