export function isSupportedNodeVersion(version = process.versions.node) {
  const [major = 0] = String(version)
    .split(".")
    .map((part) => Number.parseInt(part, 10));
  return major >= 24;
}
