package com.modhost.bedrock;

import com.modhost.api.EventHandler;
import com.modhost.api.Events;
import com.modhost.api.NativeBridge;

/**
 * Native hooks into Bedrock's own code. The C++ side finds each function in libminecraftpe.so by byte pattern,
 * patches it, and raises the event just BEFORE the game function runs (data is empty). A hook that was not found
 * never fires: check {@link #resolved(String)}.
 *
 * world.join, world.leave, player.login, player.tick, level.tick, game.update (max 20/s), block.break, block.place,
 * mob.kill, player.die, mob.interact, item.use, item.drop, item.craft, player.teleport, chat.send, chat.display.
 * Also "hook.first" (data = event name) the first time each hook runs. player.attack is wired but only fires once a pattern for it exists (docs/HOOKS.md).
 */
public final class Hooks {
    static final Hooks INSTANCE = new Hooks();

    public static final String[] EVENTS = {
        "world.join", "world.leave", "player.login", "player.tick", "level.tick", "game.update", "block.break",
        "block.place", "mob.kill", "player.die", "mob.interact", "item.use", "item.drop", "item.craft",
        "player.teleport", "chat.send", "chat.display", "player.attack" };

    private Hooks() {}

    public void on(String event, EventHandler handler) { Events.on(event, handler); }

    /** True if the native side located and installed the hook behind this event. */
    public boolean resolved(String event) {
        try { return NativeBridge.hookStatus().contains("\n" + event + "=ok"); }
        catch (Throwable t) { return false; }
    }

    /** One line per hook: "name=ok" or "name=missing". */
    public String report() {
        try { return NativeBridge.hookStatus(); } catch (Throwable t) { return "native hooks unavailable"; }
    }
}
