import { expect, test } from "bun:test"
import path from "node:path"

const python = process.env.OPENSCIENCE_TEST_RDKIT_PYTHON
test.skipIf(!python)("molecular shortlist reasons describe real diversity, gate and SA decisions", () => {
  const result = Bun.spawnSync([python!, "-B", path.join(import.meta.dir, "fixture/molagent_selection.py")], {
    stdout: "pipe",
    stderr: "pipe",
  })
  expect(result.exitCode, result.stdout.toString() + result.stderr.toString()).toBe(0)
})
