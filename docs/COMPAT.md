# Converting Java mods to Bedrock

1. Put the `.jar` in `mods-in/`, then run `python3 mods-in/convert.py --install` (or the **convert-mods** workflow) and
   download `converted-mods`.
2. Copy the built `.jar` + `.json` into `<dataDir>/mods` and restart the game.
3. Open the in-game **Mods** button; the converted mod shows a "Bedrock API" badge and its settings under Config.

The converter reads the jar and writes a new mod against the Bedrock API (docs/BEDROCK_API.md): known mods get a Bedrock
port, game events (Fabric/NeoForge/Forge events and Mixin injection points) are re-wired to the native hooks, and the
messages those handlers send become toasts. Rendering, world generation, new blocks/items/entities and Mixins into code
Bedrock does not expose cannot be carried over; `REPORT.md` for each mod lists exactly what was and was not converted.

## Troubleshooting
- Open **Mods > Copy report**. It copies every mod's status plus the last 80 log lines to the clipboard and saves
  them to `<dataDir>/data/modhost-report.txt`. Send that instead of screenshots.
- Tap a mod's gear to see a plain-language note on well-known mods (what they do on Java Edition and why they
  have no Bedrock equivalent).
- Orange = Java Edition only (cannot work on Bedrock). Red = something fixable: a missing library or an
  API call the compatibility layer lacks. Red ones are worth reporting.
