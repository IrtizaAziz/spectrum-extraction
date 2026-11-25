# Spectrum Extraction

An unofficial, read-only Hermes Agent plugin for Universiti Malaya SPeCTRUM. It is not affiliated with or endorsed by Universiti Malaya. It does not require a separate academic dashboard, account export, or academic database.

## Install

1. Unzip `spectrum-extraction-0.4.0.zip`. Review the plugin and move the `spectrum-extraction` directory into your active Hermes profile's `plugins/` folder (default: `~/.hermes/plugins/`). When upgrading an earlier copy, back it up and replace the plugin directory rather than mixing files.
2. Run `hermes plugins enable spectrum-extraction` and reload Hermes. Validate the exact directory with `hermes plugins doctor --ci /path/to/plugins/spectrum-extraction`.
3. **Live crawl/download requirements:** macOS or native Windows with an existing, explicitly approved Microsoft Edge CDP session using a dedicated `~/.hermes/spectrum-edge` user-data directory and listener at `127.0.0.1:9337`. Configure the active Hermes profile's `browser.cdp_url` to that approved loopback URL or its current browser WebSocket, with no conflicting runtime override. The plugin never launches or repairs Edge. Do not enable debugging on a personal profile or expose the port over the network. Only use automation allowed by your institution and account.
4. Sign in manually in that session and keep the official SPeCTRUM dashboard visible in a normal window. The crawler needs an authenticated, visible `https://spectrum.um.edu.my/` document without a query string or fragment, and uses the exact target ID verified by the host's approved browser tools. On a mismatch, sign-in wall, policy restriction, or challenge, stop; do not use another route as a fallback.

**Distribution defaults:** The public package uses a dedicated `~/.hermes/spectrum-edge` Edge user-data directory and `127.0.0.1:9337`. Set up that profile separately using your approved browser tooling; this plugin will not reuse arbitrary browser profiles or attach to a different port. Its source ZIP does not include browser/profile data, course evidence, or a prefilled Hermes configuration.

## Usage

- “Use Spectrum Extraction to list my enrolled courses.” `action=discover` returns only enrollment labels, IDs and links.
- “Scan these two SPeCTRUM courses for changed assignment instructions, deadlines and announcements. Don’t download anything.” `action=scan` requires explicitly selected native `course_ids` (1–16). It indexes course activities; parses assignments, quizzes and submission evidence; enumerates forums and samples replies; and verifies selected resource and folder pages. The optional supplemental action calendar filters staged events to selected course IDs. It is not a deadline authority.
- “Scan this SPeCTRUM course and download up to five teaching files.” Downloads default to **off**. With `download_limit` set, the plugin archives supported, same-origin files found in that pass. It skips submission originals and ambiguous references. It verifies file type/content, retains SHA-256 and source links, uses conditional validation when available, and never executes files or unpacks archives onto disk. Maximums: 12 download attempts, 32 MiB per file, 64 MiB of returned binary data per run. Files remain untrusted; never open unsolicited material automatically.
- “Continue the material and discussion backlog only.” Use `backlog_only=true` without re-reading assignment/quiz details. Set `material_limit=0` when only discussion bodies are wanted. Each pass re-verifies source admission; old body evidence is not proof of current coverage. To avoid late-phase starvation, narrow the course set or use backlog-only mode.
- The 0.1.0 visible-page feature is still available via `spectrum_extraction_prepare` and any browser route that your Hermes policy already approves for that selected tab. It is not a crawl fallback.

**Inline images:** Course scans automatically attempt OCR on up to three currently observed, allowlisted inline JPEG/PNG sources per run, even when file archiving is off. On macOS this uses Apple Vision via the local Swift toolchain (Xcode Command Line Tools or Xcode). On Windows, install Tesseract with English language data under `Program Files\Tesseract-OCR` (or `Program Files (x86)\Tesseract-OCR`) and install Pillow in the same Python environment as Hermes. If OCR prerequisites are missing, extracted image references are marked for review. No image goes to an AI model. Text is cached by image hash and images are revalidated through the existing signed-in session. Review the report’s `inline_image_evidence` and `inline_ocr` fields; image-derived dates are unverified candidates, not official deadlines. Empty text, failed reads and budget limits are not all-clear results. Embedded data URLs, off-site images and unsupported formats are not fetched. The one-page DOM tool does not run OCR.

The crawl is bounded by source operation count and a 180-second worker limit, and can return partial coverage. Report observed operations, source failures, unverified queues, and downloads separately. If worker completion is unknown, inspect local artifacts before retrying. Course inventory, details, change candidates, provenance, checkpoint and downloads stay under the active Hermes profile's private `state/spectrum-extraction/`. Keep one university account per Hermes/browser profile. Local storage is not encrypted by the plugin; remove old reports only according to your retention policy. Tools may put course metadata and download paths in the conversation; sending selected evidence to a model provider is subject to your own privacy and institutional policies.

The crawler uses Hermes's current no-network CDP route resolver to honor session override precedence. The native validator flags that resolver as a private API; if a Hermes build no longer exposes it, the crawler blocks rather than silently using the configured endpoint. No catalog or cross-version compatibility is implied.

There are no submission, posting, production-ingestion, recurring-job or off-platform capabilities. The bundled source adapters do not transfer credentials out of Edge. This release was verified with synthetic course, assessment, forum and file data plus native Hermes plugin checks; live sign-in, theme compatibility and real university file responses have not been accepted for this package. Windows support is a preview: a dedicated `%USERPROFILE%\.hermes\spectrum-edge` Edge user-data directory and an existing loopback listener at `127.0.0.1:9337` are required. Use a private local Hermes profile under your Windows user home with default user-only permissions; shared, redirected, and reparse-point state paths are unsupported. Windows PowerShell must be able to inspect the existing Edge listener and process. Native Windows execution and authenticated Windows scans have not yet been verified. Live crawling remains disabled on Linux and WSL.

## Contributor checks

The GitHub Actions workflow runs offline fixtures only. It does not connect to SPeCTRUM or require account credentials. The release builder uses an explicit file list; never add real course data or browser profiles to this repository.

Build the shareable ZIP with `python3 -B scripts/build_release.py dist`.

```sh
python3 -B -m unittest discover -s tests
node --test tests/test_capture.cjs
hermes plugins doctor --ci .
hermes plugins validate . --json
```

Node is needed only for the JavaScript tests. No external Python dependency is needed by the isolated source worker.
