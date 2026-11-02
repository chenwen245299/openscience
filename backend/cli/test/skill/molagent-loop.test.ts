import { expect, test } from "bun:test"
import path from "node:path"

const python = process.env.OPENSCIENCE_TEST_RDKIT_PYTHON
test.skipIf(!python)("design rounds score, select a lab batch, ingest QC'd results and refit the surrogate", () => {
  const result = Bun.spawnSync([python!, "-B", path.join(import.meta.dir, "fixture/molagent_loop.py")], {
    stdout: "pipe",
    stderr: "pipe",
  })
  expect(result.exitCode, result.stdout.toString() + result.stderr.toString()).toBe(0)
})
