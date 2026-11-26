/**
 * Shared media-type classification and attachment rendering for Web Chat.
 *
 * Mirrors ``assistant_gateway/attachment_media.py`` so the UI and server agree
 * on which media types display as inline raster thumbnails, audio/video
 * players, or downloadable file chips. ``INLINE_IMAGE_CONTENT_TYPES`` is
 * pinned against Python's allowlist by ``tests/test_attachment_media_policy.py``.
 */

export const DEFAULT_ATTACHMENT_CONTENT_TYPE = "application/octet-stream";

export const INLINE_IMAGE_CONTENT_TYPES = Object.freeze([
  "image/png",
  "image/jpeg",
  "image/gif",
  "image/webp",
]);

const INLINE_IMAGE_SET = new Set(INLINE_IMAGE_CONTENT_TYPES);
const MEDIA_TYPE_ALIASES = Object.freeze({
  "image/jpg": "image/jpeg",
});
const MEDIA_TYPE_PATTERN = /^[a-z0-9!#$&^_.+-]+\/[a-z0-9!#$&^_.+-]+$/;
const SVG_NS = "http://www.w3.org/2000/svg";

export function normalizeMediaType(contentType) {
  const raw = String(contentType || "");
  for (let i = 0; i < raw.length; i += 1) {
    const code = raw.charCodeAt(i);
    if (code < 0x20 || code === 0x7f) {
      return DEFAULT_ATTACHMENT_CONTENT_TYPE;
    }
  }
  const candidate = raw.split(";", 1)[0].trim().toLowerCase();
  if (!candidate || !MEDIA_TYPE_PATTERN.test(candidate)) {
    return DEFAULT_ATTACHMENT_CONTENT_TYPE;
  }
  return MEDIA_TYPE_ALIASES[candidate] || candidate;
}

export function isInlineImageContentType(contentType) {
  return INLINE_IMAGE_SET.has(normalizeMediaType(contentType));
}

export function isInlineAudioContentType(contentType) {
  const normalized = normalizeMediaType(contentType);
  return normalized.startsWith("audio/") && !normalized.endsWith("+xml");
}

export function isInlineVideoContentType(contentType) {
  const normalized = normalizeMediaType(contentType);
  return normalized.startsWith("video/") && !normalized.endsWith("+xml");
}

/**
 * Derive attachment display kind strictly from the normalized media type.
 *
 * Never trusts ``fileOrAttachment.kind`` so legacy or forged ``kind="image"``
 * records with SVG, HTML, or unknown content types always render as
 * downloadable file chips rather than ``<img>`` thumbnails.
 */
export function inferAttachmentKind(fileOrAttachment) {
  const source = fileOrAttachment || {};
  const normalized = normalizeMediaType(source.content_type || source.type || "");
  if (INLINE_IMAGE_SET.has(normalized)) {
    return "image";
  }
  if (isInlineAudioContentType(normalized)) {
    return "audio";
  }
  if (isInlineVideoContentType(normalized)) {
    return "video";
  }
  return "document";
}

export function normalizeAttachmentPayload(attachment) {
  const source = attachment || {};
  const normalizedContentType = normalizeMediaType(
    source.content_type || source.type || ""
  );
  return {
    attachment_id: String(source.attachment_id || source.id || ""),
    filename: String(source.filename || source.name || "attachment"),
    content_type: normalizedContentType,
    kind: inferAttachmentKind({ content_type: normalizedContentType }),
    size_bytes: Number(source.size_bytes || source.size || 0),
    sha256: String(source.sha256 || ""),
    url: String(source.url || source.preview_url || ""),
    preview_url: String(source.preview_url || source.url || ""),
  };
}

function createFileIconElement(doc) {
  const svg = doc.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  const bodyPath = doc.createElementNS(SVG_NS, "path");
  bodyPath.setAttribute(
    "d",
    "M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Z"
  );
  const foldPath = doc.createElementNS(SVG_NS, "path");
  foldPath.setAttribute("d", "M14 2v6h6");
  svg.appendChild(bodyPath);
  svg.appendChild(foldPath);
  return svg;
}

export function renderAttachmentList(
  container,
  attachments,
  { document: doc, formatFileSize }
) {
  const normalized = (Array.isArray(attachments) ? attachments : []).map(
    normalizeAttachmentPayload
  );
  if (!normalized.length) {
    return null;
  }
  const rail = doc.createElement("div");
  rail.className = "message-attachments";
  normalized.forEach((attachment) => {
    const href = attachment.url || attachment.preview_url || "";
    if (attachment.kind === "image" && href) {
      const link = doc.createElement("a");
      link.className = "message-image-attachment";
      link.href = href;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.title = attachment.filename;
      const image = doc.createElement("img");
      image.src = href;
      image.alt = attachment.filename;
      link.appendChild(image);
      rail.appendChild(link);
      return;
    }
    if (attachment.kind === "audio" && href) {
      const audio = doc.createElement("audio");
      audio.className = "message-media-attachment";
      audio.controls = true;
      audio.src = href;
      rail.appendChild(audio);
      return;
    }
    if (attachment.kind === "video" && href) {
      const video = doc.createElement("video");
      video.className = "message-media-attachment";
      video.controls = true;
      video.src = href;
      rail.appendChild(video);
      return;
    }
    const chip = doc.createElement(href ? "a" : "span");
    chip.className = "message-file-attachment";
    if (href) {
      chip.href = href;
      chip.target = "_blank";
      chip.rel = "noopener noreferrer";
    }
    const icon = doc.createElement("span");
    icon.className = "message-file-icon";
    icon.appendChild(createFileIconElement(doc));
    const label = doc.createElement("span");
    label.className = "message-file-label";
    label.textContent = attachment.filename;
    const meta = doc.createElement("span");
    meta.className = "message-file-meta";
    meta.textContent = [attachment.kind, formatFileSize(attachment.size_bytes)]
      .filter(Boolean)
      .join(" · ");
    chip.appendChild(icon);
    chip.appendChild(label);
    chip.appendChild(meta);
    rail.appendChild(chip);
  });
  container.appendChild(rail);
  return rail;
}
