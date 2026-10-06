import { expect, test } from "bun:test"
import path from "node:path"

const python = process.env.OPENSCIENCE_TEST_RDKIT_PYTHON ?? Bun.which("python3") ?? Bun.which("python")
test.skipIf(!python)(
  "body-reviewed structural evidence guides molecular selection without promoting abstract numbers",
  () => {
    const result = Bun.spawnSync([python!, "-B", path.join(import.meta.dir, "fixture/molagent_evidence.py")], {
      stdout: "pipe",
      stderr: "pipe",
    })
    expect(result.exitCode, result.stdout.toString() + result.stderr.toString()).toBe(0)
  },
)
