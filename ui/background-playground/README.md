# Dark background lab

A page for tuning and inspecting the dark metallic background. It loads the
product renderer, `assistant_gateway/onboarding_web/assets/metallic_mesh.js`,
so what you see here is what Web Chat, onboarding, Docs and broker pages draw.
Nothing in the product loads this page.

Serve the repository root (the page loads the renderer by a relative path)
and open the lab:

```bash
python -m http.server 18100 --bind 127.0.0.1
```

Then open `http://127.0.0.1:18100/ui/background-playground/`.

The controls use the same names and ranges as
`ui.onboarding.appearance.dark` (camelCase here, snake_case in
`instance_config.yaml`). **Share settings** shows the current values in the
same YAML as Customize Theme's Share settings (it fits under
`ui.onboarding.appearance` in `instance_config.yaml`), so text copies between
the lab, Web Chat and onboarding; **Apply** tries pasted settings.
**Sample Pane** shows a frosted window over the background to judge
readability.

Test switches in the address:

- `?nowebgl` hides WebGL2 from the renderer, which then draws the basic
  background and logs a warning.
- `?light2d` takes a 2D context on the page canvas first, as the light theme
  does in the product; the renderer still draws on its own layer.

Two helpers support measurements from the browser console or automation:

- `labSizeStats()` reports how evenly the faces are sized (each face against
  its neighbors and the median face) and the narrowest visible corner.
- `labCapture(name)` posts the current frame to `/upload` on the serving host.
  Only a capture-aware server saves it; a plain static server rejects it.

The renderer's `UAMetallicMesh.lab` hooks (`stats`, `world`, `freeze`,
`advance`) let a script stop the clock, step the moving mesh by a number of
seconds, and read face counts, reconnections and chrome moments.
