# Motion lab

A page for watching the interface's glass effects: popups opening and
closing (`panel_motion.js`) and the intro's polygon word (`intro_word.js`),
both built on `glass_mesh.js`. It loads the product's own files, so what you
see here is what Web Chat, onboarding and the workflow page draw. Nothing in
the product loads this page.

Serve the repository root (the page loads the product files by relative
paths) and open the lab:

```bash
python -m http.server 18110 --bind 127.0.0.1
```

Then open `http://127.0.0.1:18110/ui/motion-lab/`.

- **Play** opens a setup-sized popup (added and removed, like onboarding's
  guides), a large popup (shown and hidden with `hidden`, like Web Chat's
  Settings) and a native dialog (`showModal` and `close`, like the workflow
  page), or plays the intro word.
- **Speed** slows everything down or freezes it (`lab-clock.js` runs the
  page's timers, animation frames and CSS animations on a lab clock), so a
  single moment can be inspected.
- **Theme** switches dark and light mode, picks a preset or sets the dark
  metal, the gold tint (the dark accent) and the blue light (the light
  accent). The glass is the interface's glass of the mode (teal in dark,
  white in light) and should stand out on every background; the glints and
  the brackets' accent follow the theme. **Theme colors in use** shows the
  values the effects read. **Interface animation** off should make popups
  appear and go at once.

In dark mode the background is the product's metallic mesh. In light mode the
page draws a simple gradient from the four light colors, not the product's
light background, which is enough to judge the light glass and brackets.
