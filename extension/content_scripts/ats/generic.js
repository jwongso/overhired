/**
 * Generic ATS handler — fallback when no specific handler matches.
 * Attempts a best-effort fill using common field patterns.
 */
import { setValue, waitFor, fillCoverLetterTextarea } from './common.js';

export async function fill(profile, coverLetter) {
  console.log('[overhired] No specific ATS handler found — using generic filler');

  // Common first/last name patterns
  const firstNameSel = 'input[name*="first"], input[id*="first"], input[placeholder*="First"]';
  const lastNameSel  = 'input[name*="last"],  input[id*="last"],  input[placeholder*="Last"]';
  const emailSel     = 'input[type="email"], input[name*="email"], input[id*="email"]';
  const phoneSel     = 'input[type="tel"],   input[name*="phone"], input[id*="phone"]';

  const tryFill = (sel, value) => {
    if (!value) return;
    const el = document.querySelector(sel);
    if (el) setValue(el, value);
  };

  tryFill(firstNameSel, profile.firstName);
  tryFill(lastNameSel,  profile.lastName);
  tryFill(emailSel,     profile.email);
  tryFill(phoneSel,     profile.phone);

  await fillCoverLetterTextarea(document, coverLetter);
}
