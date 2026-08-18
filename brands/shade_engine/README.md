# Brand assets

Source (`icon.svg`) and rasterized icons for the integration. `icon.png`
(256×256) and `icon@2x.png` (512×512) match the layout Home Assistant's
[brands repository](https://github.com/home-assistant/brands) expects under
`custom_integrations/shade_engine/` — the integrations page loads its images
from `brands.home-assistant.io`, so the icon only appears in the UI once a
copy of these files is merged there.

Regenerate the PNGs from the SVG with headless Chrome:

```sh
chrome --headless --default-background-color=00000000 \
  --window-size=256,256 --screenshot=icon.png icon.svg
chrome --headless --default-background-color=00000000 \
  --window-size=512,512 --screenshot=icon@2x.png icon.svg
```
