# ADR 0090: Accessibility WCAG 2.2 AA

- **Status:** Accepted
- **Date:** 2026-10-21
- **Decided by:** Architect
- **Specified in:** 10-web-client.md, 84-testing.md, 86-operations.md
- **Related ADRs:** 0082, 0084, 0085, 0087, 0089, 0092, 0093, 0094

## Context

- The web client meets WCAG 2.2 level AA: it is fully keyboard operable, and streamed answer text is announced through a polite live region. A streamed answer changes many times a second, so announcing the container that re-renders would flood a screen reader or repeat itself.
- Vendor facts (checked 2026-10-14): WAI-ARIA 1.2 presents polite updates at the next graceful opportunity, reserves assertive for imperative interruptions, and gives role `log` an implicit polite live setting; MDN advises creating a live region empty before its first update; WCAG 2.2 success criterion 4.1.3 (Status Messages) is met with `role="status"`, `role="log"` or live regions, and using an assertive region for content that isn't important and time-sensitive is a documented failure; Fluent's guidance says never to put live-region attributes on elements that change often or hold many children, such as a whole chat message list, and that custom live regions must exist before text is inserted; Fluent's `AriaLiveAnnouncer` falls back to a visually hidden assertive region when `document.ariaNotify` is missing, and `document.ariaNotify` became Baseline only in September 2026; Fluent v9 components aim at WCAG and WAI-ARIA but don't make an application accessible on their own, and app-level focus management is the app's job.

## Decision

1. **Target.** WCAG 2.2 level AA. The criteria the app's own code can break are met this way:

   | Criterion | How |
   |---|---|
   | 1.1.1 Non-text content | Icon-only buttons have accessible names; decorative icons are `aria-hidden`; answers never contain images |
   | 1.3.1 Info and relationships | Landmarks `header`, `nav` (main navigation), `main`, and `aside` for the session panel and the citation viewer; one `h1` per screen (visually hidden on the chat screen); tables with header cells and captions; lists as lists |
   | 1.3.2 Meaningful sequence; 2.4.3 Focus order | DOM order equals visual order at every breakpoint |
   | 1.4.1 Use of colour | Every badge and status has text; error bars have an icon and text |
   | 1.4.3 Contrast; 1.4.11 Non-text contrast | Fluent tokens only, checked by axe in both themes |
   | 1.4.4 Resize text; 1.4.10 Reflow; 1.4.12 Text spacing | Works at 200% zoom and at 320 CSS px wide without two-way scrolling (tables scroll in their own regions); no set heights on text containers |
   | 1.4.13 Content on hover or focus | Only Fluent `Tooltip` (dismissed with Escape, hoverable, persistent) |
   | 2.1.1 Keyboard; 2.1.2 No keyboard trap; 2.1.4 Character key shortcuts | Everything works by keyboard (item 4); dialogs and overlay drawers hold focus and close with Escape; no single-character shortcuts |
   | 2.2.1 Timing adjustable | The only limit is the 24-hour sign-in, past the 20-hour exception, and its expiry loses no typed text |
   | 2.2.2 Pause, stop, hide | A streaming answer can be stopped; polled screens have Auto-refresh switches (item 6) |
   | 2.4.1 Bypass blocks | A skip-to-main-content link is the first focusable element |
   | 2.4.2 Page titled | One title per route (ADR 0089) |
   | 2.4.6 Headings and labels; 3.3.2 Labels or instructions | Every field has a visible label, except the composer, whose label is visually hidden and whose placeholder and hint explain it |
   | 2.4.7 Focus visible; 2.4.11 Focus not obscured (minimum) | Fluent focus indicators; `scroll-padding` keeps the focused element clear of the top bar and the composer |
   | 2.5.3 Label in name | Every accessible name starts with the visible text |
   | 2.5.7 Dragging movements | The upload drop zone has a choose-files button; nothing needs dragging |
   | 2.5.8 Target size (minimum) | Every target is at least 24 × 24 CSS px, except citation buttons inside answer text (the inline exception), whose Sources-list counterparts are 32 px high |
   | 3.1.1 Language of page | `<html lang="en">` |
   | 3.2.2 On input | Choosing another project in an open session is announced by a hint before it happens; no other control changes context on input |
   | 3.3.1 Error identification; 3.3.3 Error suggestion | Fluent `Field` with `validationState="error"` and text that says how to fix it; focus moves to the first invalid field |
   | 3.3.4 Error prevention | Confirmation before delete, clear, archive, unmap, remove and re-ingest |
   | 3.3.7 Redundant entry | Typed values survive every error, `412` included |
   | 3.3.8 Accessible authentication (minimum) | Sign-in happens on Entra ID's pages; the app adds no cognitive test |
   | 4.1.2 Name, role, value | Fluent components; citation buttons are native `<button>` elements |
   | 4.1.3 Status messages | The polite live regions of item 2; errors in `MessageBar`s |

2. **Two live regions**, rendered once at the app root inside `FluentProvider`, visually hidden, present and empty from the first paint: `answerLog`, a `<div role="log" aria-live="polite">`, to which streamed answer text is appended sentence by sentence as new `<p>` children, keeping at most 20, oldest removed first; and `statusRegion`, a `<div role="status" aria-live="polite" aria-atomic="true">`, whose text short messages replace (copied, saved, stopped, the broadened search, answer complete, run finished), emptied first and set 50 ms later so a repeated message is announced again. Errors appear in `MessageBar`s with intent `error`. `aria-live` is never put on the message list or an answer container, and Fluent's `AriaLiveAnnouncer` isn't used.
3. **Sentence-by-sentence announcing** (`src/chat/render/speakable.ts`). Each live turn keeps `spokenUpTo`, an index into its text. After each render the client takes the text from `spokenUpTo`, finds its last boundary (`.`, `!`, `?` or `:` followed by white space, or a line break) and waits for more text when there is none; the chunk up to and including the boundary, with citation tokens removed, parsed with `remark-parse` and reduced to its text values with white space collapsed, is appended to `answerLog` when not empty. On `citations` the rest is flushed and the status region announces that the answer is complete with its number of sources; on stop or failure the rest is flushed before the stop or failure announcement. Only turns started in this tab are announced; opening a stored session announces nothing.
4. **Keyboard.** Tab and Shift+Tab move through controls in visual order; Enter and Space activate buttons and Enter follows links. In the composer, Enter sends; Shift+Enter, Alt+Enter and Ctrl+Enter insert a new line; Enter does nothing while an input method is composing (`isComposing`, or `keyCode` 229); Escape stops a streaming answer. In the citation viewer and every dialog or overlay drawer, Escape closes it and focus returns to the control that opened it. Arrow keys work inside `Dropdown`, `Combobox`, `Menu`, `TabList`, `RadioGroup` and `TagPicker` as Fluent implements them. The app defines no global shortcuts.
5. **Focus.** On every route change focus moves to the screen's `h1` (`tabindex="-1"`). Sending a question leaves focus in the composer. Opening the citation viewer focuses its title. A dialog opens on its first control and returns focus to its trigger on close. After a create, focus goes to the new page's `h1`; after a row is removed, to the next row's name, or to the table's heading when none remains; a failed submit moves focus to the first invalid field. The message list's `scroll-padding-bottom` equals the composer's height plus 16 px.
6. **Moving content.** Every polled screen (an ingestion run every 5,000 ms; the runs, documents and index screens every 15,000 ms; a chat session whose newest stored turn is still streaming every 5,000 ms for at most 250 s) has an Auto-refresh `Switch`, on by default and not remembered; polling also pauses while the tab is hidden. `prefers-reduced-motion: reduce` turns the smooth scroll of the jump-to-latest button into an instant jump.
7. **Themes and forced colours.** Fluent `webLightTheme` and `webDarkTheme` with `<meta name="color-scheme" content="light dark">`; under `@media (forced-colors: active)` the few custom styles (the answer body, citation buttons, list markers) use system colour keywords.
8. **Gates.** Every browser test runs `AxeBuilder` (`@axe-core/playwright` 4.13.0) with the tags `wcag2a`, `wcag2aa`, `wcag21a`, `wcag21aa` and `wcag22aa` after each screen state it reaches, and any violation fails the test; together they cover every screen and state in both themes. Before every production release that changes the web client, a manual check runs: a keyboard-only pass of every screen; NVDA with Firefox and with Chrome on Windows and VoiceOver with Safari on macOS (current versions), sending a question and hearing the answer sentence by sentence, opening a citation, giving feedback and editing memory; 200% zoom and 320 px width; Windows contrast themes. The results go into the release checklist, and a failure blocks the release.

## Alternatives considered

- **`aria-live` on the answer container.** Rejected: every re-render would re-announce or flood the screen reader, which WAI-ARIA and Fluent's guidance both warn against.
- **Fluent's `AriaLiveAnnouncer`.** Rejected: without `document.ariaNotify` it falls back to an assertive region, a documented failure for content that isn't urgent.
- **`document.ariaNotify` directly.** Rejected: it became Baseline only in September 2026, and the supported browsers include the previous major versions, which predate it.
- **Announcing each `delta`.** Rejected: fragments of words and sentences can't be followed by ear.
- **One region for everything.** Rejected: short status messages would interleave with the answer's sentences.
- **Automated checks only.** Rejected: axe finds only part of the WCAG failures; what a screen reader actually says needs a person.

## Consequences

- Screen-reader users hear answers as they stream, one sentence at a time, and keyboard users can reach and operate everything; the regions stay quiet when a stored session opens.
- Every new screen state needs an axe pass in both themes, and the manual check gates each production release that changes the web client.
- Obligations (84-testing.md): the axe gate in every browser test; a browser test that, keyboard only, a user can skip to main content, open a session, send a question with Enter, stop it with Escape, open a citation with Enter, close it with Escape with focus returned, and give feedback; a component test that streamed text reaches the `role="log"` region sentence by sentence, that the answer container has no `aria-live` and that status messages reach the `role="status"` region; browser tests at 320 px wide (no horizontal page scroll) and at 200% zoom (no clipped text); component tests of the Auto-refresh switches with fake timers; the manual release check as a runbook (86-operations.md).

## References

- WCAG 2.2, understanding 4.1.3 Status Messages: https://www.w3.org/WAI/WCAG22/Understanding/status-messages.html (checked 2026-10-14)
- WAI-ARIA 1.2, live regions: https://www.w3.org/TR/wai-aria-1.2/#aria-live (checked 2026-10-14)
- MDN, ARIA live regions: https://developer.mozilla.org/en-US/docs/Web/Accessibility/ARIA/Guides/Live_regions (checked 2026-10-14)
- MDN, `Document.ariaNotify()`: https://developer.mozilla.org/en-US/docs/Web/API/Document/ariaNotify (checked 2026-10-14)
- Fluent UI notification best practices: https://react.fluentui.dev/?path=/docs/concepts-developer-accessibility-notification-best-practices--docs (checked 2026-10-14)
- Fluent UI `AriaLiveAnnouncer`: https://react.fluentui.dev/?path=/docs/utilities-aria-live-arialiveannouncer--docs (checked 2026-10-14)
- Fluent UI v9 accessibility overview: https://react.fluentui.dev/?path=/docs/concepts-developer-accessibility-components-overview--docs (checked 2026-10-14)
- `@axe-core/playwright` 4.13.0: https://registry.npmjs.org/@axe-core/playwright (checked 2026-10-15)
