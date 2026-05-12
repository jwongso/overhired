/**
 * overhired — Ashby ATS form filler
 * Matches: jobs.ashbyhq.com
 *
 * Ashby is a React SPA. Form fields are rendered dynamically and may not
 * exist yet when the script runs — we use waitFor() via MutationObserver polling.
 *
 * Classic content script — registers handler on window.__overhiredATS.ashby
 */
(function () {
  'use strict';

  if (!window.__overhiredCommon) { console.error('[overhired] common.js must load before ashby.js'); return; }

  const { setValue, waitFor, fillCoverLetterTextarea } = window.__overhiredCommon;

  async function fill(profile, coverLetter) {
    await waitFor(() => document.querySelector('input[data-testid], input[aria-label]'), 5000)
      .catch(() => null);

    await fillByLabel('First Name',  profile.name?.split(' ')[0] || profile.first_name || '');
    await fillByLabel('Last Name',   profile.name?.split(' ').slice(1).join(' ') || profile.last_name || '');
    await fillByLabel('Email',       profile.email    || '');
    await fillByLabel('Phone',       profile.phone    || '');
    await fillByLabel('LinkedIn',    profile.linkedin || '');
    await fillByLabel('GitHub',      profile.github   || '');
    await fillByLabel('City',        profile.address_city    || '');
    await fillByLabel('Country',     profile.address_country || '');

    if (coverLetter) {
      const ta = await findTextareaByLabel('Cover Letter')
              || await findTextareaByLabel('Message');
      if (ta) setValue(ta, coverLetter);
      else fillCoverLetterTextarea(coverLetter);
    }
  }

  async function fillByLabel(labelText, value) {
    if (!value) return;
    const input = await waitFor(() => _inputForLabel(labelText), 3000).catch(() => null);
    if (input) setValue(input, value);
  }

  async function findTextareaByLabel(labelText) {
    return waitFor(() => {
      for (const label of document.querySelectorAll('label')) {
        if (label.textContent.trim().toLowerCase().includes(labelText.toLowerCase())) {
          const forId = label.htmlFor;
          if (forId) return document.getElementById(forId);
          return label.querySelector('textarea, input');
        }
      }
      return null;
    }, 3000).catch(() => null);
  }

  function _inputForLabel(labelText) {
    for (const label of document.querySelectorAll('label')) {
      if (label.textContent.trim().toLowerCase().includes(labelText.toLowerCase())) {
        const forId = label.htmlFor;
        if (forId) {
          const el = document.getElementById(forId);
          if (el) return el;
        }
        const el = label.querySelector('input, select');
        if (el) return el;
      }
    }
    return document.querySelector(
      `input[aria-label*="${labelText}" i], input[placeholder*="${labelText}" i]`
    );
  }

  window.__overhiredATS.ashby = fill;
})();
