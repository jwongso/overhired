# Adding a New ATS Handler

This guide walks you through adding form-fill support for a new ATS (Applicant Tracking System).

---

## 1. Identify the ATS

Find the URL pattern for the ATS application page. Examples:
- Greenhouse: `boards.greenhouse.io`
- Ashby: `jobs.ashbyhq.com`

---

## 2. Register the URL pattern

Open `extension/content_scripts/extractor.js` and find the `ATS_PATTERNS` map:

```js
const ATS_PATTERNS = {
  greenhouse: /boards\.greenhouse\.io/,
  ashby:      /jobs\.ashbyhq\.com/,
  workable:   /apply\.workable\.com/,
  // Add yours here:
  myats:      /apply\.myats\.com/,
};
```

---

## 3. Create the handler file

Create `extension/content_scripts/ats/myats.js`:

```js
import { setValue, waitFor, fillCoverLetterTextarea, fullName } from './common.js';

/**
 * Fill application form fields on MyATS.
 * @param {Object} profile   - user profile from chrome.storage.local
 * @param {string} coverLetter - generated cover letter plain text
 */
export async function fill(profile, coverLetter) {
  // Wait for the form to render
  await waitFor('#application-form');

  // Fill fields by selector
  setValue(document.querySelector('#first_name'), profile.firstName);
  setValue(document.querySelector('#last_name'),  profile.lastName);
  setValue(document.querySelector('#email'),      profile.email);
  setValue(document.querySelector('#phone'),      profile.phone);

  // Fill cover letter textarea
  await fillCoverLetterTextarea(document, coverLetter);
}
```

### Common utilities (`common.js`)

| Function | Description |
|----------|-------------|
| `setValue(el, value)` | Sets value on any input/textarea; React/Vue compatible |
| `waitFor(selector, timeout)` | Returns a promise that resolves when element appears in DOM |
| `fillCoverLetterTextarea(doc, text)` | Finds the most likely cover letter textarea and fills it |
| `fullName(profile)` | Returns `profile.firstName + ' ' + profile.lastName` |

---

## 4. Handle React/Vue SPAs

If the ATS uses React or another framework, native `element.value = x` won't trigger
re-renders. Use `setValue` from `common.js` — it uses the native property descriptor
trick and dispatches `input`/`change`/`blur` events:

```js
// For dynamic forms, wait for the element to appear first
const emailField = await waitFor('input[name="email"]');
setValue(emailField, profile.email);
```

---

## 5. Handle multi-step forms

Some ATSs paginate the form. Use `waitFor` to gate each step:

```js
export async function fill(profile, coverLetter) {
  // Step 1
  await waitFor('input[name="first_name"]');
  setValue(document.querySelector('input[name="first_name"]'), profile.firstName);
  // ... more step-1 fields ...

  // Click "Next"
  document.querySelector('button[data-step-next]')?.click();

  // Step 2 — wait for new fields
  await waitFor('textarea[name="cover_letter"]');
  await fillCoverLetterTextarea(document, coverLetter);
}
```

---

## 6. Test it

1. Load the extension unpacked (or reload it)
2. Start the companion: `python companion/main.py`
3. Browse to the ATS application page
4. Click the overhired icon → **Fill Form**
5. Check that all fields are filled correctly
6. Submit the PR!

---

## Tips

- **Inspect the DOM** in DevTools before writing selectors — prefer `id` attributes over CSS class names (class names change; IDs are more stable)
- **React fields** — if a field resets after you type, use `setValue` instead of direct assignment
- **Required fields** — fill all required fields even if the user left them blank (the extension will fill with an empty string, which is safer than leaving the field untouched)
- **File upload** — resume PDF upload is intentionally left manual in most handlers; programmatic file input triggers bot detection on many platforms
