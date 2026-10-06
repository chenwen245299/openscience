import { normalizeFilePath } from "./file-sources"
import { ARTIFACT_FILES, parseArtifact, parseProgress, type Artifact, type Provenance } from "./mol-pipeline"
import type { SavedPipeline } from "./mol-pipeline-history"

export type PipelineNode = { name: string; absolute: string; type: "file" | "directory"; mtime?: number }
export type PipelineFiles = {
  read: (path: string) => Promise<string | undefined>
  list: (path: string) => Promise<PipelineNode[]>
}

export async function readPipeline(dir: string, files: PipelineFiles): Promise<SavedPipeline | undefined> {
  const [body, provenance] = await Promise.all([
    files.read(`${dir}/progress.json`),
    files.read(`${dir}/provenance.jsonl`),
  ])
  const run = body ? parseProgress(body) : undefined
  if (!run) return
  const trace = provenance?.split("\n").flatMap((line): Provenance[] => {
    try {
      const value = JSON.parse(line) as Partial<Provenance> | null
      return value && typeof value.stage === "string" && typeof value.action === "string" ? [value as Provenance] : []
    } catch {
      return []
    }
  })
  const entries = await Promise.all(
    run.stages
      .filter((stage) => stage.status === "ok" && ARTIFACT_FILES[stage.key])
      .map(async (stage) => {
        const content = await files.read(`${dir}/${ARTIFACT_FILES[stage.key]}`)
        return [stage.key, content ? parseArtifact(content) : undefined] as const
      }),
  )
  const artifacts = Object.fromEntries(entries.filter((entry): entry is readonly [string, Artifact] => !!entry[1]))
  return { dir, run, trace, artifacts }
}

/** Bounded discovery recovers older runs; remembered directories bypass this walk. */
export async function discoverPipelines(
  roots: string[],
  files: PipelineFiles,
  live: () => boolean = () => true,
): Promise<SavedPipeline[]> {
  const seen = new Set<string>()
  const found = new Set<string>()
  const scan = { frontier: roots, remaining: 192 }
  for (let depth = 0; depth < 6 && scan.frontier.length && scan.remaining > 0 && live(); depth++) {
    const dirs = scan.frontier.filter((dir) => !seen.has(normalizeFilePath(dir))).slice(0, scan.remaining)
    dirs.forEach((dir) => seen.add(normalizeFilePath(dir)))
    scan.remaining -= dirs.length
    const next: PipelineNode[] = []
    for (let index = 0; index < dirs.length && live(); index += 8) {
      const listings = await Promise.all(dirs.slice(index, index + 8).map((dir) => files.list(dir)))
      if (!live()) return []
      listings.forEach((entries, offset) => {
        for (const entry of entries) {
          if (entry.type === "file" && entry.name === "progress.json") found.add(dirs[index + offset])
          if (
            entry.type === "directory" &&
            !entry.name.startsWith(".") &&
            entry.name !== "node_modules" &&
            entry.name !== "__pycache__"
          )
            next.push(entry)
        }
      })
    }
    scan.frontier = next.sort((left, right) => (right.mtime ?? 0) - (left.mtime ?? 0)).map((entry) => entry.absolute)
  }
  if (!live()) return []
  const runs = await Promise.all([...found].map((dir) => readPipeline(dir, files)))
  return runs
    .filter((run): run is SavedPipeline => !!run)
    .sort((left, right) => left.run.updated.localeCompare(right.run.updated))
}
