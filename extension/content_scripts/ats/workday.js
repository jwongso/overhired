/**
 * grapply — Workday ATS form filler
 * Matches: *.myworkdayjobs.com, *.wd1.myworkdayjobs.com, *.wd5.myworkdayjobs.com, etc.
 *
 * Workday Recruiting is a heavy Angular SPA. Key traits:
 *   - Fields are identified by data-automation-id attributes (most stable).
 *   - The apply flow is multi-step: My Information → My Experience → Application Questions → Review.
 *   - We fill whatever step is currently visible; the user clicks Next between steps.
 *   - Some fields (resume upload, work history entries) are intentionally left for the user.
 *
 * Classic content script — registers handler on window.__grapplyATS.workday
 */
(function () {
  'use strict';

  if (!window.__grapplyCommon) { console.error('[grapply] common.js must load before workday.js'); return; }

  const { setValue, waitFor, fillCoverLetterTextarea } = window.__grapplyCommon;

  async function fill(profile, coverLetter) {
    // Wait for Workday Angular to hydrate the form.
    await waitFor(
      () => document.querySelector('[data-automation-id]'),
      10000
    ).catch(() => null);

    // ── My Information step ───────────────────────────────────────────────────

    _auto('legalNameSection_firstName',  profile.name?.split(' ')[0] || profile.first_name || '');
    _auto('legalNameSection_lastName',   profile.name?.split(' ').slice(1).join(' ') || profile.last_name || '');

    // Workday uses a single "preferredName" section too — fill if present
    _auto('preferredNameSection_firstName', profile.name?.split(' ')[0] || profile.first_name || '');
    _auto('preferredNameSection_lastName',  profile.name?.split(' ').slice(1).join(' ') || profile.last_name || '');

    // Email
    _auto('email', profile.email || '');
    // Phone — Workday splits into country code (select) + number (input)
    _auto('phone-number', profile.phone?.replace(/^\+\d+\s*/, '') || profile.phone || '');

    // Address
    _auto('addressSection_addressLine1', profile.address_street  || '');
    _auto('addressSection_city',         profile.address_city    || '');
    _auto('addressSection_postalCode',   profile.address_postal  || '');

    // Country and State are typically <select> dropdowns in Workday
    const countryEl = document.querySelector('[data-automation-id="addressSection_countryRegion"]');
    if (countryEl && profile.address_country) setValue(countryEl, profile.address_country);

    const stateEl = document.querySelector('[data-automation-id="addressSection_countryRegionCity"]');
    if (stateEl && profile.address_state) setValue(stateEl, profile.address_state);

    // ── Social / web links ────────────────────────────────────────────────────

    _auto('linkedin', profile.linkedin || '');
    _auto('website',  profile.github   || '');   // some Workday instances use "website"

    // Workday sometimes lists social links under "How Did You Hear About Us" section
    // or as custom questions — skip those (too variable per company config).

    // ── Cover letter / additional information ─────────────────────────────────
    // Workday may offer a cover letter textarea in "My Experience" or
    // "Application Questions" step.
    if (coverLetter) {
      const ta = await waitFor(() =>
        document.querySelector(
          '[data-automation-id="coverLetterSection"] textarea,' +
          'textarea[data-automation-id*="cover" i],' +
          'textarea[data-automation-id="additionalInformation"]'
        )
      , 3000).catch(() => null)
      || [...document.querySelectorAll('textarea')]
           .find(t => /cover|letter|motivation|additional.?info/i.test(
             (t.getAttribute('data-automation-id') || '') +
             (t.getAttribute('aria-label') || '') +
             (t.placeholder || '')
           ));

      if (ta) setValue(ta, coverLetter);
      else fillCoverLetterTextarea(coverLetter);
    }
  }

  /**
   * Fill a Workday field by data-automation-id.
   * Workday uses both exact IDs and prefixed variants — we try several patterns.
   */
  function _auto(id, value) {
    if (!value) return;
    const el = document.querySelector(
      `[data-automation-id="${id}"],` +
      `[data-automation-id$="-${id}"],` +
      `input[data-automation-id="${id}"]`
    );
    if (el) setValue(el, value);
  }

  window.__grapplyATS.workday = fill;
})();
