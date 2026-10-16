# Architecture diagrams

The README embeds the PNGs. The `.mmd` files are the editable source. A node written as `{{lambda|Webhook|Signature and allowlist}}` is drawn with that AWS architecture icon and the two lines of text.

From the repository root:

```powershell
.\.venv\Scripts\python.exe scripts/render_diagrams.py
```

That needs Node.js. It fills in the icons from `icons/` and writes the PNGs. Check the images for readable labels and arrows that meet the right boxes before committing.

The pictures in `icons/` are the AWS Architecture Icons, used here only to draw these diagrams.
