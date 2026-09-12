# Design

<!-- impeccable:design-schema 1 -->

## Visual World

**ZenGrid, in Safaqat's colors.** A calm, grid-perfect system built on breathability and visual silence, carrying the product's existing deep-navy / crimson / warm-stone palette rather than ZenGrid's stone-and-sage neutrals.

Procurement work is register work: references, buyers, deadlines, lots, pieces. The interface organizes that register with precision and then recedes — hairline rules and background shifts instead of shadows, generous vertical air instead of boxes-within-boxes, light display type instead of bold.

Surfaces are flat by definition. There is no elevation scale in the Tailwind theme (`boxShadow: { none }`), so a shadow cannot be reintroduced by habit.

## Mode

**Operate.** Enterprises scan a shortlist, open an offer, and read its documents. Scanability, consistency, and the real usage scene outrank expression. Brand lives in the precision of the details: the hairline grid, the mono numerals, the single accent.

## Colors

Existing Safaqat colors, mapped onto ZenGrid's roles.

| Role | Token | Value |
|---|---|---|
| Content primary | `ink` | `#172033` |
| Content secondary | `ink-muted` | `#536077` |
| Content tertiary | `ink-faint` | `#5F6A7C` |
| Surface base | `canvas` | `#F6F5F1` |
| Surface raised | `canvas-raised` | `#FFFFFF` |
| Surface sunken | `canvas-sunken` | `#EDECE7` |
| Surface ink | `bg-ink` | `#172033` |
| Border default | `line` | `#DCE0E7` |
| Border strong | `line-strong` | `#C2C9D5` |
| Accent / focus | `accent` | `#B9342D` (hover `#922720`, active `#7A1F1A`) |
| On ink | `onink-muted` / `onink-faint` | `#A9B4C9` / `#7F8CA5` |
| On accent | `onaccent-muted` | `#F3D6D3` |
| Success | `state-success` | `#0F6B44` |
| Warning | `state-warning` | `#8A5A05` |
| Error | `state-error` | `#A42017` |

Every pair clears 4.5:1 on the surface it is used on. Text on a colored surface is tinted from that hue (`onink-*`, `onaccent-*`), never gray.

Semantic colors are functional only: deadline urgency, validation, request outcomes. Match reasons, categories and profile criteria are neutral or accent chips, never green "success".

## Typography

- **Display** — Raleway. `h1` 40px/300, `h2` 32px/300, `h3` 24px/500, `h4` 18px/500. `h1`/`h2` clamp down on small screens.
- **Body** — DM Sans 15px/1.7/400. Emphasis is 500; there is no bold body text.
- **Data** — Fira Code 13px, tabular numerals (`.numeric`): references, deadlines, file sizes, rank, percentages.
- **Metadata** — `.label`: 11px/500, `0.08em`, uppercase, tertiary.
- Measure caps at `max-w-measure` (68ch). Text is left-aligned, including empty states.

All three families are self-hosted woff2 in `srcs/frontend/fonts/` (latin + latin-ext) because the CSP allows same-origin assets only.

## Spacing

Base unit 12px: **6 · 12 · 24 · 36 · 48 · 72 · 96 · 120**. Card padding 24px (36px ≥640px); major sections separate by 48–72px; related items sit 6–12px apart. More space above a heading than below it.

## Shape

`radius-sm` 2px (badges, small controls) · `radius-md` 4px (cards, buttons, inputs, chips) · `radius-lg` 6px (modal) · `radius-none` (the document viewer's iframe and any image). Nothing else exists in the theme.

## Components

Defined in `srcs/frontend/styles.css` under `@layer components`:

`.card` / `.card-elevated` · `.offer-row` · `.button-primary|secondary|ghost|destructive|inverse|on-ink` (+ `.button-sm`, `.button-lg`, `.button-icon`) · `.field` + `.field-label|hint|error` · `.tag-field` · `.chip` + `.chip-neutral|accent|success|warning|error|ink|on-ink` · `.chip-filter` (tabs and filters, active via `aria-pressed` / `aria-selected`) · `.list-row` · `.control` + `.control-check|radio` · `.notice` + `.notice-error|success|warning` · `.link-quiet` / `.link-accent` · `.skeleton`.

Separation is a 1px border or a background shift — never a shadow, never a nested card.

## Motion

Almost none, by design. The loading skeleton breathes between raised and sunken over 2.4s — a surface shift, not a moving object, so nothing scrolls or sweeps across the page. Everything else is a 150ms color, border, or background transition on interactive states. `prefers-reduced-motion` flattens all of it.

## Browser Surfaces

Selection, caret, focus ring, scrollbar, select chevron and underline offset are all themed from the palette. The focus ring is a 2px accent outline offset by 2px.

## Do / Don't

1. **Do** let the grid dictate layout; nothing breaks the column rhythm.
2. **Don't** add shadows, glows, or gradients-as-decoration.
3. **Do** keep display type light (300) at `h1`/`h2`.
4. **Don't** use color outside the tokens above for UI; semantic hues are reserved for functional states.
5. **Do** keep images and document previews unadorned — square corners, no border, no overlay.
6. **Don't** center-align blocks of text.
7. **Do** set metadata in `.label` and data in `.numeric`, so the register reads as a register.
8. **Don't** stand filler bars in for content that does not exist yet; say what is coming in words.
9. **Do** keep the heading outline unbroken (h1 → h2 → h3); set visual size with the type tokens, never by choosing a different heading level.
