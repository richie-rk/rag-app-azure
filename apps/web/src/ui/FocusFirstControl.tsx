// A dialog opens with focus on its first control (10-web-client.md sections 7.1 and 11.4), and its
// content stays available to assistive technology. Fluent focuses the first control from an
// effect of the Dialog, on the same tick that the surface receives its Tabster modal attributes;
// when Tabster hasn't created the modal for them yet (a slow render), that focus doesn't activate
// the modal, and Tabster then marks the inactive modal `aria-hidden` while focus sits inside it.
// This component sits inside the surface: it moves focus in when Fluent couldn't, and focuses
// the first control once more when the open surface is hidden, which activates the modal.

import { useEffect, useRef } from "react";

const CONTROLS =
  "button:not([disabled]), input:not([disabled]), textarea:not([disabled]), select:not([disabled]), a[href]";

/** At most this many repeats per opening, so a modal that can't activate never loops. */
const MAX_REPEATS = 3;

function firstControl(surface: HTMLElement): HTMLElement {
  const controls = [...surface.querySelectorAll<HTMLElement>(CONTROLS)];
  return controls.find((element) => !element.hasAttribute("data-tabster-dummy")) ?? surface;
}

/** Rendered anywhere inside a DialogSurface. */
export function FocusFirstControl() {
  const anchor = useRef<HTMLSpanElement>(null);
  useEffect(() => {
    const surface = anchor.current?.closest<HTMLElement>("[role='dialog'], [role='alertdialog']");
    if (surface === null || surface === undefined) return;
    let repeats = 0;
    const focusFirst = () => {
      const target = firstControl(surface);
      // Focusing the focused element raises no focus event, and the event is what activates.
      if (document.activeElement === target) target.blur();
      target.focus();
    };
    if (!surface.contains(document.activeElement)) focusFirst();
    const observer = new MutationObserver(() => {
      if (surface.getAttribute("aria-hidden") !== "true" || repeats >= MAX_REPEATS) return;
      repeats += 1;
      focusFirst();
    });
    observer.observe(surface, { attributes: true, attributeFilter: ["aria-hidden"] });
    return () => {
      observer.disconnect();
    };
  }, []);
  return <span ref={anchor} hidden />;
}
