// Unit tests for Web Chat attachment media classification and rendering (chat_attachments.mjs).
import test from "node:test";
import assert from "node:assert/strict";
import {
  DEFAULT_ATTACHMENT_CONTENT_TYPE,
  INLINE_IMAGE_CONTENT_TYPES,
  inferAttachmentKind,
  isInlineAudioContentType,
  isInlineImageContentType,
  isInlineVideoContentType,
  normalizeAttachmentPayload,
  normalizeMediaType,
  renderAttachmentList,
} from "../assistant_gateway/onboarding_web/assets/chat_attachments.mjs";
import { byClass, byTag, fakeDocument } from "./fake_dom.mjs";

const formatSize = (bytes) => `${Number(bytes || 0)} B`;

test("normalizeMediaType strips parameters, maps image/jpg, and rejects malformed values", () => {
  assert.equal(normalizeMediaType("IMAGE/PNG; charset=utf-8"), "image/png");
  assert.equal(normalizeMediaType("image/jpg"), "image/jpeg");
  assert.equal(normalizeMediaType("Image/JPG; quality=90"), "image/jpeg");
  assert.equal(normalizeMediaType("  audio/mpeg  "), "audio/mpeg");
  assert.equal(normalizeMediaType("text/html; charset=utf-8"), "text/html");
  assert.equal(normalizeMediaType(""), DEFAULT_ATTACHMENT_CONTENT_TYPE);
  assert.equal(normalizeMediaType(null), DEFAULT_ATTACHMENT_CONTENT_TYPE);
  assert.equal(normalizeMediaType("not a media type"), DEFAULT_ATTACHMENT_CONTENT_TYPE);
  assert.equal(
    normalizeMediaType("image/png\r\nX-Injected: 1"),
    DEFAULT_ATTACHMENT_CONTENT_TYPE
  );
});

test("inferAttachmentKind derives kind strictly from normalized media type and ignores forged kind", () => {
  assert.deepEqual(INLINE_IMAGE_CONTENT_TYPES, [
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
  ]);
  for (const ct of ["image/png", "image/jpeg", "image/jpg", "image/gif", "image/webp"]) {
    assert.equal(isInlineImageContentType(ct), true, ct);
    assert.equal(inferAttachmentKind({ content_type: ct, kind: "document" }), "image", ct);
  }
  assert.equal(isInlineAudioContentType("audio/wav"), true);
  assert.equal(isInlineAudioContentType("audio/fake+xml"), false);
  assert.equal(isInlineVideoContentType("video/webm"), true);
  assert.equal(isInlineVideoContentType("video/fake+xml"), false);
  assert.equal(inferAttachmentKind({ content_type: "audio/mpeg", kind: "document" }), "audio");
  assert.equal(inferAttachmentKind({ content_type: "video/mp4", kind: "document" }), "video");

  // Forged or legacy kind="image" must never promote non-raster or active types to image.
  for (const ct of [
    "image/svg+xml",
    "text/html; charset=utf-8",
    "image/heic",
    "image/tiff",
    "text/javascript",
    "audio/fake+xml",
    "video/fake+xml",
    "",
    "not a media type",
  ]) {
    assert.equal(
      inferAttachmentKind({ content_type: ct, kind: "image" }),
      "document",
      `expected document for ${JSON.stringify(ct)}`
    );
  }
});

test("normalizeAttachmentPayload normalizes content_type and recomputes kind", () => {
  const normalized = normalizeAttachmentPayload({
    attachment_id: "att_1",
    filename: "fake.png",
    content_type: "text/html; charset=utf-8",
    kind: "image",
    size_bytes: 42,
    url: "/api/web-chat/attachments/att_1",
  });
  assert.equal(normalized.content_type, "text/html");
  assert.equal(normalized.kind, "document");
  assert.equal(normalized.url, "/api/web-chat/attachments/att_1");
});

test("renderAttachmentList renders raster thumbnails, media players, and downloadable chips", () => {
  const doc = fakeDocument();
  const container = doc.createElement("div");
  doc.body.appendChild(container);

  renderAttachmentList(
    container,
    [
      {
        attachment_id: "att_png",
        filename: "photo.png",
        content_type: "image/png",
        kind: "document",
        size_bytes: 120,
        url: "/api/web-chat/attachments/att_png",
      },
      {
        attachment_id: "att_jpg",
        filename: "alias.jpg",
        content_type: "IMAGE/JPG",
        kind: "image",
        size_bytes: 121,
        url: "/api/web-chat/attachments/att_jpg",
      },
      {
        attachment_id: "att_audio",
        filename: "tone.wav",
        content_type: "audio/wav",
        kind: "audio",
        size_bytes: 200,
        url: "/api/web-chat/attachments/att_audio",
      },
      {
        attachment_id: "att_video",
        filename: "clip.webm",
        content_type: "video/webm",
        kind: "video",
        size_bytes: 300,
        url: "/api/web-chat/attachments/att_video",
      },
      {
        attachment_id: "att_svg",
        filename: "vector.svg",
        content_type: "image/svg+xml",
        kind: "image",
        size_bytes: 90,
        url: "/api/web-chat/attachments/att_svg",
      },
      {
        attachment_id: "att_html",
        filename: "spoof.png",
        content_type: "text/html; charset=utf-8",
        kind: "image",
        size_bytes: 80,
        url: "/api/web-chat/attachments/att_html",
      },
    ],
    { document: doc, formatFileSize: formatSize }
  );

  const imageLinks = byClass(container, "message-image-attachment");
  assert.equal(imageLinks.length, 2);
  assert.deepEqual(
    imageLinks.map((node) => node.href),
    ["/api/web-chat/attachments/att_png", "/api/web-chat/attachments/att_jpg"]
  );
  for (const link of imageLinks) {
    assert.equal(link.target, "_blank");
    assert.equal(link.rel, "noopener noreferrer");
    const imgs = byTag(link, "img");
    assert.equal(imgs.length, 1);
    assert.equal(imgs[0].src, link.href);
  }

  const audios = byTag(container, "audio");
  assert.equal(audios.length, 1);
  assert.equal(audios[0].src, "/api/web-chat/attachments/att_audio");
  assert.equal(audios[0].controls, true);

  const videos = byTag(container, "video");
  assert.equal(videos.length, 1);
  assert.equal(videos[0].src, "/api/web-chat/attachments/att_video");
  assert.equal(videos[0].controls, true);

  const chips = byClass(container, "message-file-attachment");
  assert.equal(chips.length, 2);
  assert.deepEqual(
    chips.map((node) => node.href),
    ["/api/web-chat/attachments/att_svg", "/api/web-chat/attachments/att_html"]
  );
  assert.deepEqual(
    chips.map((node) => byClass(node, "message-file-label")[0].textContent),
    ["vector.svg", "spoof.png"]
  );
  assert.deepEqual(
    chips.map((node) => byClass(node, "message-file-meta")[0].textContent),
    ["document · 90 B", "document · 80 B"]
  );
});
