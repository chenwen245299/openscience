import { expect, test } from "bun:test"
import path from "node:path"

const python = Bun.which("python3") ?? Bun.which("python")
test.skipIf(!python)(
  "molecular literature retrieval records selected and excluded papers without losing source failures",
  () => {
    const result = Bun.spawnSync([python!, "-B", "-S", path.join(import.meta.dir, "fixture/molagent_literature.py")], {
      stdout: "pipe",
      stderr: "pipe",
    })
    expect(result.exitCode, result.stdout.toString() + result.stderr.toString()).toBe(0)
  },
)
