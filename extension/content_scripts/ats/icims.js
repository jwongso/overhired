/**
 * overhired — iCIMS ATS form filler
 * Matches: *.icims.com
 *
 * iCIMS has two generations:
 *   Classic — table-based layout; inputs use name="applicant.firstName" etc.
 *   Modern  — card-based layout; inputs use data-field or aria-label attributes.
 * We try both strategies for each field.
 *
 * NOTE: Some iCIMS portals embed the application inside a cross-origin <iframe>
 * (careers.company.com → iframe src="company.icims.com"). Content scripts cannot
 * cross iframe origins; in that case Fill Form is a no-op with a console warning.
 *
 * Classic content script — registers handler on window.__overhiredATS.icims
 */
(function () {
  'use strict';

  if (!window.__overhiredCommon) { console.error('[overhired] common.js must load before icims.js'); return; }

  const { setValue, waitFor, fillCoverLetterTextarea } = window.__overhiredCommon;

  async function fill(profile, coverLetter) {
    // Guard: if the actual form lives in a cross-origin iframe we can't reach it.
    const iframes = document.querySelectorAll('iframe[src*="icims"]');
    if (iframes.length > 0) {
      console.warn('[overhired] iCIMS: form is in a cross-origin iframe — cannot auto-fill.');
      return;
    }

    // Wait for at least one form input to appear.
    await waitFor(() => document.querySelector('form input, input[name*="applicant"]'), 6000)
      .catch(() => null);

    // ── Field filling: try Classic (name attribute) then Modern (aria/data) ──

    _fillField(
      'input[name="applicant.firstName"], input[name*="firstname" i]',
      'input[aria-label*="First Name" i], input[placeholder*="First" i]',
      profile.name?.split(' ')[0] || profile.first_name || ''
    );

    _fillField(
      'input[name="applicant.lastName"], input[name*="lastname" i]',
      'input[aria-label*="Last Name" i], input[placeholder*="Last" i]',
      profile.name?.split(' ').slice(1).join(' ') || profile.last_name || ''
    );

    _fillField(
      'input[name="applicant.email"], input[name*="email" i]',
      'input[type="email"], input[aria-label*="Email" i]',
      profile.email || ''
    );

    _fillField(
      'input[name="applicant.phone"], input[name*="phone" i]',
      'input[type="tel"], input[aria-label*="Phone" i]',
      profile.phone || ''
    );

    _fillField(
      'input[name*="address1" i], input[name*="street" i]',
      'input[aria-label*="Address" i], input[placeholder*="Street" i]',
      profile.address_street || ''
    );

    _fillField(
      'input[name*="city" i]',
      'input[aria-label*="City" i], input[placeholder*="City" i]',
      profile.address_city || ''
    );

    _fillField(
      'input[name*="state" i], select[name*="state" i]',
      'input[aria-label*="State" i], select[aria-label*="State" i]',
      profile.address_state || ''
    );

    _fillField(
      'input[name*="zip" i], input[name*="postal" i]',
      'input[aria-label*="Zip" i], input[aria-label*="Postal" i]',
      profile.address_postal || ''
    );

    // Country — iCIMS often uses a <select>
    const countryEl = document.querySelector(
      'select[name*="country" i], select[aria-label*="Country" i]'
    );
    if (countryEl && profile.address_country) setValue(countryEl, profile.address_country);

    // LinkedIn / website URL fields
    _fillField(
      'input[name*="linkedin" i]',
      'input[aria-label*="LinkedIn" i], input[placeholder*="LinkedIn" i]',
      profile.linkedin || ''
    );

    // Cover letter — iCIMS uses a textarea or sometimes a rich-text div
    if (coverLetter) {
      const ta = document.querySelector(
        'textarea[name*="cover" i], textarea[id*="cover" i], textarea[aria-label*="Cover" i]'
      ) || await waitFor(() =>
        [...document.querySelectorAll('textarea')]
          .find(t => /cover|letter|motivation/i.test((t.name || '') + (t.id || '') + (t.getAttribute('aria-label') || '')))
      , 3000).catch(() => null);

      if (ta) setValue(ta, coverLetter);
      else fillCoverLetterTextarea(coverLetter);
    }
  }

  /** Try the classic selector first, fall back to the modern selector. */
  function _fillField(classicSel, modernSel, value) {
    if (!value) return;
    const el = document.querySelector(classicSel) || document.querySelector(modernSel);
    if (el) setValue(el, value);
  }

  window.__overhiredATS.icims = fill;
})();
