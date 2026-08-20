# Third-party notices

`footy/site/static/style.css` transcribes a subset of the design tokens
(color primitives, semantic success/error/warning colors, font family,
type scale, border-radius scale, and the `:focus-visible` treatment) and
CSS patterns published by Japan's Digital Agency (デジタル庁) as part of
its public design system. No logo, emblem, or other government branding is
used or reproduced -- only the design tokens and generic CSS utility
patterns below.

## @digital-go-jp/design-tokens

Source: https://github.com/digital-go-jp/design-tokens

```
MIT License

Copyright (c) 2023 デジタル庁

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## digital-go-jp/design-system-example-components-html

Source: https://github.com/digital-go-jp/design-system-example-components-html
(`src/global.css` -- the `:focus-visible` outline/box-shadow rule and link
color states were adapted from this file)

```
MIT License

Copyright (c) 2025 デジタル庁

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## What was and was not taken

- Taken: hex values for the blue/gray/semantic color primitives, the
  `font-size`/`line-height`/`border-radius` scale values, the font-family
  stack name (`Noto Sans` -- loaded via the system font stack here, not
  Google Fonts, to keep `footy/site` free of external network requests per
  `docs/DESIGN_SITE.md` 2.1), and the `:focus-visible` outline pattern.
- Not taken: any Digital Agency logo, emblem, favicon, or component markup;
  no claim of affiliation with or endorsement by the Digital Agency is made
  or implied anywhere on this site.
