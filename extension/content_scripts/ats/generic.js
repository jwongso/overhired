/**
 * grapply — Generic ATS handler fallback
 *
 * Used when no specific ATS handler matches the current URL.
 * Attempts a best-effort fill using common field patterns.
 *
 * Classic content script — registers handler on window.__grapplyATS.generic
 */
(function () {
  'use strict';

  if (!window.__grapplyCommon) { console.error('[grapply] common.js must load before generic.js'); return; }

  const { setValue, fillCoverLetterTextarea } = window.__grapplyCommon;

  async function fill(profile, coverLetter) {
    console.log('[grapply] No specific ATS handler — using generic filler');

    const map = [
      [/first.?name|fname/i,       profile.name?.split(' ')[0] || profile.first_name || ''],
      [/last.?name|lname/i,        profile.name?.split(' ').slice(1).join(' ') || profile.last_name || ''],
      [/full.?name|^name$/i,       profile.name || ''],
      [/email/i,                   profile.email || ''],
      [/phone|mobile|telephone/i,  profile.phone || ''],
      [/linkedin/i,                profile.linkedin || ''],
      [/github/i,                  profile.github   || ''],
      [/address|street/i,          profile.address_street  || ''],
      [/city/i,                    profile.address_city    || ''],
      [/state|province|region/i,   profile.address_state   || ''],
      [/zip|postal/i,              profile.address_postal  || ''],
      [/country/i,                 profile.address_country || ''],
    ];

    const inputs = document.querySelectorAll('input[type="text"], input[type="email"], input[type="tel"]');
    for (const input of inputs) {
      const attr = (input.name + ' ' + input.id + ' ' + (input.placeholder || '')).toLowerCase();
      for (const [pattern, value] of map) {
        if (value && pattern.test(attr)) {
          setValue(input, value);
          break;
        }
      }
    }

    if (coverLetter) fillCoverLetterTextarea(coverLetter);
  }

  window.__grapplyATS.generic = fill;
})();
