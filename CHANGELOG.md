# Changelog

## 0.4.1 — Windows compatibility patch

- Use Windows directory-list access with delete sharing disabled so open directory handles block renames and deletion while scan state is in use.
- Make the macOS identity fixture independent of the host OS. Windows regression tests cover leaf and ancestor renames, deletion, state-file replacement, and exclusive scan leases.
- Windows remains a preview; authenticated SPeCTRUM scans were not verified.

## 0.4.0 — Windows preview

- Added native Windows Edge admission, scan state and lock handling, and optional local Tesseract OCR.
- Added Windows to the offline CI matrix. Native Windows execution remains unverified.

## 0.3.0

- Default local OCR for eligible inline JPEG and PNG announcements and instructions, with hash caching and source links.

## 0.2.2 — public preview candidate

- Restored the bundled moodlekit attribution and MIT notice.
- Save the evidence report before accepting changes into the baseline.
- Report crawl and download operation totals separately and combined.
- Added offline browser identity, download exclusion, and persistence failure coverage.

## 0.2.1 — initial packaging candidate

- Added an offline GitHub Actions test matrix for macOS and Linux.
- Added a security and privacy policy and explicit release packaging.
- Live university compatibility remains unverified for this package; crawling is still macOS/Edge only. This is not an official university integration.

## 0.2.0

- Added bounded enrollment discovery, course crawling, incremental evidence, and opt-in downloads.
- Retained the one-page DOM reader for a separately approved browser route.

## 0.1.0

- Initial one-page DOM reader and Hermes plugin registration.
