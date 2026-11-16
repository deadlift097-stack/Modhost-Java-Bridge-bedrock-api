Java mod -> Bedrock converter
=============================
1. Put Java mod .jar files in this folder (mods-in/).
2. Run:   python3 mods-in/convert.py --install
   (or run the "convert-mods" workflow on GitHub: Actions > convert-mods > Run workflow)
3. For every jar you get mods-in/converted/<id>/ with the generated Bedrock source and REPORT.md.
   --install also copies it to java/mods/<id>/, and scripts/build-java.sh builds it into a mod for the phone.
   Copy the built .jar + .json from build/java/mods/ to <dataDir>/mods and restart the game.

What it does (it does NOT run or load the Java mod; it reads the jar and writes new Bedrock-API code):
- known mods (Lithium, Sodium, FPS counters ...) -> a Bedrock port of what they do for the player
- game events the mod listens to (Fabric / NeoForge / Forge events, Mixin injection points) -> the same moments on
  Bedrock through the native hooks (world.join, block.break, chat.send ...)
- each handler's BYTECODE is read and rewritten: messages sent to the player -> toasts, log lines -> log, text drawn on
  screen -> HUD lines (a "FPS: " + getFps() style text uses the real Bedrock frame rate)
- blocks and items the mod registers -> a Bedrock add-on (behavior pack + resource pack with names and textures) in
  mods-in/converted/<id>/addon and <id>.mcaddon
- everything else is listed under "NOT converted" in REPORT.md: calls into Java Edition game code with no Bedrock
  equivalent (health, inventory, entities, sounds, particles), rendering, world generation, entity models/AI, item/block behaviour.
