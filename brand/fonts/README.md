# Caption fonts

Drop a **bold, punchy display font** here for burned-in captions. The brand fonts
(Cinzel / Poppins) are too thin for viral-style captions, so caption rendering uses
a font from this folder while colors still come from `brand/theme.json`.

Recommended (all free, OFL-licensed — safe to redistribute):

- **Anton** — https://fonts.google.com/specimen/Anton (single ultra-bold weight, the
  classic "MrBeast/Hormozi" caption look). Save as `Anton-Regular.ttf`.
- **Montserrat** ExtraBold/Black — https://fonts.google.com/specimen/Montserrat
  Save the `Montserrat-ExtraBold.ttf` / `Montserrat-Black.ttf` file here.

`config/caption_styles.json` references fonts by **family name** (e.g. `"Anton"`); the
font file must be installed on the system OR present here and registered with libass.
On Windows the simplest path is to double-click the `.ttf` to install it system-wide so
ffmpeg's libass can find it by name. `build_captions.py` falls back to Arial if the
named font isn't found.
