---
name: Universal Agents Onboarding UI
version: 2
surface: browser-onboarding-web-chat-and-docs
tokens:
  color:
    ink: "#10201c"
    muted: "#334942"
    surface_panel: "rgba(247, 255, 252, 0.28)"
    surface_panel_strong: "rgba(255, 255, 255, 0.44)"
    border_outer: "rgba(229, 255, 247, 0.32)"
    border_inner: "rgba(28, 69, 60, 0.14)"
    ocean: "#075985"
    ocean_mid: "#0284c7"
    ocean_light: "#7dd3fc"
    green: "#16a34a"
    green_dark: "#0f766e"
    gold_brand: "#f6c84c"
    amber: "#b7791f"
    danger: "#b91c1c"
    dark_ink: "#f2f7f8"
    dark_muted: "#adc1c8"
    dark_surface: "#007007"
    dark_metal: "#015b1a"
    dark_metal_gold: "#464020"
    dark_metal_bronze: "#00751d"
    dark_ui_canvas: "#070b0e"
    dark_panel: "#0b151b"
    dark_panel_strong: "#10232d"
    dark_panel_raised: "#173440"
    dark_border: "#367287"
    dark_primary: "#087697"
    dark_focus: "#62d5f3"
    dark_ember: "#f0a35a"
    dark_danger: "#b94850"
    dark_system_text: "#c8cfd2"
  radius:
    default: 10px
    compact: 7px
    modal: 14px
  shadow:
    frosted: "0 34px 120px rgba(3, 28, 42, 0.34)"
  typography:
    family: "Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif"
    heading_weight: 880
    control_weight: 800
  motion:
    background_morph_seconds: 9
    accordion_ms: 260
    overlay_ms: 220
---

# Universal Agents Design System

This file captures the visual rules for the Universal Agents onboarding flow,
the post-onboarding Web Chat surface, and the built-in documentation surface. It
follows the DESIGN.md idea: keep design intent, tokens, component rules, and
interaction guidance in plain text that future coding agents can read without
opening screenshots or reverse-engineering CSS.

## Team transfer review

The operator opens `assistant_cli team review` on the destination host. Use the
shared configuration-page appearance, mesh, typography, theme controls and
opaque alternating record rows. Show the team, destination root and private
review folder before the records.

- Give each record its own selection checkbox. Filtering and selecting shown
  records preserve selections outside the filter. Support later batches from
  the same backup after the first import.
- Show editable settings as YAML, memories as text, and each schedule as
  Markdown with one fenced YAML block. Save private files so the operator can
  edit them outside the browser too.
- Require confirmation for every selected schedule after its last edit. It
  covers timing, timezone, instructions, enabled state, owner and delivery
  route. Show the saved remaining run count and explain that editing the repeat
  count resets it. Editing clears confirmation. Refuse ownership or approval-provenance
  changes and explain how to recreate a wrong-owner or wrong-route schedule.
- Keep **Import selected records** disabled until saved selections and edits
  pass validation. Unsaved edits block validation. Every change invalidates
  the earlier validation; the server checks the exact digest again at import.
  Save requests run in order. Keep newer edits dirty while an earlier save
  finishes, discard validation responses from before a change, and block
  validation and import while saves are pending.
  Bind schedule confirmation to the submitted text and clear its visible
  checkbox whenever saving clears the stored confirmation.
  Disable destination choices, selections, record edits, saves, confirmations
  and bulk selection during the import request. Filtering may redraw records,
  but their controls stay disabled until the request finishes. Keep review
  changes available during ordinary validation.
- Show refusals and dependency reasons in text. Require correction or
  deselection before writing, and display attention notes. Later batches skip
  imported identities and preserve destination edits.
- List credentials by name and owner with individual selection. Never show
  secret values. Explain that credentials need the running broker and other
  imports need a stopped destination runtime.
- Report partial credential failures without implying success. Explain
  unfinished imports and backup restoration. Validation is not an import.
- Use a temporary loopback page with no token in browser storage and no content
  caching. Its token grants access to this operator review, separate from the
  person's gateway session.

Onboarding's **Import a team export** (guided upgrade, 2026-10-12) runs the
same review page and script (`team_review.js`, `team_review.css`) on
onboarding's server, titled **Choose what to import**, with these differences:

- Records show readable names (a person's name and role, a page title, a
  memory's text, a schedule's name) grouped under section headings in import
  order; the record key stays visible, smaller, inside the opened record.
- Everything starts selected. The import button reads **Import and start the
  team**; after a successful import both actions hide, the result line says the
  team is starting, and the page opens Web Chat once the runtime answers.
- A card above the records asks **Secrets come from the environment (Docker
  secrets). Don't write team.env.**, preset to the old instance's choice and
  saying which it was. Save changes through the same ordered queue as record
  edits, including changes made during validation. Keep a failed choice save
  dirty and block import. **Validate selected records** retries that visible
  choice before validation, so the person can retry without changing it.
  Every choice change requires validation again.
- Credentials waiting for the first start show as attention lines, not
  failures. Missing environment secrets are listed by name, purpose and how to
  create them, and the page does not wait for a start.
- No review folder is shown: it is private to the new instance and deleted
  after the import. The token, as on the loopback page, is never stored; after
  a reload the person opens the export again and keeps their selections.

## Design Intent

- The interface should feel like a calm, high-agency setup console, not a
  marketing landing page.
- The visual signature is a living polygonal mesh behind frosted functional UI.
- Configuration should remain legible even when vivid background colors are
  active.
- The same design language applies to onboarding, launch/status, setup overlays,
  Web Chat, Docs, generated sites, and credential collection pages.
- Prefer utility, clarity, and steady progress over decoration.

## Background System

- Every full-screen product surface should use the selected polygonal mesh
  background. Dark metallic is the checked-in default; diffuse light remains a
  complete selectable theme.
- The dark theme is polished, brushed metal in a dark photo studio. The
  browser draws a faceted 3D sheet of metal plates; every pixel reflects a dark
  room and one large rectangular softbox light. Because a flat mirror reflects a
  flat light as a straight-edged shape, each face shows a straight brightness
  ramp and neighboring faces contrast sharply. That reflection model, not
  painted gradients, is what makes the surface read as metal (an earlier
  painted version stayed dull because its brightest faces only reached
  mid-grey).
- One light, no others. An earlier studio had four (the main strip, a cool rim
  light opposite it, a dim overhead fill and a low warm light). The main light
  sat far beyond the top of the screen, so it lit mostly plate edges, and the
  owner found that out of place (2026-10-03); on reflective metal the low
  lights also read as stray glints. People place the light with **Light left
  or right** and **Light up or down**, measured from the screen center where
  100% reaches the edge and up to 300% places it off screen, and set **Light
  softness**. Off screen it lights mostly plate edges and steep plates; over
  the screen it lights plates broadly. Softness makes the light larger
  (doubling every 25 points) and blurs its edges; it dims in proportion to its
  area, so a soft light gives even, flatter metal instead of washing out, and
  **Light strength** brings the brightness back.
- Defaults, chosen by the owner on 2026-10-04 with these controls (working
  defaults "for now"): green metal (`#015b1a`) in bright green surroundings
  (`#007007`), with olive-gold (`#464020`) and green (`#00751d`) accent tints
  on 10% and 39% of the plates and no chrome faces. Deep plates (facet depth
  80%), light strength 51%, edge highlights 31%, brushed grain 24%. The light
  rests just left of and above the screen center (left/right -7%, up/down -5%)
  at softness 37 and does not wander (wander distance stays at 133% for when
  it is turned on); reflection speed 1.10×, mesh speed 1.00×. Accent tints
  show mainly where the metal is lit, so shadowed accent faces stay close to
  the base metal. The surface color is the surroundings that unlit faces
  reflect. (The earlier steel defaults aimed for mostly dark gunmetal planes
  with about 5–10% of the screen bright; the light now sits over the screen
  and lights the plates broadly.)
- Faces are broad plates, never long narrow slivers: every visible corner stays
  above about 25°. No face may be disproportionately large next to the faces
  around it: a face stays within about 2.5× the area of its neighbors. Across
  the whole screen the largest face is usually about 3× the median and at most
  about 6×, because the point spacing varies about 2.5× between calm areas of
  large plates and busier areas of smaller ones; those areas scale with screens
  larger than a typical laptop so big monitors get the same amount of variety.
  Two rules add points instead of ever deleting them (deleting points to fix
  narrow faces cascaded into huge faces): any gap wider than the local spacing
  gets a point at its center, and any face more than about 2.2× its neighbors
  gets a point inside it. tests/metallic_mesh_geometry.test.cjs checks these
  limits for several screen sizes, layouts and two minutes of motion.
- Chrome comes from a few broad faces that already point nearly at the light and
  receive a small extra tilt: aimed at the light's middle they read as bright
  chrome plates, aimed at its soft edge as chrome-to-grey ramps. Large tilts
  drag neighboring faces into the light and make the background busy.
- Each face has one even lighting gradient of its own, and neighbors differ
  because they face different ways. A flat face is a mirror, so the light's
  straight outline used to cross the middle of a face and light half of it
  together with half of its neighbor, which the owner found unrealistic
  (2026-10-04). The face's light is now taken at its three corners and
  blended across it (the still drawing fits the same plane per color
  channel). A broad sheen adds brightness by how directly a face mirrors the
  light's middle, wider and dimmer on rougher faces, so every face reads as a
  lit 3D plate even where it does not reflect the light itself.
- Each plate edge has a 1–2 px chamfer that catches light from the side it
  faces, which gives thin hairlines; inward folds get a soft shadow. The face
  and its chamfer are shaded separately and blended, so hairlines never break
  into dashes. The chamfer is lit per pixel, but an edge is never more than
  2.5 times as bright as its face (plus a small floor): unlimited, a
  mirror-smooth chamfer drew near-white lines between dark plates, which the
  owner found overexposed (2026-10-04).
- The dark mesh is alive in Web Chat and onboarding. Points drift on a slow
  swirling current and keep their spacing. When two triangles should swap
  their shared edge they do, and the replaced pair fades out over about a
  second, because on mirror-like metal any instant change flashes. Faces tilt
  slowly into and out of the light, and chrome moments move from face to face.
  **Mesh speed** controls drift and reconnection; **Reflection speed** controls
  the light, face tilt and chrome moments. The light swings a little around the
  screen center and drifts a little, so a light placed over the screen moves
  too. **Wandering light** (off by default) also lets the light roam around
  its place, up to **Wander distance** (0–150%, default 60%), on a smooth
  seeded path that never repeats exactly and takes about a minute to cross at
  1.00× reflection speed, so it lights different parts of the interface over
  time. The strip keeps its orientation while it wanders, so a light passing
  the center does not spin. Still pages show the light at its resting place.
  The two speeds are independent because a
  tilting face reads as moving light.
- **Basic background** is a still 2D drawing of the same mesh with the same
  lighting, without WebGL, motion, grain or bevels. People can choose it in
  Appearance; it is also drawn, with a console warning, when the browser has no
  WebGL2. The dark background draws on its own canvas layer directly above the
  page canvas, because the light theme holds a 2D context on the page canvas
  and a canvas cannot switch to WebGL afterwards.
- The light theme is a two-pane composition:
  - a lower light pane with four diffuse color sources,
  - an upper polygon pane with slowly moving triangular geometry.
- Default light source colors:
  - dark blue: `#082f73`
  - bright yellow: `#ffd60a`
  - bright green: `#20f05a`
  - bright red: `#ff3030`
- Light diffusion must respect user-selected colors:
  - low diffusion keeps each quadrant saturated and true to the selector,
  - high diffusion blends lights into a softer shared field.
- Polygon fills should be grayscale with per-polygon opacity between 20% and
  70% by default.
- Polygon edge color and opacity are user-configurable.
- Polygon animation should be slow:
  - points drift over roughly 9 second cycles,
  - movement should be perceptible but never distract from reading,
  - reduced-motion users get a static mesh.
- Keep light-theme perimeter vertices pinned to their viewport edge while the
  interior mesh morphs. Boundary vertices may travel along an edge but must not
  uncover the page canvas.
- Light-theme polygon size should stay medium-large:
  - avoid tiny noisy shards,
  - avoid huge facets that make the mesh look like broad flat bands,
  - triangles should generally read between 90px and 220px across on desktop,
  - use proportionally fewer/larger polygons on narrow mobile viewports.
- Web Chat and Docs must reuse the saved onboarding background configuration
  from `instance_config.yaml` through `ui.onboarding.appearance`.
- `ui.onboarding.appearance` is the signed-out and onboarding baseline. After
  authentication, the gateway applies the appearance preference owned by that
  canonical user across Web Chat, runtime windows, the config editor, and Docs.
  **Reset to Defaults** deletes the user preference and reveals the baseline;
  user preferences never rewrite `instance_config.yaml`.
- Both themes have a stable mesh seed and a **Regenerate Mesh** action. A new
  seed changes the composition without changing the other tuning values.
- Generated sites served under `/docs/sites/` must use the same
  static polygonal background and runtime theme assets as Docs.
- Standalone credential and broker auth pages must match the same frosted
  visual language and embedded static mesh, even when the main website service
  is not running.
- The broker's account link form states whose account it is, a **Used by**
  line naming the agents that may use the tool, where the credential goes and
  how it is sent. It then offers two parts, each a small form with its own
  confirmation checkbox: **Use a stored credential** (only when one has every
  field the tool needs) and **Add a new credential** (one masked field with
  the standard inline reveal button per required field, and **Save and link
  account**). When nothing is stored, only the second part is shown.
- Broker authentication failures lead with an explanation and recovery step,
  not a process exit code. Keep the diagnostic reference, stage, reason, exit
  code, and elapsed time in a collapsed **Technical Details** disclosure. An
  unknown cause must remain explicitly unknown; do not imply consent was denied
  merely because a CLI process stopped.
- Broker pages with forms (the `/auth model` pages) have one form for one
  kind of input: an **Endpoint** list of known choices fills in the fields
  below it, and choosing "new" leaves them to type. Never repeat the same
  fields in a second form. Results follow **Errors and Results**: at the top
  of the form they came from, red `.status-error` for a failure, the form
  posting to its own `#id` so the page scrolls there. A form whose action
  waits on something slow (a live key test) disables its button, changes its
  label to **Testing…** with `aria-busy`, and shows a `role="status"` line
  saying what it is waiting for and the longest wait, so the page never looks
  idle while it works. Long-lived pages state how long they
  stay open instead of a countdown and have a **Close** button at the top and
  the bottom that ends the page.
- Standalone broker browser-authorization pages expose long provider URLs
  through a fixed-width action. Show a disabled **Wait...** button until the
  URL arrives, then enable **Open** in the same element to open a new tab.
  Under it, show a plain link to the same address ("If Open does not work,
  open or copy …"), hidden until the address arrives, so people can open it
  themselves or copy it (a script button offers no "copy link"). Short
  addresses such as GitHub's device page are the link text; long ones read
  "the sign-in link".
  Poll without reloading the pending page or rewriting unchanged status text;
  preserve focus, pasted callback text, and the surrounding layout. Keep raw
  CLI output and redirect troubleshooting in collapsed disclosures. Navigate
  once with GET when authorization finishes, including after a form POST.
- Apply the shared appearance script in the document head after theme styles,
  before rendering the body. Reapplying identical appearance settings must not
  restart the background or rewrite theme properties. A real theme change must
  still repaint, and Reset to Defaults must update preference state.
- Docs, generated sites, config editor, and broker pages keep a static mesh in
  both themes. Web Chat and onboarding retain their configured motion. Docs
  navigation should preserve the document and canvas through Material instant
  navigation, including when the website runs on a different port from the build.
- Keep visited Runtime Control tabs in memory for the current authenticated
  session. Restore their content and scroll position immediately, then refresh.
  Clicking the current tab must not blank it. Ignore late responses from another
  tab or session; clear retained content on sign-out. Settings should preserve
  its panels and reserve space for loading/status messages.

## Interface Motion

The owner asked for an interface that feels alive in the spirit of the Deus
Ex HUDs, "subtle and indicative of motion or being alive", never jarring
(2026-10-04). `ui_motion.js` (Web Chat and onboarding) and the styles under
"Interface motion" in `styles.css` implement it.

- Motion is slow, low-contrast and mostly plays once. Everything that moves
  stops when the system asks for reduced motion or when the person turns off
  **Interface Animation** in Settings > Appearance (saved per person as
  `interface_animation`; `appearance.js` sets `data-ui-motion` on the root).
  Content never depends on motion: without it the same elements appear at
  once.
- The accent for lines, brackets, sweeps and meshes is `--ua-motion-accent`.
  The owner chose "match the theme": in dark mode it is the hue of the dark
  theme's gold metal tint, raised to at least 62% lightness and 50%
  saturation because tints are usually too dark to read as lines; in light
  mode it is the hue of the person's first (blue) light, kept between 22%
  and 34% lightness so it reads on light glass (owner's request, 2026-10-09;
  it was a fixed ocean blue, `#075985`).
- Effects suit every color scheme, never only the default one (owner's
  request, 2026-10-09). Their glass is the interface's own frosted glass
  of the current color mode (`--ua-glass-base`, `-edge`, `-start`, `-end`,
  `-outline` in `styles.css`, beside the pane colors): the panes' teal with
  a cyan edge in dark mode, white glass with an ocean edge in light mode,
  so effects read as part of the interface and stand out on any background.
  A glass tinted like the person's background was tried and dropped: the
  intro word came out green on a green background and did not stand out
  (owner's decision, 2026-10-09). The person's theme comes in through the
  glints (`UAMotion.palette`), the light position of their dark background
  and the accent. Effects re-read the colors when the appearance changes, so
  a mode switch or a Customize Theme preview shows at once. `glass_mesh.js`
  holds the shared triangle mesh, facet shading and colors.
- Dark panels (Web Chat sidebar and conversation, the onboarding window)
  carry short corner brackets at the top-left and bottom-right in the
  accent, the angular frame of the HUD; popups carry them in both color
  modes. Panels assemble when they appear: a 240 ms fade-up while the
  brackets draw in.
- **Popup glass** (`panel_motion.js`, owner's request, 2026-10-09): every
  popup that is not a large window (setup-style `.setup-dialog` popups and
  native `<dialog>`s: setup guides, info, token enrollment, Share and Send,
  workflow details and review) closes by breaking into glass: the popup goes at once, as the
  page closes it, and its glass breaks into triangles above the page; each
  drifts a little, turns, and blurs (0 to 13 px over five levels) and fades
  at its own rate (0.45 to 0.9 s, starting within 0.12 s), while the
  backdrop fades out. Opening is the same run backwards: blurred, drifted
  triangles appear at their own times and come into focus in place to form
  the popup's glass (up to about 1 s), then the popup's surface and text
  return under it as the glass fades (0.22 s). An earlier opening, a small
  frame its corner brackets stretched to size, was dropped (owner's
  decision, 2026-10-09: the scaling did not look good). The glass shows
  the popup's glass, not its text: a page cannot copy an element's pixels
  (owner's choice). The popup takes focus, clicks and typing from the
  first moment. One watcher notices popups opening and closing however
  the page does it (adding or removing them, `hidden`, `showModal`/`close`),
  so new popups of either kind get the effect without changes; it remembers
  each open popup's place so the shards land where it was. A safety timer
  ends any effect shortly after its length if the browser stops delivering
  animation frames, so a popup never stays hidden. Triangle size scales with
  the popup (about 260 triangles). Tuning values are named at the top of
  `panel_motion.js`.
- **Large windows appear plainly** (owner's request, 2026-10-11: the glass
  did not look good on them): a popup that covers more than 60% of the
  browser window when it opens or closes gets no glass and appears and goes
  as it does without motion, with only the backdrop's short fade. On a
  laptop that is Settings; on a phone, where most popups fill the screen,
  it is most of them. The rule is a size share, not a list, so new large
  windows stay plain without changes.
- Never clip overflow to contain an effect. In a grid or flex container an
  item that clips its overflow loses its content-based minimum size, so the
  scrolling transcript squeezed a newly sent bubble to a sliver while its
  effect ran (2026-10-04). Effects stay inside their element instead: the
  arrival mesh fits the bubble and rounds its own corners, and the hover
  sweep moves its light band through its own background.
- Thread rows, Settings tabs and links, and New Chat get one faint light
  sweep on hover. The composer's accent line draws across when it gains
  focus. The active chat's edge glows very slowly (6 s).
- A message the person sends pops in over a small drifting triangle mesh
  that slows to a stop as it fades (about 2.2 s). The mesh uses the person's
  theme colors so it matches their background (owner's request, 2026-10-04):
  in dark mode mostly the metal color, with the gold and bronze tints at their
  shares of the plates, each kept in hue but raised to at least 58% lightness
  to show over dark panes (`UAAppearance.motionPalette`); in light mode the
  four light colors. A new reply fades
  in under one faint 3px scan line that crosses in about 1.2 s (it was 2px
  and 0.9 s, too quick and thin to notice; owner's request, 2026-10-10).
  Effects are keyed by the request id (sent) or message id (reply), so the
  transcript's re-renders continue an effect rather than restarting it, and
  only messages created in the last 15 seconds arrive; loading history never
  plays effects. The sent mesh's canvas moves to the re-rendered bubble with
  its drawing intact; a new canvas would be blank for a frame and make the
  mesh blink.
- **Team activity mesh** (`activity_mesh.mjs`): a small triangle mesh in the
  Web Chat sidebar between the chat list and the account row. Idle, it is
  calm, faint in the interface glass's edge color (it was a fixed grey until
  2026-10-09) and drifts slightly. When the team lead works, the middle
  triangle glows gold (`#f6c84c`); each busy worker lights a triangle of its
  own that it keeps while it works, in its type's color: blue for web
  research (`#62d5f3`), green for memory review (`#5ee08a`), violet for
  internal improvement (`#b18cff`), and a stable color derived from the name
  for other types; these stay fixed because they say which kind of agent
  is working. Lit triangles pulse gently and fade in and out over 0.7 s.
  Hovering one names the agent's class (e.g. WebResearchAgent), not its id;
  the canvas describes which agent classes are working for
  screen readers. It shows the whole team's work, as the Team tab does
  (owner's decision). Agents broadcast their state when work starts and ends.
  The mesh, Settings → Team and the Tasks view show one copy of the team's
  status, which arrives with each live update (about every two seconds,
  `team_activity.mjs`); none of them reads it on a timer of its own (owner's
  request 2026-10-04: one refresh, seen everywhere).
- **Onboarding intro**: opening onboarding before the person has gone past
  the Welcome step fills the background in (dark: the triangles fade in
  from the middle outward over 1.8 s; light: the background fades in). The title "Universal Agents"
  (owner's request, 2026-10-08; it was "Welcome!") is drawn on one line at
  `clamp(2rem, 7vw, 5.5rem)`, about half the earlier word's size because it is
  twice as long. It forms letter by letter in `intro_word.js` (owner's
  requests, 2026-10-04): a blurred glass slab of triangles in the interface
  glass appears where the word will be. Letter by letter from left to right
  (about 145 ms apart), the slab's triangles lift off and turn in 3D around axes of
  their own, softly blurred (3 px) while they turn, and come together into the
  letter as sharp facets of that glass, each tilted a little so it
  catches the light, with soft glints in the theme colors; triangles outside
  the letters turn away and fade. Once a letter has fully formed it
  crossfades (about 0.3 s) into the word style below and the chat bubble's flat
  drifting mesh in the theme colors plays inside it, slowing and fading over
  2.4 s, while later letters are still forming. The space between the words
  forms no letter. The last of the 15 letters forms after about 3.2 s and
  the word is complete after about 3.6 s (owner's request, 2026-10-09: about
  40% faster than before; `FORMING_SPEED` divides the forming times, and the
  Web Chat sign-in greeting forms at the same pace). From
  then until the intro is dismissed, a random letter that is not glowing
  flashes a new flat mesh (its own pattern and theme colors) every 0.5–1.1 s;
  each flash fades in gently over 0.8 s (0.25 s was jarring), then slows and fades over 3 s from its start, so about
  three or four letters always glow, each at a different stage (owner's
  request, 2026-10-08). (Earlier versions kept shimmering facets, crossfaded
  the whole word at the end, or dropped the turning facets; the owner wanted
  the turning facets back with each letter reaching the mesh as soon as it
  formed. Until 2026-10-08 the word then stayed still.) Without motion the
  finished word shows still, with no flashes. The 1 px outline filter traces
  each letter once it has formed; the facets of a letter still forming are
  drawn on their own canvas under the outlined one, because the filter
  strengthens faint edges and gave a forming letter a flickering rim
  (owner's report, 2026-10-11). The word stays text for screen readers. Tuning
  values (triangle spacing and tilt, slab and turning blur, letter step and
  spread, turn time, crossfade, flat mesh timing and spacing, flash timing,
  glint) are named at the top of `intro_word.js`. "Click to Begin" appears once the last letter is in
  and glows slowly; its border and glow use
  the word's outline color, and it has no separate focus ring (keyboard focus
  lightens its fill instead; owner's request, 2026-10-04). The word style,
  also used as text where the polygon word is unavailable: the interface
  glass, about 85% opaque, with a 1 px outline in its edge hue at 75%
  (`--ua-intro-word-fill` from `--ua-intro-word-fill-start`/`-end`, which the
  canvas paints too, and `--ua-intro-word-outline`). An SVG filter draws
  that outline around each letter's outer shape; `-webkit-text-stroke` must
  not be used, because it also traces the overlapping strokes inside heavy
  variable-font letters, which showed through as marks in the "e". A click,
  Enter, Space or Escape fades the intro out and the onboarding window in. It
  plays once per page load, never when returning with the browser's back
  button, and without motion the finished word shows at once. The browser
  remembers the last onboarding step and the furthest one reached; once a
  person has gone past Welcome, setup reopens their step without the intro,
  even if they went back to Welcome (owner's request, 2026-10-09).
  `?intro` in the address plays it on any step (for previews).
- **Web Chat sign-in greeting** (owner's request, 2026-10-08; until then
  the page showed a large "Enter your Web Chat access" panel and played the
  onboarding intro once per browser): every time the sign-in page shows (the
  page opens with nobody signed in, after signing out, after a session ends),
  the background fills in and a time-of-day greeting forms as the page
  heading with the onboarding title's effects: the same size, polygon
  forming, and random letter flashes that go on until sign-in
  (`UAMotion.playGreeting`, `intro_word.js`, `chat_sign_in.mjs`). The
  background fills in gradually from nothing, as on the onboarding Welcome
  page: the page first asks the server whether someone is still signed in,
  and while it waits `data-ua-background-hold` on `<html>` keeps the
  background empty (the dark mesh draws its empty, black frame itself;
  hiding its canvas instead showed a full frame for a moment once the fill
  began). Restoration has a five-second deadline; a failed or timed-out
  lookup logs a warning and opens the sign-in greeting. A resumed session shows the background at once. The
  greeting is "Good Morning!" from 5:00, "Good Afternoon!" from 12:00 and
  "Good Evening!" from 17:00 to 4:59 on the browser's clock. Once this browser
  has seen someone sign in it adds the first word of their display name:
  "Good Morning, Alice!". Only the browser remembers the name
  (`ua.webchat.greeting_name`): the server cannot know who is at the page
  before sign-in, and a lasting device cookie that told it would show anyone
  at the page whose device it is. Signing out keeps the name, the next person
  to sign in replaces it, and a session without a named person clears it.
  When the greeting does not fit on one line, the name moves to a second
  line, both lines centered (the polygon title takes `lines`), rather than
  the text shrinking. Resizing re-measures the font and room without
  replaying the greeting or hiding an open form. Turning motion off while
  it plays immediately shows the still word and form. Signing in removes
  the form entrance class synchronously so the next sign-in starts hidden.
  The logo (48 px) sits centered at the bottom of the
  page, below the window, and fades in with the greeting; beside the greeting
  it looked out of place once the name wrapped (owner's request, 2026-10-08).
  There is no "Click to Begin": a small floating sign-in window (546 px wide,
  30% wider than first built at the owner's request; the pane's frosted
  style) fades in under the greeting once it has formed, with the token field
  focused. It holds the field's name ("Web Chat token or invitation code"),
  one line on what to enter, the field with its show/hide button, errors in
  red, the button, and, when a name is remembered, a small "Not Alice?" link
  that forgets the name and greets again without it while the window stays
  open. The button names the page it opens: **Unlock Chat**, **Open Runtime
  Controls**, **Open Status**, **Open Knowledge** or **Open Workspace Files**. Any key or click
  shows the formed greeting and the window at once; Enter, Space and Escape
  do nothing else (so they never submit an empty form), and other keys reach
  the token field. Without motion the formed greeting and the window show at
  once. The onboarding intro keeps "Universal Agents".

## Layout

- The onboarding pane is a centered frosted application window over the mesh.
- It should be wide and tall enough for real forms, not a narrow wizard card.
- Use a left step rail on desktop and a compact top rail on mobile.
- Main content must either fill the available pane width or be intentionally
  centered when it is a compact control group.
- Do not bunch active controls against the left edge of wide panes.
- Keep page headings and subheaders aligned to the main content margin.
- Long onboarding tabs must use one top-anchored tab scroll pane; expanded
  content must never vertically center the full tab body or make earlier fields
  unreachable.
- Embedded scroll regions are acceptable for long accordion pages such as
  Communication.

## Typography

- Font family is Inter with system-ui fallbacks.
- Heading text should be large, strong, and highly legible.
- Headings use Title Case, except small connecting words such as `a`, `the`,
  `and`, `or`, `to`, `of`, `for`, `with`, and `in`.
- Do not end headings with a period.
- Subheaders should use the full content width and be darker than placeholder
  gray. Use `--muted` or stronger.
- Avoid negative letter spacing.
- Reserve hero-scale type for true page headings. Compact cards, controls, and
  accordions use smaller tight labels.

## Frosted Surfaces

- Functional UI sits on frosted glass surfaces:
  - translucent white or pale green-white fill in light mode,
  - translucent charcoal and near-black fill in dark mode,
  - visible but gentle border,
  - blur around 22px to 24px,
  - saturate around 140%,
  - shadow only for main panes and modal overlays.
- Use cards only for repeated items, modals, accordions, and control groups.
- Do not nest decorative cards inside decorative cards.
- UI boxes should maintain clear text contrast over either background without
  becoming opaque slabs.
- Inputs use the same frosted vocabulary but slightly stronger fill than parent
  cards.
- Dark functional surfaces use the **Deep Ocean + Ember** hierarchy. Use
  `#070b0e` for the UI canvas, `#0b151b` for panes, `#10232d` for controls,
  and `#173440` for raised, hovered, or selected controls. These blue-black
  levels replace medium gray slabs and make elevation legible without obscuring
  the metallic background.
- Dark Inbox, Access, Team, and Memories lists, tables, empty states, and detail
  cards use those same pane, control, and raised levels. Never carry their
  translucent light-gray fills into dark mode.
- Use `#367287` for interactive borders and `#62d5f3` for focus and link
  emphasis. Ordinary dark-theme text is `#f2f7f8`; supporting text is
  `#adc1c8`. Do not reduce the opacity of an entire control to communicate a
  disabled or locked state because doing so also destroys label contrast.

## Popups and Information Buttons

- Information affordances use a small circular `i` icon.
- Note copy should remain as always-visible frosted text fields rather than
  icon-only hover reveals.
- Info popups and setup walkthroughs must use frosted windows matching the main
  design:
  - translucent white gradient,
  - ocean-blue title text,
  - readable muted body text,
  - compact radius,
  - same blur/saturation as other overlays.
- Hover popups are small, positioned near the icon, and non-interactive.
- Click popups may open centered modal overlays with a close button.
- Avoid native browser `title` tooltips for important information; they do not
  match the product style and are not accessible enough.

## Errors and Results

Every surface (Web Chat, Settings, the Inbox, onboarding, broker pages and
generated pages) shows the outcome of an action the same way, so a person
never misses a failure or has to hunt for it (owner's requirement
2026-10-02, after an error on the `/auth model` page appeared as plain text
at the top while the person was scrolled down to the form they used).

Placement:

- Show a result at the top of the part the person was working in: the form,
  card, table row, dialog or step that sent the action. Never only at the top
  of the page, in a corner toast, or in the browser console.
- Bring it into view. After a full-page form submission, the form posts to
  its own fragment (`action="#<form-id>"`) so the browser lands on the result.
  In dynamic pages, write the result into that part's message line and move
  focus to it only when it would otherwise be out of view.
- One message per part. A new attempt replaces the previous result; stale
  errors from earlier attempts never pile up.
- Problems that block the whole page (a store that cannot be read, a service
  that does not answer) go at the top of the page in the red callout, because
  no single part owns them.

Appearance:

- A failure is red: the theme's danger color (`#b91c1c` light, `#b94850`
  dark) as a left border on a lightly tinted background, with text dark or pale
  enough to read in both themes. Use the shared classes: `.status-error` on
  broker pages, `warning-callout` in Web Chat and onboarding, and the card or
  row message line where one exists. Mark it `role="alert"`.
- A success is the neutral `.status` style with `role="status"`; never red or
  amber.
- Never rely on color alone: the words say that something failed
  ("Nothing was saved.", "The request did not run.").

Content, in this order:

1. What did not happen ("Nothing was saved.").
2. The most likely cause in plain words ("The provider refused the key.",
   "Nothing at this address accepts model requests."). An unknown cause stays
   explicitly unknown; never guess one.
3. What to do next, with the concrete step, command or setting to change.
4. The service's or provider's own answer, when it helps, quoted after the
   explanation.
5. Codes, stages, exception types and digests in a collapsed **Technical
   Details** element, never as the main text.

Never show secrets, keys or tokens in a result, and never a raw stack trace.

Behavior:

- After a failure, the form keeps everything the person typed, except secret
  fields, so they can correct one value and try again.
- Results stay until the person's next action in that part; they never fade
  out on a timer.
- Busy states disable the action that is running (see Controls), so a result
  always belongs to one attempt.

## Controls

- When holding a modifier key enables or changes an action, highlight the
  relevant controls or show a contextual hint while the modifier is held.
  Use the platform's modifier convention and make the available action clear.
  Clear transient feedback on key release, window blur, or when the page
  becomes hidden so switching apps cannot leave stale hints active.
- Primary buttons use a rich ocean-blue gradient and white text.
- In dark mode, ordinary actions use Deep Ocean `#087697` with white text.
  Secondary actions use a `#10232d` or `#173440` surface with a visible
  `#367287` border. Ember `#f0a35a` is complementary emphasis for active
  markers, slider thumbs, values, and hover/focus edges; it is not a generic
  replacement for every button.
- Destructive dark-mode actions use deep crimson `#b94850` with pale text.
  Keep warning, destructive, selected, disabled, and ordinary action states
  semantically distinct rather than communicating them with one gray fill.
- Launch buttons may use green only for the final irreversible launch action.
- Back/secondary buttons need a visible border so they remain legible over the
  background.
- Progress bars use a light-blue to ocean-blue fill, with a slightly darker
  neutral track.
- Toggle `on` state uses ocean blue. Toggle `off` state uses neutral gray.
- Sliders use one visual system everywhere:
  - light-blue to ocean-blue filled range,
  - neutral track,
  - circular knobs centered on the track,
  - knobs render above the filled range.
- Dark sliders use a `#263e48` track, a Deep Ocean-to-blue fill, and an Ember
  thumb with a dark outline. Their enclosing control uses `#10232d`, labels use
  high-contrast text, and values may use Ember. Disabled sliders replace the
  thumb and label treatment explicitly instead of fading the whole pane.
- Color selectors are wide rectangular swatches, centered within their border.
- Secret inputs must include reveal/hide controls: one inline icon button with
  an eye icon inside the field's right edge (34x32px, 6px corners, labelled
  "Show or hide value", `aria-pressed` tracks the state). Onboarding, Web Chat
  settings and every broker page (credential collection, account links and
  login fields) use this same button; in dark mode it has the `#10232d` surface
  and `#367287` border, with an Ember edge on hover and keyboard focus. Start
  masked, preserve the entered value, and never submit the form when toggling.
- Generated-token inputs should include inline icon buttons for generate/copy
  when possible, without disrupting field alignment.
- A mutation button that waits for server confirmation must enter a visible
  busy state on the first click. Disable that button and every mutually
  exclusive action for the same resource, set `aria-busy=true`, and show a
  compact progress ring beside a specific present-participle label such as
  **Approving…**, **Saving…**, or **Removing…**. Keep the control width and
  surrounding layout stable. On failure, restore the exact prior enabled state;
  on success, replace the affected view or keep an intentionally terminal
  action disabled. Reduced-motion mode keeps the textual state and a static
  ring rather than rotating it.

## Attention counts

Anything waiting for the person shows how many items wait, on every control
that leads to it (owner's request 2026-10-12).

- A tab, rail link or button that opens items waiting for this person carries
  a count badge with the exact number. This covers things to decide or act on,
  such as access requests they can decide or proposals waiting for them, and
  things they have not seen yet, such as unread notices. At zero the badge is
  hidden: never show "0" or a dot without a number.
- Count only what waits for this person. Totals that are only information
  (list sizes, page counts) stay in the list's own text, never in a badge.
- Every step on the way shows the count: the tab that lists the items, the
  Settings rail link that opens their page, and the Settings cog. The cog shows
  the sum of everything inside Settings. Current counts:

  | Count | Shown on |
  |---|---|
  | Requests to decide plus unread notices | **Inbox** tab, cog |
  | Proposals waiting for the person | **My proposals** tab on the Knowledge page, **Knowledge** link, cog |

- Tabs and links show a round `settings-tab-count` pill after the label: ocean
  with white text in light mode, light cyan with dark text in dark mode. The cog
  shows the attention-yellow pill on its corner. A badge never changes the
  height of its tab or link, and never moves the label off its neighbours' line.
- The badge's tooltip and the control's accessible name say what the number
  counts ("Knowledge: 2 proposals waiting for you"); the cog's name lists each
  part.
- Counts come from the server for the signed-in person through a small count
  read, never by loading the list. They are read once at sign-in, every 30
  seconds while the page is visible, and once when it becomes visible again.
  They are also updated right after the person's own action, or from a list
  read that already returned the total (`settings_support.mjs`
  `createAttentionCount`). A failed read keeps the last number; sign-out
  clears it. A count read never creates anything: with no conversation open,
  it waits rather than starting one.
- When a new kind of item can wait for a person, add its count in the same
  change, on every step that leads to it.

## Settings

- Settings opens above chat from the cog. Desktop uses a left primary tab rail
  with **Inbox**, **Access**, **Accounts**, **Team**, **Tasks**, **Memories**,
  and **Appearance**, in that order; narrow screens show the same choices as a
  horizontal tab row. The **Inbox** tab carries a badge with the exact number of
  requests this person can decide plus their unread notices, and the
  **Knowledge** link one with the proposals waiting for them. The cog in the
  Web Chat sidebar carries the sum of both as a small attention-yellow pill on
  its top-right corner, kept current while signed in, so people see waiting
  items without opening Settings (owner's requests 2026-10-04 and 2026-10-12;
  see **Attention counts**); the cog's accessible name says what each part
  counts. Below the tabs sit **Chat Alerts**, a label
  with the standard slider switch (on or off; text appears under it only to
  explain a problem, such as alerts blocked by the browser), and the page links
  **Workspace**, **Knowledge** (while the knowledge base is on), **Runtime**
  and, for administrators and owners only, **Status** (short names, owner's
  request 2026-10-04), kept together at the bottom of the rail with **Log out**
  last.
  The links open new windows on the signed-in Web Chat origin. No Collector token, broker token, or credential
  material belongs in either link or browser state.
- Every signed-in person sees the **Workspace** link. Members get **Your
  files**, which shows only their own folder. Administrators get **Workspace**,
  which shows `team/`, `agents/`, and their own folder (see Workspace Files).
- Inbox lists requests and notices about access, Access shows who can use what,
  Accounts shows whose account agents use for each service, Team shows the live team and installed procedures, Tasks shows the person's
  work, Memories manages durable personal and team facts, and Appearance
  controls the signed-in user's color theme and both theme presets.
- The cog reopens the last selected Settings tab and secondary tab in the current
  signed-in page session. The initial tab is **Inbox**. Sign-out resets this
  selection. Links choose their own destination:
  `/webchat?view=inbox#access_request=<id>` opens the Inbox at one request, and
  `/webchat?view=access#agent=<type>` opens the Access page at one agent type.
  Web Chat reads the link before sign-in and puts plain `/webchat` back in the
  address, so a reload stays on the chat; it applies the link after the person
  passes the normal checks. Opening a tab or following a link never
  decides or changes anything.
- Tasks includes a **Finished tasks** section across all agents on the team,
  including stopped agents. Show only the signed-in user's archive, newest first,
  with 10 records per page. Each row shows the task description, exact task and
  agent IDs, agent name, recorded status and finish time. **View result** expands
  the recorded answer as text. Finished rows have no cancellation controls.
  Keep history pagination independent from each active agent's queue.
  Place it below unfinished work, collapsed by default. Load and refresh history
  only while expanded; preserve its open state while refreshing the team.
- Tasks groups the signed-in user's unfinished work by running agent across
  conversations. Label each section with the agent name, class and exact ID.
  Put TeamArchitect groups first, retaining the existing order of other agents.
  Show current work first, followed by queued and waiting tasks, with descriptions,
  IDs, states, queue positions and waiting reasons. Page each agent at 20 items and refresh
  every 10 seconds only while the panel is active. Retain the visible page during
  refresh and reject late responses from a closed panel or previous session.
  Accepted requests must appear even while the agent is busy or paused.
  Append **Parallel** to the current, queued or waiting heading of a request
  sent in a thread started with **Launch parallel thread**. The agent runs such
  a request at the same time as its other work, up to four at once; a running
  one shows as current, not queued.
  When a task waits, show its reason as **Waiting on:** followed by the reason
  text. Do not add a request ID or claim that a request waits in the Inbox
  merely because the task is waiting.
  Limit description previews to 280 characters and waiting/activity previews to
  240 characters, with at most three visible lines per long preview. Offer a
  keyboard-accessible disclosure for the full received text. Keep expanded text
  and task results in scroll areas at most 16rem high, and preserve disclosures
  across refreshes. Render all task text as text, including embedded markup.
- Use **Remove** for queued tasks and **Stop and remove** for current work.
  **Purge this queue** includes that agent's queued and waiting tasks and preserves current work.
  Put each removal confirmation inside the selected task row, beside its work,
  so focusing Confirm does not jump to the top of the list. Keep purge confirmation
  beside the queue controls. Cancel restores focus to the originating button.
  Require a confirmation naming the task or queue count. Explain that
  cancellation history remains and recurring schedules are unchanged. Bind the
  mutation to the displayed inventory; a changed queue requires a fresh review.
  Keep partial or unconfirmed worker-stop results visible. Use the shared busy
  buttons and destructive colors, and suspend automatic refresh during confirmation.
- When a removal response times out, say the result is unconfirmed. Clear its
  confirmation, fetch the current task state, and resume polling. Never retry a
  destructive request automatically. Preserve the outcome message when a purge
  shrinks the list to an earlier page; a refresh failure must label the old list
  as potentially outdated.
- Team shows running members and a separate **Defined agent classes** table with
  names and descriptions, including classes with no running instance. Catalog
  text is rendered as text. Task paging, refresh errors and mutation
  confirmations remain independent per agent. An unavailable agent must not
  replace another agent's visible work.
- Team then shows **Installed procedures**, for administrators only. An installed
  procedure is a fixed operation a developer built into the runtime. Agents may
  propose using one, and administrators **Add** or **Reject** each proposal
  here. Explain that adding one gives no agent access by itself: each use is
  checked like any other tool. Other people see "Only administrators review
  procedure proposals."
- A proposal card's heading is the display name from the runtime's registry, or
  **Procedure not built into the runtime** when the registry has no match. A
  status chip reads **Not built into the runtime yet**, **Ready for review**,
  **Added**, or **Rejected**. The details list shows **What it does**,
  **Effect**, and **Risk** from the registry first, then **Built into the
  runtime**, **Name the agent gave**, and **Proposed**. The agent's purpose and
  justification appear under **Why (from <agent>)**. Keep the proposal ID,
  requested handler, access level, and digests in a collapsed **Technical
  details** section.
- Show **Add** (primary) only when the gateway says the built-in code and the
  registry entry match. **Reject** (ghost) is available on every open proposal.
  A proposal waiting for code is an inert waiting state, not a warning. The
  browser sends only the proposal id; it never sends or edits a handler id,
  schema, risk, effect text, or digest. Closed proposals show no buttons.
- Added procedures render as a compact table with **Procedure**, **Risk**,
  **Team lead may pass it on**, **Built-in code** (**Matches the runtime** or
  **Does not match the runtime**), **Added**, and **Definition ID**.
- Team ends with **File Retention**: how many days chat uploads and scratch
  files are kept, where 0 keeps them forever. When the gateway says the person
  may change them (`can_change`), show one row with **Days to keep chat
  uploads**, **Days to keep scratch files** and **Save** (primary; **Saving…**
  while busy). Everyone else sees the two values as a short list ("90 days",
  "Kept forever") and "Only administrators change how long files are kept."
  After saving, say that chat uploads follow the new number at the next
  cleanup, within six hours, and scratch files after the next restart. A
  failure keeps the typed numbers.
- Browser alerts are opt-in from Settings. Ask for browser permission only on a click;
  explain HTTPS/browser requirements or a denied permission in place. Store the
  preference by signed-in user and browser. Use generic alert text without message
  bodies or request details. Clicking an alert focuses its conversation. Sign-out
  closes outstanding alerts and invalidates their callbacks and pending prompts.
  The tab title counts unread conversations and distinguishes attention requests.
  A selected conversation remains unread while the document is hidden, unfocused,
  or covered by Settings. Event replay must not create duplicate alerts.
- Appearance keeps a compact **Color Theme** picker visible. Switching themes
  applies the selected background immediately. **Customize Theme** temporarily
  hides the main interface and opens a narrow control panel at the left edge so
  the live background remains the dominant preview. Only the selected theme's
  controls appear in that panel. Edits only show while it is open: **Done**
  saves them, and **Cancel**, the close button and Escape put the opening
  appearance back without saving. The owner asked for this (2026-10-03) after
  trying settings that were saved without being wanted and could not be
  undone. In Web Chat the edits show as a preview above the saved preference,
  so a preference reload cannot overwrite them; Done closes the panel only
  once the server has saved, and a failed save keeps the panel open with the
  error in red above its buttons. The footer gives **Regenerate Mesh** its own
  full-width row with **Cancel** and **Done** side by side below it; three
  buttons in one row overlapped in the narrow panel (2026-10-04).
- **Share settings**, at the end of the panel, shows the theme as YAML that
  fits under `ui.onboarding.appearance` in `instance_config.yaml`, with **Copy**
  and **Apply**. Copy uses the clipboard on secure pages (https or localhost)
  and otherwise selects the text with a note to copy it by hand. Apply reads
  pasted text (that YAML, JSON, or a piece of the config file around the
  appearance section, or a dark section on its own) and previews it; Done
  still saves. The background lab shows the same text from the same code
  (`UAAppearance.settingsText`), so settings copy between the lab, Web Chat
  and onboarding; the owner hit JSON in one and YAML in the other
  (2026-10-04). Accents come last, one line each (`gold: {color: …, share:
  …}`) as in `config.example.yaml`, and the header says to move every line
  right together under `appearance:`: a paste with unevenly indented accent
  lines stopped startup (2026-10-04). Startup now names the line and column
  of a broken `instance_config.yaml` instead of printing a parser traceback.
  Share settings exists because the
  owner tuned a theme in the preview and had no way to keep the values
  (2026-10-04).
- Both theme presets remain saved even though only one editor is visible. Every
  color selector displays its normalized `#RRGGBB` value beside its label.
  A speed control is disabled while its motion is off. Both themes expose
  **Regenerate Mesh**. The dark theme also offers **Background detail**
  (**Full** or **Basic**).
- Memories uses compact secondary tabs for **Personal** and **Team**. Render
  active facts as a readable list with tags, a local-time update label, optional
  expiration, and actions aligned to the right. Personal facts expose **Edit**
  and **Delete** to their owner. Team facts remain readable to active team
  members, but only administrators receive mutation actions from the gateway;
  the browser must not infer administrator access from client state.
  Order both lists by last update, newest first, with descending memory ID as
  the stable tie-breaker. Apply this order before pagination so edits return to
  the first page.
- Place **New** beside **Refresh** in Memories. Use the same inline editor for
  creation and editing, with memory text, tags, expiration, Save and Cancel.
  Show New only when the gateway returns creation permission for the selected
  category, including an empty category. Keep entered values after a failed save.
- Memories, each Access view, the Tasks queues, procedure proposals, and the
  workflow list and activity show at most 20 items per page. They use the shared
  `pagination.js` controls: first/previous/next/last arrow icons and up to five
  clickable page numbers around the current page, with ellipses for omitted pages.
  Do not show word labels or a “Page X of Y” sentence. Give icon buttons accessible
  names, mark the selected number with `aria-current="page"`, keep visible keyboard
  focus, disable unavailable directions, and block repeat clicks during a page
  request. Allow controls to wrap on narrow screens without horizontal scrolling;
  replace the page instead of accumulating cards. Reset to page one when the
  user changes a tab or filter. Apply status filters and current-user visibility
  before selecting a page, and enforce the fetch bound on the server. Totals come
  from the server after ownership and status filtering. Counts describe the
  current read, not a frozen snapshot of a changing collection.
- Inbox requests are not paged: it lists every waiting request the person can
  decide or that belongs to their own work. Notices are paged, 20 at a time with
  the shared `pagination.js` controls, because they pile up (owner's request
  2026-10-04). The badge counts requests the person can decide plus unread
  notices.
- During list reads, replace the Refresh button's visible label with a rotating
  circular indicator while preserving its width and accessible name. Disable
  the button and set `aria-busy`; restore it on success or failure. Reduced-motion
  users see a static indicator. Do not show transient Loading/Refreshing status
  lines or reserve empty space for them. Keep errors and action results visible.
- Memory editing stays inline in the selected list item. It supports fact text,
  comma-separated tags, and an optional local expiration. A blank expiration
  means the fact remains active until changed or deleted. Saving and deletion
  are bound to the gateway-supplied record digest so a stale browser cannot
  overwrite a newer fact. Conflict copy should ask the user to refresh.
- Memory deletion uses a two-step confirmation on the same button. Successful
  deletion may explain that an audit event remains available for recovery;
  avoid a second modal for this compact reversible operation.
- Settings keeps a stable responsive dialog height while it is open. Automatic
  Team refreshes retain the last successful tables, update unchanged rows in
  place, and atomically replace changed table content so loading, transient
  errors, and scrollbar changes do not make the dialog or tables jump.

## Inbox

- Settings → **Inbox** holds requests from agents and people, and notices about
  access. Its header has **Open Workflows** (the person's workflow list) and
  **Refresh**. The panel has two parts: **Access requests**, introduced with a
  sentence saying that nothing is approved until someone chooses, and
  **Notices**. Opening a request never approves it.
- Every card uses one layout: a title sentence with a status badge on the right,
  a details list of label and value rows, optional sections, the decision area,
  and a message line for results and errors. Render all server and agent text
  as text, never as HTML.
- There are four kinds of request card: access requests, proposals, delivery
  requests, and staged changes. The badge reads **Needs your decision** when this
  person can decide, or **Waiting for <who>** naming the person or role who can,
  such as **Waiting for an administrator**. A card waiting for someone else has
  no buttons.
- Dark Inbox and Access cards use the same Deep Ocean hierarchy as other
  controls. Details and reason blocks use blue-black fills, grant rows use the
  raised control color, Ember marks impact and selection, and **Deny** keeps the
  dark crimson action color.

### Access Request Cards

- The title says in plain words who asks for what, such as `WebResearchAgent
  asks for read access to Google Workspace`. When an agent asks on behalf of
  another agent, name both: `TeamLead asks to give WebResearchAgent read access
  to Google Workspace`.
- The details list shows, in this order:
  - **Tool or service**: the tool's name, with its catalog description beneath
    it in supporting text. Code tools must say what they allow; for example,
    `code:unconfined` runs code outside the sandbox with the runtime's own
    rights.
  - **Access**: the level in words: **read**, **read and change**, or **full**.
  - **For whose work**: **your work** or **<name>'s work**.
  - **How long**: for example **for this task** or **until someone removes
    it**, with the local end time when there is one.
  - **Asked**: the local date and time.
- **What this changes** follows as a short list of plain effects.
- When the person the task works for lacks the access too, show a warning line
  with the warning icon. The person sees "You don't have this access either;
  approving gives it to you as well." An administrator deciding for someone else
  sees "<name> doesn't have this access; approving gives it to them too."
- **Why (from <agent>)** shows the agent's reason, labeled as the agent's words.
  It may explain the request but never defines the effect, labels, or buttons.
- Each grant the request would create is its own row with the target, the level,
  and a **Keep for later tasks** checkbox (only when the agent asked to keep the
  access), ticked to start so approving as shown gives what was asked. Its helper text is
  written by the gateway and follows "access follows the work": on TeamLead's
  own request "Off: only for this task. On: TeamArchitect keeps it whenever it
  works for you, until it is removed."; on a worker's request "... On:
  TeamArchitect keeps it for you and gives it to <AgentType> and its other
  workers in later tasks, until it is removed."; in a scheduled run "Off: only
  for this run. On: this scheduled job keeps it for its later runs ...". When
  the task works for someone else, the text names that person instead of
  "you". When a request has no task, the row says the access lasts until
  someone removes it and shows no checkbox.
- Next to the Keep checkbox, a **How long** select (disabled until Keep is
  ticked) offers **Until I remove it** (the default), **For 1 day**, **For 7
  days** and **For 30 days**, so a person can give access for this task, for a
  while, or permanently.
- On TeamLead's own requests the row has a second checkbox, **TeamLead may hand
  this on to workers without asking you**, ticked from the start, because
  workers get access only from TeamLead. It is how a person delegates authority
  to TeamLead for their own work; lasting hand-downs appear as notices, access
  added to one worker task does not.
- The decision area has a **Note (optional)** field and the buttons
  **Approve** (primary), **Approve reading only** (secondary, shown only when
  more than read access was asked), and **Deny** (danger). Each grant's Keep
  setting, and the hand-on box when present, is sent with the decision.
- **What each choice does** follows the buttons, always visible in both the
  Inbox and the compact Web Chat card: one line per button, "<Label>: <what it
  does>", written by the gateway (`assistant_gateway/access_choice_effects.py`)
  from the request's own fields, never from the agent's reason. The lines say
  who ends up holding what, for how long, whether TeamLead may hand it on, and
  what Deny does, and change as the Keep and hand-on boxes change. Each button
  points to its line (`aria-describedby`); hovering or focusing a button marks
  the line with the Ember edge. No native `title` tooltip.
- **What this changes** includes what approving does to access the holder
  already has on the same tool, such as "TeamArchitect now has read access to
  Schedules until someone removes it. Approving replaces it." or "... keeps its
  read access ...; approving adds this beside it."

### Proposal Cards

- A proposal is a set of changes the team lead proposes at once. The title names
  the proposing agent and what the changes are for.
- Sections appear in this order, each only when it has items. Each item is one
  row that describes itself in plain words:
  1. **New tools**: name, host, and what each access level allows.
  2. **Access**: who gets it → what it reaches, the level, and whether it can be
     handed on. Each row has its own **Keep for later tasks** checkbox, as on
     access request cards.
  3. **Restrictions**: the limits the proposal adds.
  4. **Folders to register**: each folder by name.
  5. **Credentials to link**: each tool that needs an account, with "After
     approval you choose the credential for <tool>."
- Items that broaden access carry a warning chip on their row: **New host**,
  **Private address**, **Full access**, **Can hand on**, or **Runs code**. Chips
  use the amber warning treatment and always include their text.
- The decision area has the optional note, **Approve all** (primary), and
  **Deny** (danger).
- After approval, the collapsed card keeps a **Choose the credential** link
  button for each account link in the proposal. It opens the broker's account
  link form in a new tab; the credential is chosen only there.

### Delivery Request Cards

- When a person sends a file into another person's folder, the receiver sees
  "<name> wants to put <file> into your folder". An administrator deciding for
  the receiver sees the receiver's name instead of "your".
- Details: **Size**, **Destination** (`received/<sender>/`), and **Copy or
  move**. Buttons: **Approve** (primary) and **Deny** (danger). The file arrives
  only after approval.

### Staged Change Cards

- A staged platform change, such as a settings change, restart, attachment,
  specialist, or team skill, reads "<agent> asks to make a change: <title>". It
  shows **Asked**, the **What this changes** list, and **Why (from <agent>)**,
  with **Approve** and **Deny**. Approving approves exactly the plan shown.
- Chat announcements and the Web Chat card lead with the change's one-line
  summary instead of the title (see Access Requests in Chat Channels); the
  Inbox card keeps the title and the full list.

### Decisions, Links, and Notices

- A decision button uses the shared busy state (**Approving…**, **Denying…**)
  and disables the other buttons on the card. After a saved decision, Settings
  stays open, the card collapses to one line with the outcome, such as
  **Approved for this task**, **Approved reading only**, or **Denied**, and the
  list refreshes. Decided lines stay until the next Inbox open or Refresh. A
  request whose task ended first reads **Withdrawn**. On failure, keep the card
  and the note, and show the error on the card's message line.
- The gateway decides which choices a card offers and binds each button to its
  outcome with a stable choice key, not its label. The browser sends back only
  the chosen key, each grant's Keep setting, and the note. Agent text never
  supplies labels or choices. Browser writes need the signed session and the
  same-origin CSRF token.
- An Inbox link highlights and focuses its card. When the request is no longer
  waiting for this person, say "That access request is not waiting for you any
  more. It may already be decided, or only someone else can decide it."
- The empty state describes this person's view, not the whole team's: "Nothing
  is waiting for your approval. Requests for other people's tasks that you
  cannot decide are not shown here."
- **Notices** are updates about access changes and work that needs attention,
  such as an agent handing its access on or a schedule that is not running.
  They need no decision and never show as browser alerts. The **Notices**
  heading carries an "<n> unread" chip. Below it an **Unread** / **All** tab
  row (Memories' tab style) chooses the list; it opens on **Unread**, so read
  notices leave the default view. A toolbar holds **Select all on this page**
  (a checkbox), **Mark selected as read (<n>)** (secondary, disabled with
  nothing ticked) and **Mark all as read** (ghost, two-step **Confirm: mark all
  as read**; it covers every page). Each unread row starts with a checkbox left
  of its title, then shows a **New** badge, a summary, and the local time, with
  **Mark as read** (secondary) and, for a notice about a grant, **Remove
  access** (danger) with the two-step **Confirm removal**. Removed access shows
  an **Access removed** badge. After marking, the page re-reads; a page left
  empty steps back one page, and a line under the toolbar says how many were
  marked.
- With the default `authorization.presentation.mode: friendly`, user-facing
  access copy says **access**, **for this task**, **keep for later tasks**,
  **hand on**, **block**, and **remove access**. Reserve grant, lease, attach,
  consumed, invoke, revoke, elevation, scope, policy, TTL, and digest for code,
  APIs, logs, or collapsed technical details. With `mode: technical`, also show
  exact tool ids, levels, lifetimes, and identifiers, and open technical details
  by default. Keep the plain decision labels in both modes.

## Accounts Page

- Settings → **Accounts** (owner's request, 2026-10-11) shows what the
  `/accounts` chat command lists, from the same rows on the gateway, so the two
  always agree: the model keys first, then every service that acts with an
  account. It is read only; administrators change sharing in chat
  (`/accounts share|private`), and the line under the table says so (members
  read "Owners and administrators change this in chat with `/accounts`.").
- One table with **Service** (the name in bold, its tool id as code under it),
  **Sharing** (**Shared**, **Private**, **Always shared** or **Always
  private**, with a muted line where the word needs explaining), **Team
  account** (**Linked**, **Not linked**, or **Linked, not used while private**)
  and **Your own** (**Linked**, or what to do with the command as code under
  it, such as "Not linked: link it with" `/link http:exa`). A dash marks an
  account the service does not take. Narrow screens turn rows into labeled
  cards, as the other team tables do. **Refresh** reads it again.

## Access Page

- Settings → **Access** shows who can use which tools, where that access came
  from, and what is blocked. Opening it never changes access. Its compact
  secondary tabs are **By Agent Type**, **By Person**, **By Tool**, and
  **Files**; switching tabs replaces the content in place. Each view pages at 20
  items.
- With authorization off, replace the tabs with **Access Control Is Off** and
  the gateway's explanation, followed by the **Data folders** section with the
  same list and administrator controls as on the **Files** tab. Its note says
  every agent can use the folders at the access shown. There are no shares
  without access control, so the other Files sections are left out.
- **By Agent Type** opens with **TeamLead's standing access for your work**:
  the starter profile choices as radio buttons, each with its description, the
  one in use marked **In use**, and **Save**, which applies the choice to the
  signed-in person's own work only and reloads the view.
- **By Agent Type** shows one card per agent type, with its role as a badge,
  and one card per scheduled job that holds access, titled **Scheduled job
  <name>** (its id when it has no name), after the agent types. Access follows
  the work: a worker's rows are access its running tasks got from TeamLead;
  lasting access for a person's work held by a worker from before that rule is
  not listed. Each group lists one row per tool (an entry). Groups appear in
  this order:
  - **Standard for its role**: access that comes with the role. Rows show the
    target and level and have no buttons. A note says a block can stop it.
  - **For your work**: access that serves the signed-in person's tasks, one row
    per tool. Each row shows **Access** (the main level and any step-up for one
    task, such as "read-and-change, and full while one task runs"), **How
    long**, **Handing on**, **Came from** (a chain such as "You → TeamArchitect
    → WebResearchAgent"), **Why**, and a **Note** when an administrator also
    gave the same tool for everyone's work ("Removing it here does not remove
    that."), with a **Remove** button that removes the whole row.
  - Administrators also see **For <name>'s work**, one group per person, and
    **For everyone's work**, with the same rows and **Remove**.
  - **Blocks**: each block with **How long**, **Blocked by**, **Added**, and
    **Why**, and a **Remove block** button.
- Members see each agent's standard access and the access that serves their own
  work, never access that serves other people.
- **Remove** and **Remove block** use a two-step confirmation on the same button
  and the shared busy state. The first click says "Select Confirm removal to
  remove all of this access. Access handed on from it is removed too." Removing
  access removes the whole row and what was handed on from it; the status line
  counts only what was handed on as "more", and says the change applies from
  the next action. Rows still waiting for a decision show a **Waiting for
  approval** badge and no Remove button.
- **Add a block** opens the inline Block form in the card: **Who** (this agent
  type, or, for administrators, **Everyone in the team**), **Tool** (a list; for
  administrators it starts with whole tool sets such as **Internet access (all
  of it)**, which is how internet access is turned off),
  **Operation (optional)** (empty blocks the whole tool), **How long** (**Until
  someone removes it**, **1 hour**, **1 day**, **7 days**, or **30 days**), and
  **Why** (required, because people read it), with **Block** (danger) and
  **Cancel**. Entered values stay after a failed submission.
- **By Person** lists what each person has given, and to whom, including the
  blocks they added. Members see only their own.
- **By Tool** shows each tool with its description and kind, then **Tool ID**,
  **Account**, **Tool sets**, **Operations**, and how many operations are not
  reviewed yet (those need full access). **Accounts** names only who owns each
  linked account and who linked it, never its details. **Show operations**
  reveals a paged list of operations and the level each one needs.
- **Files** has these tables; narrow screens turn their rows into labeled cards:
  - **Shared by you**: **Item**, **Person**, **Level**, **Until**, and **Stop
    sharing** (two-step).
  - **Shared with you**: **Item**, **Owner**, and **Level**.
  - **Data folders**: **Name**, **Path**, and **Read-only** or **Writable**.
    Everyone sees the list; members also see "Only administrators add or remove
    data folders." Administrators get **Remove** on each row and an **Add
    folder** form below the table (a name, an absolute path, and a
    **Read-only** toggle that starts on). With authorization off the Access page
    shows only its notice; data folders are then managed with `/access folders`.
- An Access link (`#agent=<type>`) opens By Agent Type at that card, highlighted
  and focused. When nothing is listed for that type, say "No access or blocks
  are listed for <type>."

## Status

- `/webchat?view=status` is **Status**: what the framework flags to
  administrators and owners about itself, now. Only they see its rail link
  (the gateway answers `/api/web-chat/status/access`) and only they can read
  it; a member who opens the address sees one plain sentence saying so.
- It uses the Workspace Files frame (brand lockup, kicker and title
  **Status**, a one-line description, **Refresh** and **Log out**, the
  reserved status slot) with one scrolling list instead of the tree and
  viewer.
- Findings are current state, not history: each one disappears once its
  condition is gone. The list puts **Problem** first (something does not work
  now), then **Warning** (it works, but not as configured, such as an
  environment variable overriding a saved setting or model key), then **Note**
  (a deliberate choice worth knowing, such as access checks turned off).
  Severity is the team presence dot with its word: red Problem, amber
  Warning, neutral gray Note. Never show the color alone.
- Above the list, a summary row repeats the counts as the same dot-and-word
  chips ("2 Problems", "1 Warning"), leaving out zero counts.
- Each finding shows its title in strong text, what it affects, **What to
  do:** with the concrete repair (`/auth model`, a `team.env` variable, a
  Settings page), and a muted line "Reported by <source> · since <local
  time>". Technical text goes in a collapsed **Technical Details** element.
  Long names and addresses wrap inside the row.
- Sources the gateway could not ask (the broker, for example) appear above
  the list as the red `warning-callout` with `role="alert"`, because silence
  must not look like health. An empty report says "Nothing needs attention."
  in muted text.
- At most 20 findings per page with the shared `pagination.js` controls; the
  gateway pages and counts. Refresh uses the standard busy indicator. The page
  only reads: every repair happens elsewhere.

## Knowledge Page

`/webchat?view=knowledge` is **Knowledge**: the team's and the person's own
knowledge pages. It applies the existing rules; it adds none.

- Frame: the Workspace Files frame with brand lockup, kicker and title,
  **New page**, **Refresh**, **Log out** and the reserved status slot, then Memories-style tabs **Team**,
  **Personal** and **My proposals** with **Home** and **All pages** navigation.
  Each space opens its ordinary `home` article in a full-width reader. This
  article is the editable wiki landing page; its topic links provide the main
  reading path. If absent, explain how to create it and offer **Create Home
  page** and **All pages**. Opening Knowledge never creates or rewrites content.
  Home creation fixes the title to **Home** so its normal title-derived name
  is `home`; it uses the ordinary page editor, permissions and revision checks.
  **All pages** reveals the Workspace two-pane layout: a search box and page
  directory on the left, one viewer on the right. It toggles closed without
  replacing the article. **Home** closes the directory and opens that space's
  landing article. Narrow screens stack the directory and viewer as Workspace
  Files does. **My proposals** carries the count of
  proposals waiting for this person (see **Attention counts**).
- Lists: 20 rows per page with the shared `pagination.js` controls and a
  visible range and total in a separate footer below the scrolling rows. Rows
  must keep their full height; shrinking a grid child with `overflow: hidden`
  clips the rows without making its parent scroll. Reaching the twentieth row
  and the pagination footer must be checked in a real browser with long lists.
  Page changes reset list scrolling; if a removal empties the final page, read
  the last remaining page. Use the
  standard opaque row stripes, and the selected row marked by its left edge
  and `aria-current`. Each row shows the title, the summary or search snippet,
  and the local change time. Below search, a full-width select uses the existing
  `memory-editor-input` style for **Current pages** and **Archived pages**.
  Current pages is the default when changing tabs or signing out. Search and
  pagination retain the chosen status within a space. Archived pages open in the
  same viewer with **Restore**.
- Viewer: the heading holds the title, a muted meta line (space, type,
  revision, who changed it last and when) and the actions. Page text goes
  through the same escaped Markdown renderer as chat messages; afterwards its
  `[[links]]` (found in text nodes, never in code) become buttons styled as
  links that open the page, and a link to no page stays text with a muted
  "(no page yet)". Archived and
  frozen states, proposals waiting for this person, and "<old name> was merged
  into this page" are plain notice lines. Notes, revisions and links reuse the Memories row and tag styles.
  Article addresses keep the selected space, full page reference and optional
  revision in the URL. Reload and browser Back/Forward reopen that article
  through the normal authenticated API. Creating an article preserves the
  previous article in browser history; editing keeps the same entry. After
  reverting, the address opens the new current revision. The URL names no person. Sign-out
  clears the reader, directory and pagination and resets the address to Team Home.
- Editing: the Memories inline editor (title, summary, tags, text) inside the
  viewer, with **Save**/**Create page** and **Cancel**. Results and refusals
  follow **Errors and Results**: at the top of the editor, scrolled into view,
  red with `role="alert"` for refusals, text kept. A conflict offers **Show
  the latest version**; content held for review lists each flag by kind,
  field, line and column (never the text) with **Save anyway**.
- Archive, Revert, Withdraw and publishing a flagged proposal use the
  two-step confirmation on the same button, like Memory deletion. Busy
  states use the shared progress labels (**Saving…**, **Publishing…**).
- Proposals show what would change: changed title, summary and tags as
  "before → after" rows and the text as a line diff marked with `+` and `-`
  (color is never the only sign), with the agent's reason under **Why (from
  <agent>)**. Archive proposals describe a status change that keeps the current
  page's content. They offer **Publish** and **Withdraw**, with a notice when the
  page changed since the proposal, and no **Edit, then publish** action.

## Workspace Files

- `/webchat?view=files` is **Workspace Files**. Every signed-in person can open
  it from the Settings rail. Members see **Your files**: only their own folder.
  Administrators see **Workspace**: `team/`, `agents/`, and their own folder.
  Use that name as the page title and the tree heading, and do not label the
  page as an administrator view.
- A lazy file tree sits on the left and one viewer pane on the right. Narrow
  screens stack the browser above the viewer. Keep folder expansion in place so
  browsing does not replace the selected preview or create page-level
  navigation churn.
- Keep the last filename extension visible when a tree row is narrow. Clip the
  stem first, then reveal the full name in the standard frosted tooltip after a
  one-second hover or keyboard focus. Do not make users scroll the tree
  horizontally just to distinguish file types.
- The gateway lists only the folders the person may read. It hides sensitive
  paths, version-control internals, dependency trees, virtual environments,
  every symbolic link, and every multi-linked file. Direct path requests must
  enforce the same rules as directory listings. Unsupported formats and files
  above the 5 MB preview limit show a short explanation in the viewer and expose
  no raw content response.
- Render Markdown through the same escaped renderer as Web Chat messages.
  Render HTML in a separate iframe with an empty `sandbox` attribute and a
  response policy that blocks scripts, forms, network access, nested frames,
  and parent-document access. Common source and configuration files use a
  read-only code pane that wraps by default and offers an explicit no-wrap
  view. Recognized languages receive escaped, server-generated syntax coloring
  up to 512 KiB. Valid JSON is formatted for display and offers a raw-source
  view; invalid JSON, JSON Lines, and YAML retain their exact text. The original
  file content remains canonical. Raster images and PDFs may use bounded native
  browser previews.
- The viewer never edits files. Clicking a row selects it (one at a time): a
  file opens in the preview, and a folder shows its name with "Folder" and a
  short note that folders have no preview. For an item the gateway allows the
  person to share or send, **Share…** and **Send…** (files only) appear at the
  top right of the preview pane's heading, after any preview controls: ghost
  buttons with their icon (a person with a plus, a paper plane) and word, and
  "Share <name>" or "Send <name>" as the accessible name. The tree itself
  carries no buttons (owner's request 2026-10-04; it replaced a strip under
  the selected row, which had replaced buttons overlaid on rows). Each opens a
  frosted modal dialog titled with the item's name, with focus containment,
  Escape to close, and focus returned to the button.
- **Share…** has a person picker listing team members by name, the level
  (**Read** or **Read and change**), how long (**Until I stop sharing**, **1
  day**, or **7 days**), and **Share** (primary) with **Cancel**. The item then
  shows a **Shared** chip in the tree. Stopping a share happens in Access →
  Files.
- **Send…** chooses **To a person** (with the person picker) or **To the team
  folder**, and **Copy** or **Move**. For a person, the dialog says that they,
  or an administrator, must approve before the file arrives in their
  `received/` folder; the request appears in their Inbox as a delivery request.
  For the team folder, it says the file arrives at once. **Send** (primary) and
  **Cancel** close the dialog; the page status line then says whether the file
  arrived or is waiting for approval.

## Visual Access Reviews

The live implementation uses the shared `workflow_graph.mjs` renderer in chat
and on `/workflows/<id>`. It presents a validated dependency graph, reasons,
agent-reported progress, information dialogs, and diagnostic downloads. Each
step lists its linked access and change requests with their current status in
words (waiting, approved, approved and applied, denied, withdrawn); a waiting
request links to its Inbox review. A denied step reads **Denied: it will not be
asked for again**. Requests that matched no single step are listed separately.
Setup pages a step waits for (sign-ins, account links, the team's sign-in app)
are listed on the step in the same row style as requests: the page's name
("GitHub sign-in") and trusted code's plain status ("Waiting for the person to
finish the page", "Signed in as exampleuser", "The page expired", "Closed without
finishing; it will not be asked for again"). Waiting and expired pages use the
pending style, finished ones the approved style, closed ones the denied style.
They carry no action (the page itself opens in chat), except a step done
outside Universal Agents (such as creating a token on a service's site): while
it waits, its row has a **Mark Done** button on the full workflow page (not on
the chat card); after a click it reads **Marked Done** until the step updates.
Setup pages that matched no single step are listed with the requests not on a
step.
Inbox links to the owner-scoped workflow list. There is no review conversation
on the page; questions go through chat. The standalone example in
`ui/access-review-preview/` remains the reference for the broader
combined-access interaction described below.

The live **Review Ready Requests** dialog shows the plan's waiting requests as
the same review cards the Inbox renders (`access_inbox.mjs`), with the Inbox's
controls (see Inbox): each grant's **Keep for later tasks** checkbox,
**Approve**, **Approve reading only** when more than read was asked, and
**Deny** for access; **Approve all** or **Deny** for a proposal; **Approve** or
**Deny** for a staged change. **Approve All** and **Deny All** decide every
request the person can decide. Decisions go
through the access Inbox. Requests only an administrator can decide are shown
without choices. Later input-dependent requests stay on the same page. A batch
can be partly saved when a request is decided elsewhere meanwhile; say how many
were saved. Credential selection opens the trusted broker form and returns to
this page.

Lasting access appears only as the Inbox's per-grant **Keep for later tasks**
checkbox, off by default. Do not show blanket future-use choices, a combined
credential and attachment permit, or automatic **Start/Stop** execution controls
merely because the prototype contains them. Those require their own enforced
runtime contracts. The agent task remains the executor in the live
implementation; source-task failure or completion before the plan finishes is
shown separately from agent progress. The workflow list and activity use
20-record pages that remain stable while status refreshes. Diagnostic exports
retain the most recent bounded events and disclose truncation.

Correcting what an unfinished workflow step needs (its kinds of change and
tools) keeps its identity, dependencies and results. Record the correction in
its history. Steps with linked requests cannot use this correction.

- Present one durable review for the intended outcome, such as **Set Up X
  Access**. Lead with the agent receiving access; show **Requested by
  TeamArchitect** as secondary context. Internal tool-group names and requesting
  managers must not replace the service name or access recipient.
- The agent submits structured proposed steps and dependencies ahead of review.
  Trusted code resolves registered operations, identities, prerequisites, current
  access, limits, and impact. Render only that validated projection. Agent prose
  is labeled context; agents cannot supply HTML, trusted impact copy, approval
  status, or executable graph edges.
- Use a small directed dependency map. Account selection feeds the selectable
  capabilities; those branches join at enablement and then readiness. Arrows
  mean **requires**. On narrow screens use dependency order and visible
  prerequisite labels. Never require panning a canvas to understand access.
- Put native, labeled checkboxes on capability nodes. Select requested
  capabilities initially; leave other offered capabilities unselected. Offer
  only registered operations that policy allows the reviewer to authorize.
  Unavailable options have a visible reason. Selection updates the dependency
  map and summary without submitting a decision.
- Show required setup as a shared prerequisite with its effect explained.
  Do not present necessary steps as independent, misleading checkboxes. Users
  can remove a dependent capability or reject the workflow. A step requiring
  another person's authority shows **Waiting for <who>**, as Inbox cards do; the
  user can decide their own portion without impersonating that person.
- Use text and icons together for **Connected**, **Needs your choice**, **Needs
  approval**, **Ready**, **Not selected**, **Blocked**, and **Completed**. Color
  alone cannot convey state. Reuse completed steps when identity, revision,
  scope, and current policy still match.
- Place a small circular **i** button beside each step or access item. A click
  opens an anchored, frosted information popup with **Why this is needed**,
  **What it allows**, and dependencies. Use a titled dialog, visible close
  control, Escape dismissal, focus containment, and focus return. On small
  screens use a width-bounded dialog. No hover-only information or native title
  tooltip. Keep account, recipient, effects, duration, cost, posting behavior,
  and restart disruption visible outside popups.
- Keep the summary beside the map on desktop and after it on mobile. Show the
  selected account, consumer scope, actions, duration, future-use rules, and any
  concrete action to execute now. **This agent** is the proposed default scope.
  **Whole team** requires explicit selection and a credential eligible for
  sharing. Access lasts for the current task unless the person turns on **Keep
  for later tasks** for that grant. Short review-session expiry is a different
  fact.
- Put account choice inside the trusted review. Provider sign-in may open the
  broker's secure page, then return to this review. Show the final account before
  the decision. Reuse valid connections; an attachment timeout must not cause
  repeated credential selection.
- Use **Approve selected** and **Reject request** for the whole review. One
  decision can cover selected setup, account consent, and exact use shown.
  Opening info, choosing an account, or checking an option never grants access.
  Unchecked capabilities are excluded and must not trigger automatic replacement
  requests from the agent.
- Explain future actions. **Ask before each post** retains review for future
  content. **Allow future text posts** is a separate, explicit choice with
  account, recipient, limits, and duration visible. Offer it only when trusted
  policy and broker guards enforce those bounds. If a concrete post is included,
  preview its exact content and destination here; do not ask again for that same
  unchanged post after approval.
- After approval, show progress on the same map. Checkpoint completed steps and
  resume remaining work after interruption. Changed scope or account needs a
  review of the change. A service error does not justify repeating all approvals.
  Rejection stops unstarted work for this request; it does not revoke unrelated
  existing access.
- Chat and Inbox open the same workflow and revision. The compact chat card
  (**Your Access Plan**) shows the plan's title, the agent type it is for, its
  goal, and the dependency map, with **Open Full Workflow**. Keep 20-row server
  pagination and bound graph size; group large plans by service with counts and
  a paged list. Every selected effect must be inspectable before approval.
- The dedicated page keeps the same dependency map with the current step,
  upcoming steps, activity, **Other Requests From This Task**, **Review Ready
  Requests**, and **Download Diagnostic Logs**. It has no conversation and no
  start button: the agent's task carries out the plan, and questions go to the
  conversation through **Open Chat**. Already approved unchanged steps run
  without another approval. A ready draft and its setup can be approved
  together. Unknown future content receives a later exact review.
- Show why the current step is running or waiting and what depends on it. Mark
  automated steps and human decisions before starting. Prefer completed-step
  counts and named states to invented progress percentages. Show explicit
  waiting, interrupted, stale-connection, and disconnected states.
- Derive a step's review label from every linked request's current status.
  Show how many decisions remain and how many are approved.
  Never infer approval from one request. After all approvals, state that
  execution still needs verification. Pending review keeps the original task waiting; it is not a
  workflow failure or a completed step.
- A batch of change requests pauses its task once and resumes it after every
  request is decided; do not add separate chat summaries for each click. A live
  workflow link must use the tool-returned `/workflows/<id>` URL; skill
  documentation links cannot substitute for it.
- Open a focused review dialog when human input is needed. Let the user close
  it to read the map or ask a question, then reopen with **Review Ready
  Requests**. Preserve selections while closed. The conversation in Chat
  clarifies, requests changes, and reports progress; free-text agreement never
  submits an approval. A revised
  effect invalidates only its affected pending review and dependent authority.
- Batch every currently reviewable, compatible effect into one decision and
  let users remove individual effects. Display what remains for later and why.
  Do not split a known credential/setup/use plan into consecutive dialogs. Do
  not include unknown content, another owner's decisions, or changed effects in
  an earlier approval. Leaving publication out must not trigger another prompt
  for that same publication.
- On failure, retain the map and show the failed step, a plain-language issue,
  completed work, blocked dependents, and the next valid action. Distinguish a
  confirmed failure from an unknown external result. If a post might exist,
  offer **Check publication result**, not an automatic publish retry.
- Offer **Download diagnostic logs** beside failures and in the workflow header.
  Explain that the export contains step states, event times, error codes, and
  safe runtime references for debugging. Exclude credentials, bearer links, raw
  provider responses, and conversation/post content by default. Production
  exports must come from authenticated, owner-scoped, redacted server records;
  do not scrape browser content or offer another user's logs.
- Diagnostic format 3 includes incomplete dependencies, each linked request's
  current outcome (structural facts only, no titles or reasons), explanatory
  codes, and the ended task's timestamp and known system lifecycle events. Separate local-plan counts from open workflow steps.
  Oversized tool-result events indicate context pressure, not an external-service
  failure. Unknown or missing evidence must stay explicit. Never export free-text
  task summaries or arbitrary tool-event details to explain a failure.
- Keep recent activity bounded to 20 displayed records with a way to inspect
  earlier records. A diagnostic export can cover the full failed attempt within
  an explicit server limit; report truncation. An export or copied log never
  grants authority. Closing the page does not stop the agent's task; the person
  stops it from Settings → Tasks with **Stop and remove**.

## Time And Expiration Displays

- User-facing websites should not expose raw ISO timestamps as the primary
  visible time format. Keep raw timestamps in data attributes, logs, APIs, or
  diagnostic details only.
- Absolute times shown to users should be local and readable, with a timezone
  label when ambiguity matters.
- Short-lived sessions, credential forms, broker auth pages, and setup tokens
  should show remaining lifetime as a countdown, not as a full date.
- Countdown timers must be functional when displayed:
  - compute from a real machine-readable expiration value,
  - update at least once per second for minute/second displays,
  - switch to a clear `expired` state when time has elapsed,
  - keep working in generated previews and standalone broker pages.
- For short expirations, prefer `m:ss` such as `14:59`; this is more scannable
  than prose such as `about 15 minutes`.
- Countdown styling should use normal readable text weight and color. Do not
  communicate urgency through color alone.
- Tests or preview checks should verify both the initial readable value and
  that the countdown changes over time when countdown behavior is added or
  modified.

## Onboarding Flow

- Welcome is for look-and-feel customization and first contact.
- Welcome shows the compact **Color Theme** picker before **Customize Theme**.
  Customization hides the onboarding pane and uses the same narrow left-side
  control panel as Settings, preserving an unobstructed live preview and one
  consistent tuning workflow.
- The step rail is a progress indicator, not free navigation to future pages:
  users may click completed/current pages, but unreached pages stay disabled
  until unlocked by the `Next` button.
- Let's Begin offers a new-user path and migration paths, including
  **Import a team export (upgrade or move a team)**. That choice replaces the
  secrets card and file pickers with one card: where to copy the export (the
  data folder's `import/` path), the export files found there as one-per-row
  radio options (name, size, when copied; the only file is preselected),
  **Look again**, a revealable **Export passphrase** field and **Open
  export**. Next stays disabled on this path. A folder that already holds an
  instance shows the reason as an alert and disables **Open export**; a wrong
  passphrase shows "The export was not opened." with the reason as an alert and
  clears the field.
- Below the path choices, Let's Begin shows one toggle card, **Secrets come
  from the environment (Docker secrets). Don't write team.env.**, off by
  default, using the standard `choice-card` toggle with its help text always
  visible under the title (`choice-note`), not behind an info icon. It applies
  to both paths and follows `launch.secrets_source` of an instance already in
  the folder or of an imported file. While it is on, Safety hides **Generate
  new internal credentials** and Review offers no `team.env` download.
- After a settings import, Let's Begin shows the import message and, below
  it, a scrollable **Not imported:** list (`begin-import-skipped`): one line
  per setting or section, its dotted name in code style followed by a plain
  reason. Import errors appear in the same message line in plain words.
- Runtime Basics is the only page that gates Next on successful connection
  testing.
- Runtime Basics groups the selected model's optional input capabilities in the
  model card as three equally weighted toggles: `Supports Images`, `Supports
  Audio`, and `Supports Files`. Do not combine them into a single multimodal
  switch because provider support differs by input type.
- Runtime Advanced Setup uses an editable reasoning-effort field with common
  suggestions rather than a closed enum, because supported values vary by
  provider. Keep the numeric context-window control beside it and enforce a
  1024-token minimum.
- Runtime Basics, Safety, Communication, Team, and Secrets may include collapsed
  `Advanced Setup` sections for deployment-oriented controls. These sections
  stay closed by default, sit at the bottom of the tab, use an explicit
  chevron expand affordance next to the title, and every control in them must
  map to a real generated `instance_config.yaml` field or `team.env` value
  rather than preview-only state. When expanded, the section shows a clear
  horizontal divider before the advanced controls and remains inside the same
  tab scroll pane.
- Safety uses toggles plus frosted information popups.
- Safety offers no switch or security-mode select that turns the secure tool
  broker off: the broker always runs, so its Advanced Setup holds only broker
  address, port and credential-store settings.
- Communication uses one accordion row per channel.
- New-user onboarding should omit advanced local-only channels such as Local CLI.
- Channels that are designed but not production-ready, such as WhatsApp, remain
  visible only as disabled gray options with a short availability note.
- User Preferences starts with quiet-hours settings.
- Team keeps Team Architect locked on and makes specialist agents selectable.
- Team Advanced Setup may collect public skills repositories for setup-time
  import. The controls must validate reachability when leaving the Team tab
  and surface warnings in the same pane without breaking aesthetic preview mode.
- Secrets explains optional reach-extending API keys without making setup feel
  blocked.
- Secrets shows who uses the accounts set up there right below the key fields
  (owner's decisions 2026-10-08): one `choice-card` toggle, **Share accounts
  with people you add**, on by default, with its help text always visible
  (`choice-note`). While it is on, a list follows with one Team-page style
  `agent-row` per service: the model keys first (captioned with the provider
  and model chosen on Runtime Basics), then the services a team may choose,
  then the fixed ones as locked rows (`is-locked`, disabled toggle) whose
  caption gives the reason and whose state line says **Always private.**
  (GitHub, Google) or **Always shared.** (the team's own Crawl4AI server). The toggle on
  means shared. Every row has a state line (`sharing-state`) that says in
  plain words what its choice does, and it changes with the toggle, so no
  choice needs an information button to explain itself. Rows come from the
  tool catalog through `/api/bootstrap`, never from a list in the page script.
  Turning the card off hides the list and makes every choice private. Review
  shows one **Accounts** line naming what is shared and what is private.
- Secrets may include Advanced `.env` fields for optional service endpoints
  and integrations; empty fields must not overwrite uploaded env values.
- Secrets warnings such as missing voice API keys are visible and red, but
  non-blocking unless the runtime truly cannot proceed.
- Review becomes the final configuration step: show a succinct summary, surface
  red flags, keep configuration downloads disabled until files are finalized,
  then allow launch/restart after the user can download the generated files.
  Users may rerun finalization to regenerate the files; downloads and launch
  are disabled only while regeneration is actively writing.
- When secrets come from the environment and finalization finds required
  secrets missing, Review shows them first in a red `warning-callout` with
  `role="alert"`: each secret's name in code style, what it is for, how to
  create it, and the `<NAME>_FILE` alternative, never a value. **Launch!**
  stays disabled while the list is not empty, because the runtime cannot
  start without them; adding the Docker secrets restarts the container.
- Onboarding completion removes progress framing and offers **Open Status**.
  It explains that the person must sign in to see team status, and does not
  poll private agent data. The action opens the existing Web Chat Status view,
  which handles sign-in and enrollment before loading diagnostic data.
- The completion **Open Status** action uses the live browser-facing website origin and
  public port supplied by onboarding bootstrap. It must not substitute the
  container listener/bind port when those ports differ.
- The post-launch reload/standby page should show a compact animated loading
  indicator next to the wait message, and status-page icons must stay bounded
  to normal icon dimensions.

## Communication Accordions

- Each channel row has its own toggle.
- Enabled channel rows can expand to reveal fields.
- Opening a channel should animate rather than snap.
- Accordion rows size to their expanded content and the accordion container
  owns scrolling; a fixed-height accordion must never shrink an open row and
  clip token controls behind the following channel.
- Text for the selected/open tab is ocean blue.
- `Set as default delivery channel` is compact and must not span the full
  accordion width.
- Setup walkthrough overlays should use numbered steps and external links that
  open in a new window.
- Web Chat has no setup walkthrough; it has a fixed `/webchat` public URL and
  auth-token controls.
- Voice settings sit above channel selection and apply across supported channels.

## Web Chat

- Use descriptive text links such as **Open Inbox** instead of printing
  navigation URLs in messages.
- Cancellation receipts summarize remaining scheduled work with counts of
  scheduled items and distinct scheduled objectives. Link to the schedules and
  objectives dashboard for names, states, and next-run details; do not append
  an inventory of jobs to the chat message.
- Web Chat uses the same polygon mesh background and saved appearance settings
  as onboarding.
- Chat panes are more transparent than onboarding panes. In the dark theme
  the conversation pane covers about 65% of the background (raised from
  57% at the owner's request) and the sidebar about 67% (12 px blur), so the moving plates show through (owner's request,
  2026-10-04); the sidebar keeps more cover because its small muted text must
  stay above 4.5:1 contrast over brightly lit plates. Message bubbles and the
  composer blur what is right behind them (8 px) to keep their text readable.
- **Log out** sits at the bottom of Settings' left column, below the page
  links, which leaves the sidebar's account row for the name, connection
  state and version (owner's request, 2026-10-04).
- The auth-token panel is shorter and wider than the onboarding content pane.
- A reusable Web Chat token or one-time invitation code must be entered into
  the `/webchat` auth panel and exchanged with a POST request for an HTTP-only
  browser session; do not place either credential in the URL.
- A one-time invitation creates an enrollment-only session. Before Chat is
  visible, show a blocking frosted **Finish Setting Up Web Chat** dialog that
  reuses onboarding's generated-token control, including Generate, reveal,
  copy, and confirmation actions.
- The permanent token is shown only once and must remain in page memory only;
  never place it in a URL, cookie, local storage, or session storage. Keep the
  server token pending and unusable until the user selects **I've Saved My
  Token**.
- If a pending enrollment resumes after the raw value is no longer present,
  explain that it cannot be shown again and offer **Generate Replacement**.
  Replacement revokes the prior pending credential.
- After authentication, keep the account controls as a compact block at the
  bottom of the sidebar. Show the signed-in display name without a redundant
  `Signed in as` label and truncate it with an ellipsis when space is
  constrained. Log out lives in Settings (see above).
- Show the canonical public release version as muted text at the far right of
  the same compact row as the account name. Read it from the server's
  `VERSION`-backed settings response rather than hardcoding it in browser
  assets.
- Serve the Web Chat and configuration-editor HTML, CSS, JavaScript, and image
  assets with `Cache-Control: no-store`, and give their shell references one
  shared content-derived revision, so controls and layout from different
  releases cannot be combined in one browser render. The revision is necessary
  to bypass unversioned assets cached before this policy existed.
- Logout returns to the authentication panel without changing the user's
  gateway-owned transcript. Signing in as another canonical user loads only
  that user's server-owned sessions and disposable principal-scoped UI state.
- The assistant name replaces generic "Universal Agents" text inside the chat
  interface.
- The gateway is authoritative for Web Chat transcript messages, stable message
  ids, ordering, session ownership, and conversation mappings. Browser storage
  may retain drafts, active selection, unread presentation state, and
  disposable thread metadata, but never an authoritative message list.
- Echo a submitted user message into the active transcript immediately, while
  keeping that row local and disposable until the gateway returns the accepted
  transcript row for the same client request id. Watch acknowledgement in the
  background for 15 seconds. If it does not arrive, retain the bubble and show
  lowercase red **not delivered** text beneath its content and attachments. A
  late acknowledgement replaces the local row and removes the warning.
- Anchor each local echo after the highest durable `session_sequence` visible
  when the user submits it. Keep pending and failed echoes at that position as
  later durable rows arrive. When an earlier local echo receives its durable
  user row, advance later local echoes past that row so concurrent submissions
  retain their original order. At the same anchor, render the durable row,
  memory-only transient items, and local echoes in that order. Do not use the
  displayed wall-clock timestamp as the canonical ordering key.
- Load sidebar session metadata at sign-in, then hydrate only the active thread
  with its newest 50 durable messages before starting live event delivery. Load another
  thread's newest page when selected, including selection after archive.
  Background events may update unread state without preloading unopened
  transcripts or moving the active reader's viewport.
- Load earlier history in 50-message cursor pages when the reader scrolls
  upward near the top. Keep **Load earlier messages** as a keyboard-accessible
  alternative. Allow one page request per thread at a time and retain the
  visible message's position when prepending. Follow new replies only when
  already near the bottom; selecting a different thread opens its newest end.
  Sending a message always shows it at the bottom, even after scrolling up,
  and the reader follows from there; replies never move a reader.
  Whether the reader follows the latest message comes from their own
  scrolling, not from measuring at render time. Keep following, or keep the
  rows being read still, when content changes size after it renders (a
  workflow or access review card filling in, an image loading); cards never
  scroll the view to themselves (`chat_transcript_view.mjs`). A re-rendered
  workflow card starts from its last loaded content, not a loading line.
- Merge history and events by server `message_id`, ordered by `session_sequence`.
  Show an explicit retry banner for failed history requests. Discard responses
  from an earlier sign-in or invalidated history revision. Cursor resets
  invalidate cached history and reload only the active thread; metadata-only
  refreshes must not advance the event cursor or rewind older-page navigation.
- Thread history includes a New Chat button plus per-thread rename and archive
  icon buttons. Archive hides a thread from the active list without implying
  permanent transcript deletion. Keep the history label and its scrollable
  thread list together as the sidebar's flexible region so rows stay
  top-anchored immediately below the label rather than drifting toward the
  account controls.
- Opening or refreshing Web Chat must not create a conversation. Restore an
  existing selection when available; otherwise, once the server has answered
  with no thread, show one selected, unsaved "New Chat" row (no rename or
  archive; choosing it focuses the composer) and an empty composer (owner's
  request, 2026-10-09). New Chat and the first submitted message explicitly
  create a conversation, which replaces that row.
- A thread's name is fixed once given (owner's request, 2026-10-09; it used
  to follow the latest message, renaming the thread on every entry): the
  name the person gave it, else the first ordinary message typed in it (the
  earliest that is not a slash command, or its first attachment's file name),
  else "New Chat". The server sends it as `title`
  (`assistant_gateway/application/web_chat_threads.py`), and the thread
  list's revision follows names, not every message, so open pages reload
  the list only when it shows something new.
- The gateway's own delivery threads on a person's Web Chat (the team's
  proactive updates and the person's memory reviews) are listed only once
  something has arrived in them, as "Updates" and "Memory review". They used
  to show as two empty "New Chat" rows on a newly set-up Web Chat, and the
  first message went into the memory-review thread.
- Thread rows show a compact active-reply indicator between the truncated
  thread title and rename/delete actions while the agent is still replying.
- The active-reply pulse must remain clearly visible after its thread loses the
  active-row backdrop. Keep the pulse at 78% opacity or higher and use scale
  plus a stronger blue halo to convey motion on an unfocused row.
- The initiating browser request owns that active-reply indicator until its
  terminal success or failure. Durable transcript delivery only merges
  presentation rows; an accepted user row must not clear the indicator while
  the request is still waiting for the agent or create an unread-reply dot.
- Any unfocused thread with a fresh unread reply uses a green border and halo.
  If that thread is still running, keep the blue pulsing dot so unread content
  and active execution remain distinct signals. Use a gold border and halo
  instead when the unread reply needs user attention.
- Completed replies in unfocused threads use that same indicator slot as a
  solid dot: green for ordinary unread replies, gold/yellow when the reply
  needs user attention such as an error or recoverable runtime-pause choice.
- Truncated thread titles show their full title in a custom frosted tooltip
  after one second of hover or focus; do not rely on native browser title
  tooltips for this.
- The Web Chat sidebar header places the brand/UA lockup on the left and a
  compact Settings cog on the far right. The cog uses the same frosted control
  treatment as other icon buttons, has an accessible name, and opens Settings;
  do not render a second full-width Settings or Inbox button below New Chat.
- The sidebar brand/UA icon in Web Chat opens the authenticated config editor.
- The brand/UA icon in the config editor returns to Web Chat.
- The input placeholder is `Type your message here...`.
- The send action uses an ocean-blue icon button.
- A compact **Launch parallel thread** switch sits below Send, off by default,
  with no helper sentence.
  It creates a parallel thread: the browser asks for it when it creates the
  chat (`{"parallel_thread": true}`), and every request sent in it runs at the
  same time as the agent's other work. Admit its request without waiting for
  the current model turn, so Settings → Tasks can show and control it
  immediately. New Chat and `/new` start ordinary threads.
  It applies to one ordinary message; slash commands keep their usual behavior.
  Create the new chat before clearing the composer, then show the message,
  files, name and replies only there. Keep the earlier chat in the sidebar.
  Reset the switch once the message moves to its chat. While creating the chat,
  disable composer edits and New Chat, and announce **Preparing your chat…**.
  A failed creation keeps the draft and files. **Retry** on a send failure
  resends the original message and files with the same request id and destination.
  Show each send error and its **Retry** beside the failed message, using the
  shared error text and compact secondary button. Later messages and their
  acknowledgements must not remove another message's retry. Retrying a send
  does not use or clear a newer composer draft. Remove that message's retry
  when the server accepts it; clear retained files at sign-out or archive.
- The composer includes an icon-only paperclip button and a hidden file input.
  Pasted images and drag/drop files appear in a compact attachment rail above
  the textarea before sending.
- Attachment preview chips use the same frosted compact control style as other
  Web Chat controls: image thumbnails for images, file chips with icon/name/kind
  and size for documents/audio/video, plus an icon-only remove button.
- Attachment rendering inside messages stays below the markdown body. Images
  render as bounded thumbnails that open/download through the authenticated
  attachment URL. Audio and video use native browser controls only for served
  safe content types. Documents render as compact download chips.
- Message copy controls copy only raw text or markdown content, never binary
  content or attachment metadata.
- Dark user and assistant message bodies use the supporting blue-gray
  `#adc1c8`. System messages use neutral light-gray body text `#c8cfd2`, a
  quiet Deep Ocean frosted background, a subdued amber left accent, and a muted
  warm **System** label. Transient and error notices may retain stronger alert
  treatment.
- In dark mode, the message textarea stays transparent over the composer panel
  so the input, attachment control, and send control read as one unified
  surface rather than a black inset rectangle.
- Every durable user, assistant, and System message shows a quiet local timestamp
  beside its sender label. Use time only for messages from today, add a short
  date for older messages, and retain the full local date, time, and timezone as
  the accessible label.
- Web Chat sessions keep slash-command guidance above the durable transcript,
  including after messages arrive. Render it as a distinct **Web Chat guide**
  rather than a server-authored System message.
- Render command guidance as semantic syntax rather than a Markdown text blob:
  command names use ocean blue, selectable options use green, placeholders use
  amber, and grouping brackets/separators use muted gray. Put visible spaces
  around pipes and brackets, preserve nested groups, and let long invocations
  wrap independently from their plain-language descriptions.
- Keep the guide as a quick reference rather than an exhaustive command manual.
  Show no more than three representative options before an ellipsis, move the
  complete syntax to the user documentation, and add a compact **see options**
  or **see controls** link when more forms exist.
- Every visible slash-command name in the guide links to its relevant user
  documentation in a new tab. Keep the command and option links keyboard
  focusable and visibly identifiable as links.
- The Web Chat command list includes browser-safe `/user` administration
  and self-service forms. Direct external-id registration and `/user link`
  return concise guidance to finish the binding on the external chat platform.
- One-time credentials and capability material returned by `/user`
  appear in a dismissible, copyable memory-only notice in the same ordered chat
  timeline as durable messages. Place the notice immediately after its safe
  durable System receipt. Refresh intentionally removes the secret notice.
- Broker sign-in pages from `/link <tool>` (GitHub, Google, X) and `/auth`
  (team sign-in apps, password managers) use the memory-only **Open** link control,
  never a copyable raw-URL notice. Open the secure form in a separate tab with
  `noopener noreferrer`; show the broker expiry countdown when supplied.
  Dismiss or refresh removes the link. Authentication status remains available
  through `/auth status`.
- Credential collection, deletion, manual-signup collection, and tool setup
  pages a task asks for (kind `tool_setup`: a sign-in, an account link form, or
  the team's sign-in app) use the same memory-only layer. A setup card's copy is
  the task's own one-sentence reason plus one short note (what you enter goes to
  the secure broker, or that linking gives nobody access), and its action label
  names the step, such as **Sign in to GitHub** or **Choose your account for
  Portainer**. A setup card closes as soon as its own page ends: the gateway
  posts one short System line under the page's own id ("The GitHub sign-in is
  done (account exampleuser).", "The page for the GitHub sign-in expired before it
  was finished.", or, after the person's own `/link`, "You finished ... yourself,
  so this page is no longer needed."); a page offered again gets a new card
  that this line never closes. Show an explicit frosted **Open**
  action in the same ordered chat timeline as durable messages. Use the
  gateway-owned `session_sequence` plus an in-memory subsequence so the action
  follows the safe System receipt created with it and remains before every
  later message. Do not use displayed wall-clock timestamps as the canonical
  sort key. Use concise descriptive copy and the action label as the visible
  link; never print the bearer URL as text. Open it in a separate tab with
  `noopener noreferrer`, and display a working `m:ss` countdown from the broker
  expiry. At expiry, remove the Open action and countdown, show a clear local
  expired status, and keep manual Dismiss available. Keep the inert card until a
  correlated terminal result arrives because a submission claimed before expiry
  may still finish. Remove the card after either a successful or failed terminal
  result. Multiple pending actions retain independent positions, countdowns, and
  terminal correlation. Do not show the generic copy control for these link
  actions. The broker URL must never enter transcript history, replay events,
  local storage, or session storage. Setup pages that a waiting task
  requested arrive through the authenticated live connection even when the
  original chat request has ended. Keep their URLs in gateway and browser memory only. A page refresh
  may retrieve a still-live form from the same gateway process; gateway restart
  does not recover an issued form. Dismissal suppresses repeated presentation of that
  exact request for the current browser session.
- A `/link` card (an account link form, kind `account_link`, or a sign-in
  page, kind `broker_authentication`) is finished by the gateway's result
  message, not by an agent task. While the page is open, chat history keeps one
  System line in place of the card, such as "Account link form opened for
  mcp:crawl4ai (the team's account)." or "GitHub sign-in page opened.", never
  the page address. When the page ends, the gateway posts one System message
  (`link_result`) with the card's correlation id in the chat that ran `/link`:
  linked (naming the tool and whose account, pointing to Settings → Access →
  By Tool, and saying a link gives nobody access), closed without linking,
  expired with the command to run again, refused with the broker's plain
  reason, or "couldn't confirm" with where to check (Settings → Access → By
  Tool, or `/auth status` for sign-ins). Web Chat removes the card when that
  message arrives; until then an expired card keeps its local expired state.
  The card lives only in the page that ran `/link`, so a refresh drops it and
  history shows the opened line and, later, the result. After a gateway
  restart no result follows for a page that was still open, and the card keeps
  its expired state with Dismiss. `/auth google team_client` and `/auth x
  team_client` cards get the same result message ("The team's Google sign-in app
  is stored; people can now sign in with it."); other `/auth` cards get none.
- When a broker collection or tool setup result completes a waiting interactive
  Task, append the agent's success or failure confirmation to the originating
  Web Chat thread immediately. Quiet-hours batching applies to autonomous work,
  not to the completion of a user-initiated form workflow. Write that
  confirmation as ordinary user-facing language that names the service or
  affected agent. Do not show credential keys, raw workflow status values,
  opaque ids, broker implementation details, or routine assurances about
  redaction. When a failed Task has both a friendly answer and diagnostic
  errors, render the answer and retain the errors only for attention state and
  operational diagnosis.
- Manual-signup broker forms may show a separate validated HTTP(S) signup
  action above the credential fields. Name the service and destination host,
  explain that signup finishes first in another tab, and never render a
  template-provided non-HTTP URL or URL containing credentials.
- Broker consent checkboxes use a compact control aligned to the left of a
  readable, wrapping label. The entire label is clickable. Use the theme's
  strong text color in both themes, retain keyboard focus, and exclude the
  checkbox from full-width text-field sizing. Share this layout across account
  link and deletion forms.
- The authenticated `/webchat?view=objectives` surface is **Runtime Controls**.
  It uses the same background, glass treatment, identity, and session lifecycle
  as chat, with compact tabs for Objectives, Schedules, and MCP resources.
  Current lifecycle state and available actions come from the authoritative
  manager and gateway stores; Activity Collector data may enrich history but
  never authorizes a control.
- List schedules by ascending next-fire time. Put missing or invalid times last
  and use schedule IDs to break ties. Preserve this order across refreshes.
- Repeated Runtime Controls cards are strict containment boundaries. Long names,
  identifiers, tool names, and agent types wrap inside their card rather than
  expanding a grid track or covering a neighboring card. Give a schedule's
  pause-until input its own full-width row, then keep Pause, Disable, Delete, or
  the currently available lifecycle actions together on the row below it.
- Team displays the gateway's live same-team presence as a table with Agent,
  Type, Status, Skills, and Last Seen columns. Pair every colored status dot
  with a label: green Available, amber Busy, red Exiting, and neutral gray
  Paused, Stale, or unavailable. Show skills as a count with an expandable
  semantic-name list rather than an unbounded cell. The automatic presence
  refresh must preserve each expanded skill row by stable agent identity and
  discard that UI state only after the agent leaves the roster.
- Below presence, Team uses matching compact tables for **MCP Connections** and
  **Hosted MCP Servers**. Show semantic names and lifecycle first; keep digests,
  source locators, listener details, and compatibility flags in expandable
  details. Never render endpoint credentials, injected headers, Sealbox keys,
  bearer tokens, or sidecar log contents.
- Connection rows show the agent types a server was proposed for, with the note
  "Who may use it: grants on <tool> (Access page)", because grants, not the
  connection, decide which agents may call it. An installed connection reads
  **Installed**; a pending one reads **Waiting for approval**.
- The Credentials column uses **None**, **Set by administrator**, and **Uses a
  linked account**. Set by administrator means the broker resolves an approved
  administrator-owned header or environment reference; it must not be shown as
  None or expose the reference value. Uses a linked account means the broker
  uses the account a person linked for this tool; never show the account's
  details.
- Pending MCP connection rows may show fixed **Approve** and **Reject** actions
  only when the server says that authenticated user can decide the exact
  resource. Button meaning and payload mapping remain gateway-owned. A
  successful choice refreshes the row and inserts a normal System receipt.
  Friendly mode names the server and outcome; technical mode also includes the
  request and decision digests. While either decision is being saved, both
  actions are disabled and the selected button shows its visible busy state so
  repeated clicks cannot submit the same decision twice.
- Authorization API failures return a stable error code plus a plain title,
  message, and recovery step. Every visible failure says what did not happen,
  why, and what the user can do next. Log the internal exception; do not expose it
  as browser copy. Friendly mode shows the explanation and recovery step;
  technical mode also shows the stable code, HTTP-style status, operation, and
  failure type.
- Debug controls, when enabled by config, sit at the bottom of the Web Chat
  sidebar as compact operational controls rather than chat-message prompts.
- Context-dump debug controls include a compact last-dump timestamp row using
  the browser's local timezone formatting plus the dump ID.
- The config editor opened by `/edit config` is a dense operator tool: a
  full-height YAML editor with compact save/download/reload controls, backup
  restore controls, and a clearly confirmed runtime restart action. It should
  provide lightweight YAML syntax highlighting plus code-editor keyboard
  affordances for comment toggling and two-space indentation. The YAML editor
  uses the shared regular text font rather than a monospace face, and the
  textarea and highlighting layer must share identical font size, weight,
  line-height, padding, and text-transform behavior so the caret stays aligned
  with visible text.
- Chat message bodies render sanitized markdown for common assistant output:
  paragraphs, explicit links, bare URL autolinks, lists, blockquotes, tables,
  inline code, and fenced code blocks. Raw HTML must remain escaped.
- Bare URL autolinks must not absorb trailing Markdown formatting markers such
  as `**` into the link target. If an assistant sends `**https://...**`, render
  it as a bold link or at least keep the href equal to the real URL.
- Chat message bubbles include icon-only copy buttons that copy the raw stored
  markdown or text content, not the rendered HTML.
- Slash-command tokens such as `/help` and `/docs` render bold in message
  bodies, except inside inline or fenced code.
- `Cmd+Enter` on macOS and `Ctrl+Enter` on Windows/Linux submit the current
  message. `Enter` and `Shift+Enter` insert newlines. Composing text and held-key
  repeats must not submit.
- The Send button uses the green token as its background on hover or while the
  platform's send modifier is held, with dark ink for contrast in both themes.

## Performance Dashboards

- The initial performance-dashboard surface is operator-first. It should expose
  detailed execution, prediction, shadow-policy, model, endpoint, tool, and
  agent insights rather than simplifying the data for a general-user view. A
  separate user-oriented view may be designed later, but is not part of the
  initial implementation.
- Treat the dashboard as a dense analytical workspace, not an onboarding card
  or a wall of decorative KPI tiles. It may use a materially different layout
  from onboarding while retaining the shared typography, color tokens,
  frosted-surface vocabulary, background system, and accessibility rules.
- This section is expected to evolve as real task-performance data reveals
  better visual structures. Record deliberate dashboard-specific departures
  here instead of silently diverging from the common design system.
- Use a wide, nearly full-height frosted workspace with a compact filter and
  health header. Charts should receive most of the available area; avoid
  wrapping every chart in multiple nested cards.
- The default overview should combine:
  - a **Prediction River** showing actual executions against P50, P95, and P99
    prediction bands over time,
  - a log-scaled **Reality Map** plotting predicted against actual resource use,
    with the perfect-fit diagonal and dangerous underprediction region visible,
  - a **Surprise Radar** showing persistent signed residuals, interval misses,
    change points, out-of-distribution executions, and statistically unusual
    endpoint/model/agent/task-family interactions without flattening them into
    one generic anomaly score,
  - a **Stop Decision Matrix** separating productive continuations, correctly
    stopped runaway work, incorrectly stopped productive work, missed runaway
    work, and outcomes that remain indeterminate,
  - a **Shadow Lab** comparing frozen predictor and policy candidates by
    coverage, premature-stop risk, useful runaway stops, headroom, cost, and
    sample sufficiency,
  - an endpoint/model comparison matrix that keeps provider endpoint, model,
    model revision, reasoning profile, and observed endpoint health distinct,
  - a task-anatomy timeline separating queue, model, tool, child critical path,
    external wait, checkpoint, recovery, and stopped/terminal intervals, and
  - a learning timeline showing observation windows, training runs, shadow
    promotions, drift, canaries, enforcement changes, and rollbacks.
- Persistent prediction error should be visible by direction, magnitude,
  duration, affected cohort, sample size, and first suspected change point.
  Distinguish a sustained bias from a single high-severity surprise and from an
  expected rare tail event.
- Include an **Investigation Workbench** that presents automatically opened and
  operator-requested cases with severity, deduplicated recurrence count,
  hypotheses, experiment plan, bounded resource use, reproduction attempts,
  evidence, candidate root causes, remediation, shadow validation, and closure
  confidence. An investigation remains visibly unresolved when evidence is
  insufficient; do not turn lack of reproduction into a successful closure.
- Investigation views must distinguish organic task executions from synthetic,
  replayed, sandboxed, and live experimental runs. Experimental results must
  not silently enter ordinary training cohorts or production success metrics.
- A raw stop count is not a success metric. Correct intervention on runaway work
  should read as a positive outcome, while interruption of productive work is a
  high-severity miss. Every stop visualization must expose the decision reason,
  confidence, evidence, counterfactual status, avoided resource estimate, and
  whether later completion proved or disproved the recommendation.
- Use ocean blue for predictions, green for verified outcomes inside the
  intended envelope and useful runaway intervention, gold for deliberate
  headroom, red for harmful underprediction or interruption of productive work,
  and neutral gray for censored or non-comparable outcomes. Never rely on color
  alone; pair states with shapes, labels, line styles, or patterns.
- Filters must support time range, task family, task/run, agent type, agent
  instance when authorized, execution environment, provider endpoint, model and
  revision, reasoning profile, tool family, orchestration shape, policy version,
  predictor version, outcome, and shadow/canary cohort. Active filters remain
  visible and serializable so a view can be shared or discussed without hiding
  its statistical context.
- Cross-filtering should make the workspace feel cohesive: selecting a point,
  cohort, model, endpoint, or stop outcome updates related panels and exposes a
  clear reset affordance. A selected task can open a drill-down drawer without
  destroying the broader comparison context.
- Show sample count, observation window, confidence or uncertainty, censored
  share, data freshness, and relevant predictor version beside every aggregate.
  Never present a precise-looking score without the context needed to judge it.
- The dashboard may animate new points, confidence-band changes, and panel
  transitions gently. Keep the polygon background stable during filtering and
  navigation, and provide a fully static reduced-motion experience.
- Custom chart tooltips must be keyboard reachable, remain within the viewport,
  use readable local times, and expose the same evidence available to pointer
  users. Important values must also be available in a table or accessible text
  representation.
- Include a persistent but collapsible **Ask Performance** analyst panel. The
  operator can ask a live agent or model about the current view, selected chart,
  cohort, task, or anomaly without manually restating the active filters.
- The analyst panel must show which view state and data timestamp will be sent,
  identify the responding model/profile, cite the aggregate queries or run ids
  supporting its answer, and distinguish measured facts from interpretations.
  It may create temporary charts, comparisons, or annotations inside the
  workspace, but it must not silently change runtime settings, deploy a model,
  or promote a policy. Suggested changes become explicit shadow-experiment
  proposals through the trusted operator workflow.
- The analyst may explain or add a hypothesis to an investigation, request a
  bounded reproduction plan, and summarize experimental evidence. It must not
  declare root cause or close a case without the independent evidence and
  validation required by the investigation lifecycle.
- Dashboard queries and analyst tools are read-only, owner/team scoped, bounded
  by query time and row limits, and served through an application API rather
  than browser-held database credentials or unrestricted model-authored SQL.
- When adaptive execution is disabled, show the intentional off state without
  alarm styling. When statistics storage is unavailable, show that optimization
  is paused, tasks are using static execution behavior, and any displayed data
  is a clearly timestamped last-known snapshot.

## Docs

- Docs live under `/docs/` on the same internal website host as `/webchat`.
- Keep the Material docs navigation and page layout recognizable; adjust the
  visual treatment rather than replacing the docs IA.
- Docs use a static canvas-rendered polygonal background derived from the same
  saved appearance values and low-poly geometry logic as onboarding and Web Chat.
- Docs must not rebuild or animate the background when switching topics;
  navigation should feel like moving across a stable reading surface.
- On desktop, keep the docs center content pane substantially wider than the
  original Material default, targeting roughly a 1.5x wider reading/card area
  while preserving the left navigation and right table-of-contents columns.
- Main docs content, sidebars, headers, search, cards, and generated docs panes
  use frosted translucent surfaces with the shared Inter font.
- Dynamic docs pages must keep the same frosted header strip, independent
  sidebar card, main content card, and table-of-contents card geometry as the
  static docs pages.
- Docs navigation, active states, and search controls must use dark readable
  text over pale/translucent fills; avoid light text on light highlight states
  and avoid dark shaded title bars.
- Dynamic skill docs and generated-site indexes use the same docs route prefix:
  `/docs/skills/` and `/docs/sites/`.
- The skill index separates workspace skills, verified managed installations, and
  **Built-in runtime skills**. Built-in guides use the same cards and detail
  layout, identify their source, and explain that documentation does not grant
  tools or imply every agent has the skill enabled. Explicit source links must
  keep selecting that source when names overlap. Workflow progress links use
  `/workflows/<id>`; skill pages explain procedures.
- Skill-index cards show compact previews rather than allowing unbounded
  `SKILL.md` metadata to determine grid height. Visible skill names are capped
  at 48 characters and descriptions at 160 characters, including the ellipsis;
  the canonical name, full description, slug, and detail-page content remain
  unchanged. Card headings use at most two lines and descriptions at most six
  lines as a viewport-dependent containment fallback.
- Generated-site index cards use the human-readable title from each site's
  manifest. Keep the canonical site id in the link target and storage path,
  not in the primary visible heading; legacy sites without manifest metadata
  derive a readable title from the final site-id slug.
- Generated-site card headings must remain contained within their cards and
  wrap safely even when a user-supplied title contains an unusually long token.
- The Docs brand/UA icon opens Web Chat at `/webchat`.
- Do not keep legacy top-level docs routes such as `/skills/`, `/sites/`, or
  `/auth/<provider>` on the internal website host.

## Generated Sites

- Generated sites share the Docs background, frosted surfaces, typography,
  spacing, embedded chat, and responsive behavior.
- The sites index at `/docs/sites/` lists only the signed-in person's pages.
  Without a Web Chat session it lists no pages and shows a short sentence with a
  sign-in link to Web Chat.
- A page opened from a link works without signing in, so its owner can share
  it. Its embedded chat needs the owner's Web Chat sign-in; until then, the chat
  panel says so and offers the sign-in link while the page stays readable.
- Choose the page kind from the information shape. Do not turn every generated
  site into a checklist or numbered walkthrough.
- Article sites may use content sections, key-point lists, highlight cards,
  callouts, and references.
- Comparison sites may use option cards, criteria tables, pros and cons,
  best-fit notes, recommendations, and sources.
- How-to sites may use prerequisites, materials, tools, safety callouts,
  ordered steps, verification notes, and browser-local completion controls.
- App sites may use stat cards, counters, progress controls, task or habit
  checklists, action links, and supporting content sections. Browser-local
  state must be clearly treated as convenience state rather than a durable
  team record.
- These UI elements are optional building blocks. Render only elements backed
  by useful content; do not add empty cards, fake metrics, redundant callouts,
  or decorative controls merely to fill a template.

## Device OAuth During Onboarding

- Treat device OAuth as an inline runtime-setup state, not a separate visual
  application: keep the provider, connection action, user code, countdown,
  status, and connection test in the existing Runtime Basics card.
- Open the provider authorization page from the user's explicit connect action
  so browser popup protection does not turn a successful device request into a
  dead end. Always keep the verification URL and user code visible as a manual
  fallback.
- Use the shared ocean-blue primary action and compact status copy. Reserve
  error color for an actual denied, expired, or failed session; pending status
  should not visually compete with the rest of the form.
- The browser may receive only display-safe device-flow fields such as the
  verification URL, user code, expiry, and status. Device codes, access tokens,
  refresh tokens, and token-file contents remain server-side.
- Stop polling and clear countdown timers when the user leaves the runtime
  step, changes providers, or the session reaches a terminal state.

## Do

- Keep copy readable over all background colors.
- Use ocean blue as the main action and focus color.
- Keep controls aligned and stable while helper text appears.
- Persist user appearance preferences so post-reboot surfaces match onboarding.
- Prefer reusable CSS classes and shared UI helpers for repeated components.
- Show every error in red at the top of the part the person was working in,
  with what failed, why and what to do (see Errors and Results).

## Do Not

- Do not expose preview/export controls in production onboarding.
- Do not use native browser tooltips for core explanations.
- Do not use dark dashboard styling as the dominant visual theme.
- Do not let dynamic helper text move neighboring controls out of alignment.
- Do not add ornamental blobs, gradient orbs, or decorative nested cards.
- Do not make headings or controls rely on washed-out gray text.
- Do not show an error only at the top of a long page, in a disappearing
  toast, or as plain uncolored text.

## Objective Work Dashboard

- Use the live manager's canonical-user projection for current Objective state
  and controls. Browser state and Activity Collector projections are never
  canonical.
- Present outcome, evidence-based progress, current focus, success criteria, and
  Plan subgoals before runtime diagnostics.
- Distinguish continuous and time-boxed execution with compact badges. When a
  policy exists, show the profile, next run, latest session state, turns/tokens,
  checkpoint health, and next subgoal.
- Show cumulative session tool-call usage, material-work allowance, and
  checkpoint-only reserve as separate values. The counter does not reset per
  model turn, and the reserve is part of the profile total.
- Keep the layout light, responsive, and information-dense using shared system
  typography, ocean-blue focus color, restrained borders, and frosted panels.
- Render only actions returned by the server for the exact resource version.
  Pause and stop remain distinct: pause prevents future managed work, while
  stop also cancels current work at a safe boundary. Abandon and delete use a
  second in-place confirmation click; recoverable deletion retains audit and
  evidence history.
- Schedule cards expose pause-until, resume, disable, and delete when permitted.
  MCP cards expose exact connection attachment and hosted-server lifecycle
  controls without showing secrets or accepting arbitrary endpoints.
- Schedule cards show the manager-projected assigned agent type and any exact
  agent identity. Label missing type information **Not specified**. Show automatic
  creation as **Enabled by TeamLead**, **Disabled by TeamLead**, or **Not configured**,
  independently of the schedule lifecycle badge. Explain that enabled creation
  remains subject to current access and launch limits. Show a pending reassignment
  beside the current type until its claim ends. Use the existing wrapped metadata
  layout; never infer automatic creation from live agent presence.
- Default local startup policies show **Automatic creation: Allowed by default
  policy**. Lead-configured policies retain their enabled/disabled labels. These
  describe saved startup permission, not live worker presence or tool access.
- System memory-review cards show **Review agent: MemReviewAgent**, **Managed by:
  Local scheduler**, and **Agent startup** with the same policy labels. Old timers
  show **Upgrade required: Move this timer to the local scheduler** until the
  explicit upgrade changes their assignment. Exact recipients are **Specific
  agent**. Keep all assignment details in the existing wrapped metadata layout.
- Do not invent completion percentages. Progress must come from durable evidence
  summaries and checkpoint deltas.

## Offline installation guides

Setup guides embed the static light mesh, the dark background renderer and the
agent mark so a downloaded HTML file works without external assets. The
embedded dark renderer is an exact copy of
`assistant_gateway/onboarding_web/assets/metallic_mesh.js`; a test fails when
the two differ. Keep OS prerequisites and generated commands
together, expose only relevant initial settings, and collapse optional GitHub
authentication. Desktop and runtime release versions remain independent. Never
collect credentials in the guide or store configuration in browser storage.

Keep the full setup guide distinct from the concise quickstart. It includes
provider signup, non-secret model settings, optional search and channel setup,
and separate local-installation and hosted-service paths. Hosted clients need
only a browser; only service operators see deployment instructions. Label the
UA public origin separately from the model API endpoint. Invalid URLs clear
generated links and commands. Keep account-specific setup in expandable sections
without replacing the instructions with links alone.

The runtime repository owns the single comprehensive setup guide and publishes
it as `setup-guide.html`. Desktop docs and releases link to that download.
Only the active theme renderer may draw the background, including after resize
and system-theme changes. Expandable sections use tinted, bordered headers,
a visible disclosure chevron and Show/Hide steps labels. Animate user opening
and closing over 220 ms; reduced-motion users get immediate native toggles.
Keep keyboard activation, nested panes, rapid toggles, and deep links usable.

### Status colors and repeated records

- Schedules show Enabled in green, Paused in amber/yellow, and Disabled in slate.
  Keep the status word visible; color supplements the label.
- A stuck schedule (enabled, overdue and not picked up) shows **Not running**
  in the danger color instead of Enabled, and a `warning-callout` under its ID
  (the "!" icon, then both lines in one text block: the callout is a
  two-column grid, and two loose paragraphs once squeezed the explanation to
  one letter per line) saying since when and, in plain words, what is wrong
  and what to do; the
  text is the same as in the notice its owner receives
  (`custom_agents.stuck_schedules.not_running`). Owner's request 2026-10-04,
  after stuck schedules showed as Enabled for a day.
- Schedule Resume/Enable actions use green, Pause uses amber, Disable uses slate,
  and Delete uses red. Memory Edit uses the standard blue primary style; Delete
  uses the shared danger style, including the existing confirmation step.
- Alternate repeated memory rows, Inbox and Access cards, and Team table rows
  with opaque fills. Dark mode pairs dark gray-blue (#101c24) with muted teal
  (#18343b); light mode pairs near-white (#f4f8f9) with pale teal (#e0efed).
  Keep borders, metadata and tags readable over both fills.
- Use the shared list-row and status CSS tokens so both themes receive the same
  semantics. Hover preserves the action's meaning; keyboard focus has a visible
  outline. Disabled controls retain their disabled treatment.
- Styling follows existing server-provided statuses and available actions. It
  does not change scheduling, authorization, confirmation or agent workflows;
  runtime skill instructions need no adjustment for these presentation changes.

## Access Requests in Chat Channels

- Each waiting request is announced once, in one chat: the task's own
  conversation, including its topic or thread, when the person there can decide
  it; otherwise the owner's proactive conversation when that person can decide
  it. Otherwise it waits in the Inbox only. Do not create a separate approval
  channel or send the same announcement to every connected channel.
- The announcement uses the same plain words as the Inbox card: who asks for
  what, for how long, what it allows, and why. A staged change is announced in
  one line: "<agent> asks for approval: <summary>", where the summary is written
  by trusted code from the plan's own values, such as "Switch TeamArchitect to
  deepseek/deepseek-v4.1-flash via openrouter (images on). Takes effect now."
  Name only values that change and end with when it takes effect ("Takes
  effect now.", "Takes effect at each running agent's next turn.", "Takes
  effect after a manager restart."). Never use agent text
  or setting registry descriptions for it; the full effect list stays in the
  Inbox.
- An access request's announcement ends with **What each button does:**, one
  line per button in the gateway's words (as on the card), because chat
  buttons cannot show a card or a hover. A staged change's one-line
  announcement does not.
- Telegram and Discord show native buttons under the announcement. For an access
  request: **Allow for this task**, **Allow and keep**, **Allow reading only**
  (only when more than read access was asked), and **Deny**. **Allow and keep**
  approves with **Keep for later tasks** turned on. For a proposal: **Approve
  all as proposed** and **Deny**. Both end with an **Open Inbox** link button,
  shown only when the Web Chat address uses HTTPS.
- A press carries only the request id and the choice. The gateway looks up the
  person linked to the pressing chat account and decides with that person's
  current roles. A press from an unlinked account decides nothing; the reply
  explains how to link the account or to decide in the Inbox.
- After a decision, remove the buttons and add the outcome and who decided to
  the message, such as "Allowed for this task by Dana." When the request was
  already decided elsewhere or its task ended, remove the buttons and say so,
  such as "This request was already approved." or "This request is no longer
  waiting: its task ended."
- Other channels show the announcement text with an **Open Inbox** link. In
  Web Chat the announcement is a System message that shows the review card
  below (Access Review Cards in Web Chat), and a browser alert for it says that
  attention is needed.
- Access requests have no deadline. Show no countdown, expiry time, or reply-by
  wording on announcements, Inbox cards, or chat buttons. A request waits until
  someone decides it or its task ends.
- Typed replies never decide a request. A bare number, "approve", or "yes" in
  chat does nothing to a request; only a button press, the Inbox, or the
  workflow page's **Review Ready Requests** dialog decides.

### Access Review Cards in Web Chat

- An access review announcement in Web Chat shows the Inbox's review card in
  its compact form inside the System message, in place of the announcement
  text (**Copy raw message** still copies that text). The card sits inside the
  bubble without its own card surface, separated by a thin top rule, so cards
  never nest.
- The heading is the request's one-line summary, with the status badge on the
  right: **Needs your decision** when this person can decide, otherwise
  **Waiting for an administrator** and no buttons or note.
- Warning chips (**Full access**, **Runs code**, **New host**, ...) and the
  lacks-access warning stay visible under the heading. Everything else is
  folded under a **Details** toggle, closed by default: the full title, the
  details list, the rows with their **Keep for later tasks** boxes and **How
  long** selects, **What this changes**, **Why (from <agent>)** and **Note
  (optional)**.
- The buttons are the Inbox card's, with the same server labels, busy states
  and request body, and they decide through the Inbox decide API with the
  signed-in session and CSRF header. After a saved decision the card collapses
  to the decided line with the outcome, as in the Inbox.
- The card reads its request from the gateway by id, so after a reload or a
  decision made elsewhere it shows the stored outcome and who decided, such as
  **Approved for this task** with "Decided by Dana on <local time>.". A request
  still waiting is read again at most every 15 seconds when the transcript
  redraws; a decided one is never read again.
- While the request loads, the card says "Loading this request…". When it
  cannot be read, it says why in plain words and offers **Open Inbox**
  (secondary), which opens Settings at that request.

### Chat live connection

- Keep one authenticated server-sent event connection per open Chat page. Show
  **Live updates connected**, **Connecting live updates…**, or
  **Updates disconnected. Reconnecting…** beside the account controls. Status
  changes use a polite announcement; repeated heartbeats do not repeat it.
- Reconnect after a missing heartbeat or connection failure and resume from the
  last successfully applied durable event page. Refresh saved history when the
  server rejects the cursor. Never create a conversation during recovery.
- Keep message identity, unread indicators, and reading position stable during
  replay. Stop the connection at sign-out or page disposal and reject callbacks
  from previous connections. Live delivery must not show an announcement twice.
- An access request announcement goes only to the chat chosen for it (see Access
  Requests in Chat Channels). Live Chat delivery does not copy another channel's
  announcements into this transcript.
