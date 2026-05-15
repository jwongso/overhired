/**
 * grapply — ATS handler shared utilities
 *
 * Classic content script (no ES modules). Exposes helpers on
 * window.__grapplyCommon so subsequent ATS handler scripts can use them.
 */
(function () {
  'use strict';

  /**
   * Set a value on an input/textarea in a way that React, Vue, and Svelte
   * synthetic event systems all pick up (they intercept the native setter).
   */
  function setValue(el, value) {
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
   * Poll for a truthy return value from `fn` up to `timeoutMs`.
   * Resolves with the value; rejects on timeout.
   */
  function waitFor(fn, timeoutMs = 5000, intervalMs = 200) {
    function _tryFn() {
      try { return fn(); } catch (e) { console.warn('[grapply] waitFor: fn threw:', e); return null; }
    }
    return new Promise((resolve, reject) => {
      const result = _tryFn();
      if (result) { resolve(result); return; }

      const timer    = setInterval(() => {
        const r = _tryFn();
        if (r) { clearInterval(timer); clearTimeout(deadline); resolve(r); }
      }, intervalMs);

      const deadline = setTimeout(() => {
        clearInterval(timer);
        reject(new Error(`waitFor timed out after ${timeoutMs}ms`));
      }, timeoutMs);
    });
  }

  /**
   * Find the most likely cover letter textarea on any page and fill it.
   */
  function fillCoverLetterTextarea(coverLetter) {
    const textareas = [...document.querySelectorAll('textarea')];
    const named = textareas.find(ta =>
      /cover|letter|motivation|introduction/i.test(
        (ta.name || '') + (ta.id || '') + (ta.placeholder || '') + (ta.getAttribute('aria-label') || '')
      )
    );
    const largest = textareas.sort((a, b) => (b.rows || 0) - (a.rows || 0))[0];
    const target = named || largest;
    if (target) setValue(target, coverLetter);
  }

  /** Build "First Last" from a profile object. */
  function fullName(profile) {
    if (profile.name) return profile.name.trim();
    return [profile.first_name, profile.last_name].filter(Boolean).join(' ');
  }

  window.__grapplyCommon = { setValue, waitFor, fillCoverLetterTextarea, fullName };
  window.__grapplyATS    = window.__grapplyATS || {};
})();
