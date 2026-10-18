//! Exercise both AnyDoc's package reader and Calamine through the shipped CLI.
use std::fs;
use std::io::{Cursor, Write};
use std::process::{Command, Output};
use std::sync::atomic::{AtomicU64, Ordering};

use zip::{CompressionMethod, ZipWriter, write::SimpleFileOptions};

static NEXT_ID: AtomicU64 = AtomicU64::new(0);

fn archive(parts: &[(&str, &str)]) -> Vec<u8> {
    let mut writer = ZipWriter::new(Cursor::new(Vec::new()));
    let options = SimpleFileOptions::default().compression_method(CompressionMethod::Deflated);
    for (name, content) in parts {
        writer
            .start_file(*name, options)
            .expect("start archive part");
        writer.write_all(content.as_bytes()).expect("write part");
    }
    writer.finish().expect("finish archive").into_inner()
}

fn convert(extension: &str, bytes: &[u8]) -> Output {
    let id = NEXT_ID.fetch_add(1, Ordering::Relaxed);
    let directory =
        std::env::temp_dir().join(format!("ua-archive-conversion-{}-{id}", std::process::id()));
    fs::create_dir_all(&directory).expect("create fixture directory");
    let input = directory.join(format!("fixture.{extension}"));
    fs::write(&input, bytes).expect("write archive");
    let output = Command::new(env!("CARGO_BIN_EXE_anydoc"))
        .arg(&input)
        .output()
        .expect("run anydoc");
    fs::remove_dir_all(directory).expect("remove fixture directory");
    output
}

fn spreadsheet_parts() -> Vec<(&'static str, &'static str)> {
    vec![
        (
            "_rels/.rels",
            r#"<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>"#,
        ),
        (
            "[Content_Types].xml",
            r#"<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/></Types>"#,
        ),
        (
            "xl/workbook.xml",
            r#"<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Scores" sheetId="1" r:id="rId1"/></sheets></workbook>"#,
        ),
        (
            "xl/_rels/workbook.xml.rels",
            r#"<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>"#,
        ),
        (
            "xl/worksheets/sheet1.xml",
            r#"<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><dimension ref="A1:A1"/><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>Archive marker</t></is></c></row></sheetData></worksheet>"#,
        ),
    ]
}

#[test]
fn converts_compressed_document_formats() {
    let cases = [
        (
            "docx",
            archive(&[(
                "word/document.xml",
                r#"<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Archive marker</w:t></w:r></w:p></w:body></w:document>"#,
            )]),
        ),
        (
            "odt",
            archive(&[(
                "content.xml",
                r#"<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"><office:body><office:text><text:p>Archive marker</text:p></office:text></office:body></office:document-content>"#,
            )]),
        ),
        (
            "ods",
            archive(&[(
                "content.xml",
                r#"<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"><office:body><office:spreadsheet><table:table table:name="Scores"><table:table-row><table:table-cell><text:p>Archive marker</text:p></table:table-cell></table:table-row></table:table></office:spreadsheet></office:body></office:document-content>"#,
            )]),
        ),
        (
            "epub",
            archive(&[
                (
                    "META-INF/container.xml",
                    r#"<container><rootfiles><rootfile full-path="book.opf"/></rootfiles></container>"#,
                ),
                (
                    "book.opf",
                    r#"<package><manifest><item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/></manifest><spine><itemref idref="chapter"/></spine></package>"#,
                ),
                (
                    "chapter.xhtml",
                    "<html><body><p>Archive marker</p></body></html>",
                ),
            ]),
        ),
        (
            "pptx",
            archive(&[
                (
                    "ppt/presentation.xml",
                    r#"<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><p:sldIdLst><p:sldId id="256" r:id="rId1"/></p:sldIdLst></p:presentation>"#,
                ),
                (
                    "ppt/_rels/presentation.xml.rels",
                    r#"<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide1.xml"/></Relationships>"#,
                ),
                (
                    "ppt/slides/slide1.xml",
                    r#"<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><p:cSld><p:spTree><p:sp><p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>Archive marker</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld></p:sld>"#,
                ),
            ]),
        ),
        ("xlsx", archive(&spreadsheet_parts())),
    ];
    for (extension, bytes) in cases {
        let result = convert(extension, &bytes);
        assert!(
            result.status.success(),
            "{extension}: {}",
            String::from_utf8_lossy(&result.stderr)
        );
        assert!(
            String::from_utf8_lossy(&result.stdout).contains("Archive marker"),
            "{extension}: {}",
            String::from_utf8_lossy(&result.stdout)
        );
    }
}

#[test]
fn legacy_non_utf8_archive_name_preserves_conversion() {
    let mut parts = spreadsheet_parts();
    // ZIP 9 decodes non-UTF-8 names as CP437. An unused legacy entry must not
    // prevent the workbook's valid document parts from being read.
    parts.push(("ébad.txt", "unused"));
    let mut bytes = archive(&parts);
    let name = "ébad.txt".as_bytes();
    let offsets: Vec<_> = bytes
        .windows(name.len())
        .enumerate()
        .filter_map(|(offset, candidate)| (candidate == name).then_some(offset))
        .collect();
    assert_eq!(offsets.len(), 2, "local and central directory names");
    for offset in offsets {
        bytes[offset] = 0xff;
    }
    let result = convert("xlsx", &bytes);
    assert!(
        result.status.success(),
        "{}",
        String::from_utf8_lossy(&result.stderr)
    );
    assert!(String::from_utf8_lossy(&result.stdout).contains("Archive marker"));
}

#[test]
fn truncated_archive_returns_a_conversion_error() {
    let mut bytes = archive(&spreadsheet_parts());
    bytes.truncate(bytes.len() - 22); // Remove the end-of-central-directory record.
    let result = convert("xlsx", &bytes);
    assert_eq!(result.status.code(), Some(1), "must return a normal error");
    assert!(String::from_utf8_lossy(&result.stderr).starts_with("anydoc:"));
}
