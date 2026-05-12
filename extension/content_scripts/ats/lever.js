/**
 * overhired — Lever ATS form filler
 * Matches: jobs.lever.co
 *
 * Lever's apply page is a React SPA with a clean, consistent structure:
 *   - Standard inputs with name attributes (first-name, last-name, email, phone)
 *   - Social/URL fields with placeholder text
 *   - A single "Additional Information" or cover letter textarea
 *
 * Classic content script — registers handler on window.__overhiredATS.lever
 */
(function () {
  'use strict';

  if (!window.__overhiredCommon) { console.error('[overhired] common.js must load before lever.js'); return; }

  const { setValue, waitFor, fillCoverLetterTextarea } = window.__overhiredCommon;

  async function fill(profile, coverLetter) {
    // Lever renders the form on page load — no heavy SPA delay, but wait briefly.
    await waitFor(() => document.querySelector('input[name="name"], input[name="first-name"]'), 5000)
      .catch(() => null);

    // Lever has two name formats: single "name" field, or "first-name"/"last-name" split.
    const singleName = document.querySelector('input[name="name"]');
    if (singleName) {
      setValue(singleName, profile.name || [profile.first_name, profile.last_name].filter(Boolean).join(' '));
    } else {
      _try('input[name="first-name"], input[name="firstName"]',
        profile.name?.split(' ')[0] || profile.first_name || '');
      _try('input[name="last-name"],  input[name="lastName"]',
        profile.name?.split(' ').slice(1).join(' ') || profile.last_name || '');
    }

    _try('input[name="email"]',                                  profile.email   || '');
    _try('input[name="phone"], input[type="tel"]',               profile.phone   || '');
    _try('input[name="org"],   input[name="company"]',           '');              // current company — leave blank
    _try('input[name="urls[LinkedIn]"], input[placeholder*="LinkedIn" i]',  profile.linkedin || '');
    _try('input[name="urls[GitHub]"],   input[placeholder*="GitHub" i]',    profile.github   || '');
    _try('input[name="urls[Portfolio]"],input[placeholder*="Portfolio" i]',  profile.github   || '');

    // Lever's cover letter / additional info
    if (coverLetter) {
      const ta = document.querySelector(
        'textarea[name="comments"], textarea[name="additionalInfo"], textarea[name="coverLetter"]'
      ) || await waitFor(() =>
        [...document.querySelectorAll('textarea')]
          .find(t => /cover|comment|additional|motivation/i.test(
            (t.name || '') + (t.id || '') + (t.placeholder || '') + (t.getAttribute('aria-label') || '')
          ))
      , 3000).catch(() => null);

      if (ta) setValue(ta, coverLetter);
      else fillCoverLetterTextarea(coverLetter);
    }
  }

  function _try(selector, value) {
    if (!value) return;
    const el = document.querySelector(selector);
    if (el) setValue(el, value);
  }

  window.__overhiredATS.lever = fill;
})();
