# Weaverbird — visual identity

Sleek, modern and minimalist, and intuitive before it is decorative.
The identity is a design system, not a logo file: tokens live as code
inside the app (`src/weaverbird/assets/design_tokens.json`, with the
wordmark in `src/weaverbird/assets/weaverbird.svg`), so the CLI, the
future app mode, and anything the factory generates for a game — store
pages, capsules, documents — all use the same rules. Print the summary
any time with `weaverbird identity`; export the tokens and mark with
`weaverbird identity --export <dir>`.

## Colour

Light first. Dark themes and gloss read as outdated here.

- Background: off-white with a hint of lavender, `#F7F6FA`.
- Surfaces: plain white, `#FFFFFF`.
- Text: dark slate, `#232936`. Secondary text: muted slate, `#68707F`.
- Border, used sparingly: `#E5E3EE`.
- One accent, used for the one primary action and for marks of
  progress: vivid violet, `#6C4CF1`. Its soft tint `#ECE8FD` is for
  quiet highlights behind the accent, never for large areas.
- Signals, muted and only when they mean something: success `#2F9E6E`,
  warning `#D9932E`, error `#D24545`.

No other colours. If a screen seems to need a new one, the layout is
probably doing too much.

## Typography

One clean sans everywhere — Inter where available, otherwise the
system sans. No decorative fonts, no second family for headings.

- Display 32, title 24, large 18, body 15, small 13.
- Line height 1.5. Sentence case for headings and labels; short words.
- Numbers and statuses line up — tabular figures where the platform
  offers them.

## Shape and depth

- Rounded corners from the token scale: 8 small, 12 medium, 16 large,
  pill for tags and the primary button where a pill is honest.
- Depth is one soft shadow under a lifted surface:
  `0 8px 24px rgba(35, 41, 54, 0.08)`. That is the whole depth system.
- No gradients. No gloss or shine. No borders everywhere — whitespace
  separates, lines rarely do.

## Layout

- Lots of whitespace. Spacing comes only from the scale:
  4, 8, 12, 16, 24, 32, 48, 64. Nothing in between.
- One primary action per screen, in the accent. Secondary actions are
  plain text or white surfaces; they wait quietly.
- Progressive disclosure: a screen shows the current stage, its one
  next step, and what it needs. Detail lives one level down, behind a
  deliberate click — pipelines, ledgers and logs are never dumped on a
  first screen.
- Status is shown the same way everywhere: a short label, then the
  detail, then the next step. In the terminal that is the consistent
  ✓ (done/ok), ✗ (missing/blocked) and – (optional) markers, with
  quiet headings and aligned columns, and no colour codes forced on
  output that is not a terminal.

## Copy

Plain, short, human. Say what happened, what it means, what to do
next. Errors always name the next step — ideally the exact command.
No hype, no exclamation marks, no spec-sheet numbers as decoration.

## The mark

A minimal woven bird: one flat shape in the accent, a single white
eye, a quieter second stroke suggesting woven strands. The wordmark
is the name in the same sans, semibold, lowercase. The mark never gets
a gradient, a shadow of its own, or a redraw in another style.
