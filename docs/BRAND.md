# GrabLine brand

The desktop app, the website (grabline.github.io) and the Android app share
these tokens. The source of truth is the logo (`app/ui/assets/logo.png`, exactly `#2563EB`).
In code they live in `app/ui/design.py`.

| Token | Light | Dark |
| --- | --- | --- |
| Brand blue (fills, buttons) | `#2563EB` | `#2563EB` |
| Hover / pressed | `#1D4ED8` | `#1D4ED8` |
| Accent text (links, selected tab) | `#1D4ED8` | `#86AAFF` |
| Background | `#F3F5F9` | `#07090E` |
| Surface / panel | `#FFFFFF` | `#121821` |
| Sidebar | `#EAEEF4` | `#0C1018` |
| Border | `#DBE0EA` | `#1E2736` |
| Text | `#161B24` | `#E8EEF8` |
| Muted text | `#5A6473` | `#A4B0C4` |

- Fonts: IBM Plex Sans for UI text, IBM Plex Mono for numbers, sizes and
  speeds (SIL OFL 1.1, bundled in `app/ui/assets/fonts/`).
- Radius: sm 4, md 6 (buttons, inputs, cards), lg 8, pill 999.
- Icon: `app/ui/assets/logo.png`.

The Android app uses the same tokens.
