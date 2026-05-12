/**
 * overhired — Workable ATS form filler
 * Matches: apply.workable.com
 *
 * Workable uses a multi-step form. Fields are identified by name attributes.
 * Some steps load asynchronously — we use waitFor() for reliability.
 *
 * Classic content script — registers handler on window.__overhiredATS.workable
 */
(function () {
  'use strict';

  if (!window.__overhiredCommon) { console.error('[overhired] common.js must load before workable.js'); return; }

  const { setValue, waitFor, fillCoverLetterTextarea } = window.__overhiredCommon;

  async function fill(profile, coverLetter) {
    await waitFor(() => document.querySelector('form input'), 5000).catch(() => null);

    const nameEl = document.querySelector('input[name="name"], input[id="name"]');
    if (nameEl && profile.name) {
      setValue(nameEl, profile.name);
    } else {
      _tryField('input[name="firstname"], input[name="first_name"]',
        profile.name?.split(' ')[0] || profile.first_name || '');
      _tryField('input[name="lastname"],  input[name="last_name"]',
        profile.name?.split(' ').slice(1).join(' ') || profile.last_name || '');
    }

    _tryField('input[name="email"], input[type="email"]', profile.email || '');
    _tryField('input[name="phone"], input[type="tel"]',   profile.phone || '');
    _tryField('input[name="linkedin"], input[placeholder*="LinkedIn" i]', profile.linkedin || '');
    _tryField('input[name="github"],   input[placeholder*="GitHub" i]',   profile.github   || '');
    _tryField('input[name="city"],     input[placeholder*="City" i]',     profile.address_city    || '');
    _tryField('input[name="country"],  select[name="country"]',           profile.address_country || '');

    if (coverLetter) {
      const ta = document.querySelector(
        'textarea[name="cover_letter"], textarea[name="coverLetter"], textarea[id*="cover"]'
      ) || await waitFor(() =>
        [...document.querySelectorAll('textarea')]
          .find(t => /cover|letter|motivation/i.test(t.name + t.id + (t.placeholder || '')))
      , 3000).catch(() => null);

      if (ta) setValue(ta, coverLetter);
      else fillCoverLetterTextarea(coverLetter);
    }
  }

  function _tryField(selector, value) {
    if (!value) return;
    const el = document.querySelector(selector);
    if (el) setValue(el, value);
  }

  window.__overhiredATS.workable = fill;
})();
