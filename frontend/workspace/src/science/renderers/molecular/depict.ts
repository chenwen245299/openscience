/**
 * One RDKit.js worker, shared by everything that draws a molecule.
 *
 * The worker holds a single WASM instance and keeps RDKit's Emscripten glue
 * out of the application page, so it is worth having exactly one of. This
 * module owns it: the `chem-2d` artifact renderer and the design-run pane's
 * structure thumbnails both call `renderMolecule` here rather than each
 * starting a worker of their own.
 *
 * A failed or timed-out worker is discarded so the next call starts a fresh
 * one instead of inheriting a wedged instance.
 */

export interface Spec {
  input: string
  width: number
  height: number
}

type WorkerResponse = { ok: true; svg: string } | { ok: false; error: string }

let rdkitWorker: Worker | undefined
const WORKER_TIMEOUT_MS = 30_000

function getRDKitWorker(): Worker {
  rdkitWorker ??= new Worker(new URL("./rdkit.worker.ts", import.meta.url), {
    type: "module",
    name: "openscience-rdkit",
  })
  return rdkitWorker
}

export function renderMolecule(spec: Spec): Promise<string> {
  const worker = getRDKitWorker()
  const channel = new MessageChannel()

  return new Promise((resolve, reject) => {
    const discardWorker = () => {
      if (rdkitWorker !== worker) return
      worker.terminate()
      rdkitWorker = undefined
    }
    const cleanup = () => {
      clearTimeout(timeout)
      channel.port1.close()
      worker.removeEventListener("error", onWorkerError)
    }
    const onWorkerError = (event: ErrorEvent) => {
      cleanup()
      discardWorker()
      reject(new Error(event.message || "RDKit worker failed"))
    }
    const timeout = setTimeout(() => {
      cleanup()
      discardWorker()
      reject(new Error("RDKit worker timed out"))
    }, WORKER_TIMEOUT_MS)

    channel.port1.onmessage = (event: MessageEvent<WorkerResponse>) => {
      cleanup()
      if (event.data.ok) resolve(event.data.svg)
      else reject(new Error(event.data.error))
    }
    channel.port1.onmessageerror = () => {
      cleanup()
      discardWorker()
      reject(new Error("RDKit worker returned an unreadable response"))
    }
    worker.addEventListener("error", onWorkerError, { once: true })
    channel.port1.start()
    try {
      worker.postMessage(spec, [channel.port2])
    } catch (error) {
      cleanup()
      reject(error)
    }
  })
}

/** Strip fixed width/height so the SVG scales to its container (viewBox is kept). */
export function responsive(svg: string): string {
  return svg
    .replace(/(<svg[^>]*?)\s+width=(['"])[^'"]*\2/i, "$1")
    .replace(/(<svg[^>]*?)\s+height=(['"])[^'"]*\2/i, "$1")
}
