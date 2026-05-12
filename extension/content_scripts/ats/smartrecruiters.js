/**
 * overhired — SmartRecruiters ATS form filler
 * Matches: jobs.smartrecruiters.com
 *
 * SmartRecruiters uses an Angular SPA with a multi-step application wizard.
 * Fields are typically identified by name attributes or aria-label text.
 * Each step loads independently — we fill what is currently rendered and let
 * the user advance steps manually.
 *
 * Classic content script — registers handler on window.__overhiredATS.smartrecruiters
 */
(function () {
  'use strict';

  if (!window.__overhiredCommon) { console.error('[overhired] common.js must load before smartrecruiters.js'); return; }

  const { setValue, waitFor, fillCoverLetterTextarea } = window.__overhiredCommon;

  async function fill(profile, coverLetter) {
    // Wait for the Angular form to render.
    await waitFor(
      () => document.querySelector('input[name], input[data-qa]'),
      8000
    ).catch(() => null);

    // ── Personal details step ─────────────────────────────────────────────────
    // SmartRecruiters uses data-qa attributes and/or name attributes.

    _qa('first-name',  profile.name?.split(' ')[0] || profile.first_name || '');
    _qa('last-name',   profile.name?.split(' ').slice(1).join(' ') || profile.last_name || '');
    _qa('email',       profile.email || '');
    _qa('phone',       profile.phone || '');

    // Also try by name attribute (some SR versions)
    _name('firstName', profile.name?.split(' ')[0] || profile.first_name || '');
    _name('lastName',  profile.name?.split(' ').slice(1).join(' ') || profile.last_name || '');
    _name('email',     profile.email || '');
    _name('phone',     profile.phone || '');

    // ── Address ───────────────────────────────────────────────────────────────
    _qa('city',         profile.address_city    || '');
    _qa('postal-code',  profile.address_postal  || '');

    // Country / state are typically <select> in SR
    const countryEl = document.querySelector('[data-qa="country"] select, select[name="country"]');
    if (countryEl && profile.address_country) setValue(countryEl, profile.address_country);

    // ── Social links ──────────────────────────────────────────────────────────
    _label('LinkedIn', profile.linkedin || '');
    _label('GitHub',   profile.github   || '');
    _label('Website',  profile.github   || '');

    // ── Cover letter ──────────────────────────────────────────────────────────
    if (coverLetter) {
      const ta = document.querySelector(
        '[data-qa="cover-letter"] textarea,' +
        'textarea[name="coverLetter"],' +
        'textarea[data-qa*="cover" i]'
      ) || await waitFor(() =>
        [...document.querySelectorAll('textarea')]
          .find(t => /cover|letter|motivation/i.test(
            (t.getAttribute('data-qa') || '') +
            (t.name || '') +
            (t.getAttribute('aria-label') || '') +
            (t.placeholder || '')
          ))
      , 3000).catch(() => null);

      if (ta) setValue(ta, coverLetter);
      else fillCoverLetterTextarea(coverLetter);
    }
  }

  /** Fill by data-qa attribute (wrapping div → descendant input/textarea). */
  function _qa(qaValue, value) {
    if (!value) return;
    // data-qa may be on the input itself or on a wrapper div
    const el = document.querySelector(
      `input[data-qa="${qaValue}"],` +
      `textarea[data-qa="${qaValue}"],` +
      `[data-qa="${qaValue}"] input,` +
      `[data-qa="${qaValue}"] textarea`
    );
    if (el) setValue(el, value);
  }

  /** Fill by name attribute. */
  function _name(nameVal, value) {
    if (!value) return;
    const el = document.querySelector(`input[name="${nameVal}"], textarea[name="${nameVal}"]`);
    if (el && !el.value) setValue(el, value);
  }

  /** Fill by visible label text. */
  function _label(labelText, value) {
    if (!value) return;
    for (const label of document.querySelectorAll('label')) {
      if (label.textContent.trim().toLowerCase().includes(labelText.toLowerCase())) {
        const el = label.htmlFor
          ? document.getElementById(label.htmlFor)
          : label.querySelector('input, textarea');
        if (el && !el.value) { setValue(el, value); return; }
      }
    }
    const el = document.querySelector(`input[aria-label*="${labelText}" i], input[placeholder*="${labelText}" i]`);
    if (el && !el.value) setValue(el, value);
  }

  window.__overhiredATS.smartrecruiters = fill;
})();
