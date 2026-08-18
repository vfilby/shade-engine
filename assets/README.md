# Brand assets

`icon.svg` is the source for the integration's brand images. The rasterized
copies live at `custom_components/shade_engine/brand/icon.png` (256×256) and
`icon@2x.png` (512×512): since Home Assistant 2026.3, brand images shipped in
a `brand/` directory inside the integration are served locally and take
priority over the brands CDN, so no submission to
[home-assistant/brands](https://github.com/home-assistant/brands) is needed
(that repo stopped accepting custom-integration PRs in early 2026).

Regenerate the PNGs from the SVG with headless Chrome:

```sh
chrome --headless --default-background-color=00000000 \
  --window-size=256,256 --screenshot=icon.png assets/icon.svg
chrome --headless --default-background-color=00000000 \
  --window-size=512,512 --screenshot=icon@2x.png assets/icon.svg
```
