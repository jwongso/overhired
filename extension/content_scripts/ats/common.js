/**
 * overhired — ATS handler shared utilities
 */

/**
 * Set a value on an input/textarea in a way that React, Vue, and Svelte
 * synthetic event systems all pick up (they intercept the native setter).
 */
export function setValue(el, value) {
  const proto = el.tagName === 'TEXTAREA'
    ? HTMLTextAreaElement.prototype
    : el.tagName === 'SELECT'
      ? HTMLSelectElement.prototype
      : HTMLInputElement.prototype;

  const descriptor = Object.getOwnPropertyDescriptor(proto, 'value');
  descriptor?.set?.call(el, value);

  el.dispatchEvent(new Event('input',  { bubbles: true }));
  el.dispatchEvent(new Event('change', { bubbles: true }));
  el.dispatchEvent(new Event('blur',   { bubbles: true }));
}

/**
 * Poll for a DOM element (or any truthy return value from `fn`) up to
 * `timeoutMs`. Resolves with the element, rejects on timeout.
 */
export function waitFor(fn, timeoutMs = 5000, intervalMs = 200) {
  return new Promise((resolve, reject) => {
    const result = fn();
    if (result) { resolve(result); return; }

    const timer    = setInterval(() => {
      const r = fn();
      if (r) { clearInterval(timer); clearTimeout(deadline); resolve(r); }
    }, intervalMs);

    const deadline = setTimeout(() => {
      clearInterval(timer);
      reject(new Error(`waitFor timed out after ${timeoutMs}ms`));
    }, timeoutMs);
  });
}

/**
 * Generic fallback: find the most likely cover letter textarea on any page
 * (used when no ATS-specific handler matches).
 */
export function fillCoverLetterTextarea(coverLetter) {
  const textareas = [...document.querySelectorAll('textarea')];

  // Prefer one with 'cover', 'letter', or 'motivation' in its attributes
  const named = textareas.find(ta =>
    /cover|letter|motivation|introduction/i.test(
      (ta.name || '') + (ta.id || '') + (ta.placeholder || '') + (ta.getAttribute('aria-label') || '')
    )
  );

  // Fall back to the largest textarea by row count / character capacity
  const largest = textareas.sort((a, b) => (b.rows || 0) - (a.rows || 0))[0];

  const target = named || largest;
  if (target) setValue(target, coverLetter);
}

/**
 * Build a display name from a profile object.
 * Returns "First Last" from profile.name, or constructs from first_name + last_name.
 */
export function fullName(profile) {
  if (profile.name) return profile.name.trim();
  return [profile.first_name, profile.last_name].filter(Boolean).join(' ');
}

/**
 * Stub handler for ATS platforms not yet implemented.
 * Falls back to the generic extractor.js filler.
 */
export function fill(profile, coverLetter) {
  // No ATS-specific logic — generic filler in extractor.js handles it
  console.info('[overhired] No specific handler for this ATS — using generic filler.');
  fillCoverLetterTextarea(coverLetter);
}
