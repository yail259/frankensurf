export function reducedMotion() {
  return matchMedia("(prefers-reduced-motion: reduce)").matches || new URLSearchParams(location.search).get("motion") === "reduced";
}
