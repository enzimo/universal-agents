---
name: convert-documents-to-markdown
description: Converts local Word, PowerPoint, Excel, OpenDocument, RTF, EPUB, CSV, and text-based PDF files to GitHub-Flavored Markdown with the container-installed native AnyDoc CLI. Use when an agent needs readable Markdown from a supported local document without uploading it to an external service.
---

# Convert Documents to Markdown

Use the locally installed native `anydoc` executable. Keep document conversion
inside the runtime; do not invoke `npx`, npm, PyPI, Cargo installers, or the
hosted Firecrawl Parse API as a fallback.

## Universal Agents Shell Behavior

Before running conversion commands through Universal Agents, load
`shell-execution-workflows`. Pass the underlying command without an `rtk`
prefix, set `work_dir` explicitly, keep input and output paths inside
this task's folders (the person's folder and the shared `team/` and `agents/`
folders), and use an explicit timeout for large documents. Inspect
status, exit code, separate output/error, termination reason, RTK mode, and
warnings before diagnosing a conversion failure.

## Workflow

1. Confirm the local binary before the first conversion in a task:

```bash
anydoc --version
```

If it is unavailable, report that the current runtime does not provide the
local AnyDoc capability. Do not download a replacement implicitly.

2. Convert one document per invocation. Prefer an output file for all but small
documents so large Markdown does not flood tool or model context:

```bash
anydoc report.docx --output report.md
anydoc slides.pptx -o slides.md
anydoc data.csv -o data.md
```

To inspect a small document directly, omit `--output` and read Markdown from
stdout:

```bash
anydoc notes.rtf
```

3. Let AnyDoc detect the format from content, using the filename extension as
the fallback. Pass `--format` only for signature-less stdin input or a missing
or incorrect extension:

```bash
anydoc - --format csv < data.csv
anydoc unknown.bin --format docx -o unknown.md
```

4. Confirm that the output exists and is non-empty. Inspect representative
headings, tables, lists, and the sections relevant to the task before treating
the conversion as complete.

## Supported Inputs

Use AnyDoc for `.doc`, `.docx`, `.docm`, `.odt`, `.rtf`, `.epub`, `.pdf`,
`.ppt`, `.pps`, `.pot`, `.pptx`, `.pptm`, `.ppsx`, `.ppsm`, `.odp`, `.xls`,
`.xlsx`, `.xlsm`, `.xlsb`, `.ods`, and `.csv` files.

Use the `markitdown` skill instead when the source is a URL, HTML, JSON, XML,
an image, audio, an archive, an Outlook message, YouTube content, or another
format outside AnyDoc's local input set. Also use the MarkItDown workflow when
the task explicitly requires a configured plugin or OCR path.

## Failure Handling

- Interpret exit code `0` as success, `1` as a read/conversion failure, and `2`
  as invalid command usage.
- Read the single `anydoc: ...` diagnostic from stderr and change the input,
  format hint, path, or hypothesis before retrying.
- Treat encrypted, malformed, unsupported, and resource-limited documents as
  conversion failures; do not claim partial extraction unless output was
  actually produced and inspected.
- Treat scanned and image-only PDFs as unsupported because local AnyDoc does
  not perform OCR. State that limitation clearly instead of silently calling a
  hosted service.
