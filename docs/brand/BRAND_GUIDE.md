# Kuratorr Brand Kit

## Recommended direction

Option C: a monogram record crate. It communicates three things at once:

- a curated music collection
- stored local media
- a private cataloging tool

The crate is the primary mark. The record-disc version can be used as a secondary “now playing” indicator.

## Fonts

No font files are included.

- **Marcellus** — wordmark and major headings
- **Manrope** — UI, navigation, labels, forms, tables and body text
- **IBM Plex Mono** — filenames, paths, logs and technical metadata

Suggested CSS imports are included in `kuratorr-theme.css`. Self-host the fonts or load them through your preferred font provider.

## Logo usage

- `kuratorr-logo-horizontal-dark.svg`: use on dark headers
- `kuratorr-logo-horizontal-light.svg`: use on light headers
- `kuratorr-lockup-*.svg`: horizontal logo with tagline
- `kuratorr-icon-dark.svg`: app icon on dark green
- `kuratorr-icon-light.svg`: light-mode/marketing variant
- `favicon.svg`: preferred modern browser-tab icon
- PNG/ICO versions: fallbacks and device icons

Keep clear space around the mark equal to roughly the width of the letter K.

## Motion

Keep the logo static by default. A tiny record rotation on hover is appropriate:

- 6–8 degrees
- 350–450 ms
- ease-out / spring-like easing
- never spin continuously
- disable under `prefers-reduced-motion`

The included CSS already implements a restrained version.

## Theme application

Use bronze as the navigational highlight and selection color.
Use clay sparingly for:
- warnings that are not errors
- notable-track badges
- enrichment activity
- small visualization accents

Do not use clay for every primary button. Forest/moss should remain the structural brand color.

## Suggested navigation treatment

Dark mode title bar:
- background: `#0F1312`
- logo/icon: bronze `#B8854A`
- text: parchment `#E9E3D6`
- active item: bronze with a 2px underline
- separators/borders: `#2B322F`

Light mode title bar:
- background: `#F0E8D9`
- logo/icon: bronze `#B8854A`
- text: ink `#1A1C19`
- active item: moss `#5A6B4E`

## Accessibility

Use:
- light text on dark background
- ink text on parchment/cream
- bronze mostly for icons, borders, and larger text
- clay as an accent rather than body text

Confirm final contrast ratios after integrating with the real components.
