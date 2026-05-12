/**
 * overhired — Greenhouse ATS form filler
 * Matches: boards.greenhouse.io, *.greenhouse.io/applications
 *
 * Greenhouse uses a clean, stable DOM with data-field attributes.
 * Most fields are standard <input> or <select> elements.
 *
 * Classic content script — registers handler on window.__overhiredATS.greenhouse
 */
(function () {
  'use strict';

  const { setValue, waitFor, fillCoverLetterTextarea } = window.__overhiredCommon;

  async function fill(profile, coverLetter) {
    const fields = {
      first_name: profile.name?.split(' ')[0] || profile.first_name || '',
      last_name:  profile.name?.split(' ').slice(1).join(' ') || profile.last_name || '',
      email:      profile.email || '',
      phone:      profile.phone || '',
      location:   [profile.address_city, profile.address_country].filter(Boolean).join(', '),
    };

    for (const [id, value] of Object.entries(fields)) {
      if (!value) continue;
      const el = document.getElementById(id)
              || document.querySelector(`input[name="${id}"]`);
      if (el) setValue(el, value);
    }

    // LinkedIn / GitHub — Greenhouse custom URL fields
    const urlFields = document.querySelectorAll(
      '.custom-field input[type="text"], input[id*="url"], input[id*="linkedin"], input[id*="website"]'
    );
    for (const el of urlFields) {
      const attr = (el.id + ' ' + el.name + ' ' + (el.placeholder || '')).toLowerCase();
      if (/linkedin/.test(attr) && profile.linkedin) setValue(el, profile.linkedin);
      else if (/github/.test(attr) && profile.github) setValue(el, profile.github);
    }

    // Cover letter
    if (coverLetter) {
      const ta = document.getElementById('cover_letter')
              || document.querySelector('textarea[name="cover_letter"]')
              || await waitFor(() =>
                  document.querySelector('textarea[id*="cover"], textarea[class*="cover"]')
                ).catch(() => null);
      if (ta) setValue(ta, coverLetter);
      else fillCoverLetterTextarea(coverLetter);
    }
  }

  window.__overhiredATS.greenhouse = fill;
})();
