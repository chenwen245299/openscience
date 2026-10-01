/** The version the sidecar must report before startup counts as healthy, or
 * undefined when any version will do.
 *
 * A supervised update relaunch still proves it booted the exact runtime it
 * shipped, so it pins the shell's own version. Every ordinary launch — a
 * packaged build, a shell run from source, or one pointed at a sidecar through
 * OPENSCIENCE_DESKTOP_SIDECAR — only checks that the runtime is live, so a
 * runtime built separately (which reports its own build stamp, e.g.
 * `0.0.0-main-<timestamp>`) is accepted. This lets a self-built desktop bundle
 * ship a from-source sidecar without a version-pin startup failure. */
export function pinnedVersion(shell) {
  if (shell.supervised) return shell.version
  return
}

/** Whether a `/global/health` body proves a live runtime, at `version` when one
 * is pinned. */
export function healthyRuntime(health, version) {
  if (health?.healthy !== true) return false
  if (typeof health.runId !== "string" || !health.runId) return false
  return version === undefined || health.version === version
}
