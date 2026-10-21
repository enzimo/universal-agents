export function isSupportedNodeVersion(version = process.versions.node) {
  const [major = 0, minor = 0] = String(version)
    .split(".")
    .map(Number);
  return major > 24 || (major === 24 && minor >= 5);
}
