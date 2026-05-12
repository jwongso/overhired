/**
 * overhired — SAP SuccessFactors ATS form filler
 * Matches: *.successfactors.com, *.sapsf.com, *.successfactors.eu
 *
 * SuccessFactors Recruiting is an Angular SPA. Fields are identified by:
 *   1. data-automation-id attributes (most reliable — set by SAP)
 *   2. Visible label text (fallback — works across localized portals)
 *
 * The apply flow is typically multi-step (General Info → Resume → Questions →
 * Review). We fill whatever is visible; the user clicks Next between steps.
 *
 * Classic content script — registers handler on window.__overhiredATS.successfactors
 */
(function () {
  'use strict';

  const { setValue, waitFor, fillCoverLetterTextarea } = window.__overhiredCommon;

  async function fill(profile, coverLetter) {
    // Wait for the Angular form to bootstrap.
    await waitFor(
      () => document.querySelector('[data-automation-id], input[formcontrolname]'),
      8000
    ).catch(() => null);

    // ── Strategy 1: data-automation-id (SAP standard career site) ────────────

    _auto('firstName',   profile.name?.split(' ')[0] || profile.first_name || '');
    _auto('lastName',    profile.name?.split(' ').slice(1).join(' ') || profile.last_name || '');
    _auto('email',       profile.email || '');
    _auto('phone',       profile.phone || '');
    _auto('address1',    profile.address_street || '');
    _auto('city',        profile.address_city   || '');
    _auto('state',       profile.address_state  || '');
    _auto('zip',         profile.address_postal || '');
    _auto('linkedin',    profile.linkedin || '');

    // Country is often a <select> in SF
    const countryEl = document.querySelector(
      'select[data-automation-id="country"], select[formcontrolname*="country" i]'
    );
    if (countryEl && profile.address_country) setValue(countryEl, profile.address_country);

    // ── Strategy 2: label-based fallback (company-customised portals) ────────

    await _labelFill('First Name',   profile.name?.split(' ')[0] || profile.first_name || '');
    await _labelFill('Last Name',    profile.name?.split(' ').slice(1).join(' ') || profile.last_name || '');
    await _labelFill('Email',        profile.email || '');
    await _labelFill('Phone',        profile.phone || '');
    await _labelFill('Address',      profile.address_street  || '');
    await _labelFill('City',         profile.address_city    || '');
    await _labelFill('State',        profile.address_state   || '');
    await _labelFill('Postal',       profile.address_postal  || '');
    await _labelFill('LinkedIn',     profile.linkedin || '');

    // ── Cover letter ─────────────────────────────────────────────────────────
    // SF sometimes shows a cover letter step with a textarea; sometimes it's
    // a file upload only. Fill the textarea if present.
    if (coverLetter) {
      const ta = document.querySelector(
        'textarea[data-automation-id*="cover" i], textarea[formcontrolname*="cover" i]'
      ) || await waitFor(() =>
        [...document.querySelectorAll('textarea')]
          .find(t => /cover|letter|motivation/i.test(
            (t.getAttribute('data-automation-id') || '') +
            (t.getAttribute('formcontrolname') || '') +
            (t.getAttribute('aria-label') || '')
          ))
      , 3000).catch(() => null);

      if (ta) setValue(ta, coverLetter);
      else fillCoverLetterTextarea(coverLetter);
    }
  }

  /** Fill an element identified by data-automation-id. */
  function _auto(automationId, value) {
    if (!value) return;
    // SF uses both exact and prefixed ids: "firstName", "input-firstName", etc.
    const el = document.querySelector(
      `[data-automation-id="${automationId}"],` +
      `[data-automation-id="input-${automationId}"],` +
      `input[formcontrolname="${automationId}"]`
    );
    if (el) setValue(el, value);
  }

  /** Fill an input whose associated <label> contains labelText. */
  async function _labelFill(labelText, value) {
    if (!value) return;
    // Skip if _auto already filled something with matching aria-label
    const existing = document.querySelector(`input[aria-label*="${labelText}" i]`);
    if (existing && existing.value) return; // already filled

    const el = _inputForLabel(labelText)
            || document.querySelector(`input[placeholder*="${labelText}" i]`);
    if (el && !el.value) setValue(el, value);
  }

  function _inputForLabel(labelText) {
    for (const label of document.querySelectorAll('label')) {
      if (label.textContent.trim().toLowerCase().includes(labelText.toLowerCase())) {
        if (label.htmlFor) {
          const el = document.getElementById(label.htmlFor);
          if (el) return el;
        }
        const el = label.querySelector('input, select, textarea');
        if (el) return el;
        // SF sometimes puts the label as a sibling
        const next = label.nextElementSibling;
        if (next?.matches('input, select, textarea')) return next;
      }
    }
    return null;
  }

  window.__overhiredATS.successfactors = fill;
})();
