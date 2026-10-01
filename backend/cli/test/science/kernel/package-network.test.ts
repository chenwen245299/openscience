import { expect, test } from "bun:test"
import { Config } from "../../../src/config/config"
import { Instance } from "../../../src/project/instance"
import type { PermissionNext } from "../../../src/permission/next"
import { Sandbox } from "../../../src/sandbox/sandbox"
import { KernelEnvironmentMutation } from "../../../src/science/kernel/environment-mutation"
import { KernelRuntime, type KernelIdentity } from "../../../src/science/kernel/registry"
import { PythonTool } from "../../../src/tool/notebook"
import { executionSession, tmpdir } from "../../fixture/fixture"

// A sandboxed policy that denies network, so only an escalated process connects.
async function sandboxed<T>(fn: () => Promise<T>) {
  const config = Config as { trustedSandbox: typeof Config.trustedSandbox }
  const original = config.trustedSandbox
  config.trustedSandbox = async () => ({
    enabled: true,
    network: "deny",
    allowWrite: [],
    onUnavailable: "error",
    requireProjectTrust: false,
  })
  try {
    return await fn()
  } finally {
    config.trustedSandbox = original
  }
}

test("only a package-change process with network granted can open a socket", async () => {
  if (!Sandbox.available()) return
  await using tmp = await tmpdir({ git: true })
  let connections = 0
  const server = Bun.listen({
    hostname: "127.0.0.1",
    port: 0,
    socket: {
      open(socket) {
        connections++
        socket.end()
      },
      data() {},
    },
  })
  try {
    await Instance.provide({
      directory: tmp.path,
      fn: () =>
        sandboxed(async () => {
          const session = await executionSession()
          const identity = (name: string): KernelIdentity => ({
            projectID: Instance.project.id,
            sessionID: session.id,
            name,
            language: "python",
          })
          const probe = [
            "import socket",
            "try:",
            `    socket.create_connection(("127.0.0.1", ${server.port}), timeout=5).close()`,
            '    print("connected")',
            "except OSError as error:",
            '    print("blocked", type(error).__name__)',
          ].join("\n")
          const run = async (name: string, runtime: Promise<Parameters<typeof KernelRuntime.execute>[3]>) => {
            const result = await KernelRuntime.execute(identity(name), probe, { timeout: 30_000 }, await runtime)
            await KernelRuntime.release(identity(name))
            return result.stdout.trim()
          }

          expect(await run("ordinary", KernelEnvironmentMutation.pythonRuntime("python"))).toStartWith("blocked")
          expect(await run("offline", KernelEnvironmentMutation.pythonRuntime("python", true, false))).toStartWith(
            "blocked",
          )
          expect(connections).toBe(0)
          expect(await run("granted", KernelEnvironmentMutation.pythonRuntime("python", true, true))).toBe("connected")
          expect(connections).toBe(1)
        }),
    })
  } finally {
    server.stop(true)
  }
}, 60_000)

test("a package change folds child-process output into its result and keeps the kernel usable", async () => {
  await using tmp = await tmpdir({ git: true })
  await Instance.provide({
    directory: tmp.path,
    fn: async () => {
      const session = await executionSession()
      const identity: KernelIdentity = {
        projectID: Instance.project.id,
        sessionID: session.id,
        name: "capture",
        language: "python",
      }
      const runtime = { ...(await KernelEnvironmentMutation.pythonRuntime("python")), captureProcessOutput: true }
      try {
        const child = await KernelRuntime.execute(
          identity,
          [
            "import subprocess, sys",
            `subprocess.run([sys.executable, "-c", "import sys; print('from the child'); print('child error', file=sys.stderr)"])`,
            'print("from the kernel")',
          ].join("\n"),
          { timeout: 30_000 },
          runtime,
        )
        expect(child.ok).toBe(true)
        expect(child.stdout).toContain("from the kernel")
        expect(child.stdout).toContain("from the child")
        expect(child.stderr).toContain("child error")

        // The descriptors were restored: the protocol still answers.
        const next = await KernelRuntime.execute(identity, 'print("still answering")', { timeout: 30_000 }, runtime)
        expect(next.stdout.trim()).toBe("still answering")
      } finally {
        await KernelRuntime.release(identity)
      }
    },
  })
}, 60_000)

test("an approved bare pip install reaches the index and reports pip's own error", async () => {
  if (!Sandbox.available()) return
  await using tmp = await tmpdir({ git: true })
  const requests: string[] = []
  const index = Bun.serve({
    hostname: "127.0.0.1",
    port: 0,
    fetch(request) {
      requests.push(new URL(request.url).pathname)
      return new Response("not here", { status: 404 })
    },
  })
  try {
    await Instance.provide({
      directory: tmp.path,
      fn: () =>
        sandboxed(async () => {
          const session = await executionSession()
          const tool = await PythonTool.init()
          const approvals: Array<Omit<PermissionNext.Request, "id" | "sessionID" | "tool">> = []
          const context = (callID: string) => ({
            sessionID: session.id,
            messageID: "message_package_network",
            callID,
            agent: "research",
            abort: new AbortController().signal,
            messages: [],
            metadata() {},
            async ask(request: Omit<PermissionNext.Request, "id" | "sessionID" | "tool">) {
              approvals.push(request)
            },
          })
          const identity: KernelIdentity = {
            projectID: Instance.project.id,
            sessionID: session.id,
            name: "python",
            language: "python",
          }
          try {
            const pip = await tool.execute(
              { code: `import importlib.util\nprint(importlib.util.find_spec("pip") is not None)`, timeout: 30_000 },
              context("call_probe"),
            )
            if (pip.output.trim() !== "True") return

            const url = `http://127.0.0.1:${index.port}/simple/`
            const install = await tool.execute(
              {
                code: [
                  "import subprocess, sys",
                  `subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--no-cache-dir", "--retries", "0", "--timeout", "5", "--index-url", "${url}", "--trusted-host", "127.0.0.1", "openscience-never-published"], check=True)`,
                ].join("\n"),
                timeout: 60_000,
              },
              context("call_install"),
            )

            expect(approvals.find((request) => request.permission === "environment_mutation")).toMatchObject({
              metadata: {
                environment_mutation: { warning: expect.stringContaining("may contact package repositories") },
              },
            })
            expect(requests.some((pathname) => pathname.includes("openscience-never-published"))).toBe(true)
            // pip's own diagnosis reaches the model, and the failure is not reported as a change.
            expect(install.output).toContain("No matching distribution found for openscience-never-published")
            expect(install.output).toContain("CalledProcessError")
            expect(install.metadata.restarted).toBeFalsy()

            const seen = requests.length
            const ordinary = await tool.execute(
              {
                code: [
                  "import urllib.request",
                  "try:",
                  `    urllib.request.urlopen("${url}", timeout=5)`,
                  "except Exception as error:",
                  '    print("blocked", type(error).__name__)',
                ].join("\n"),
                timeout: 30_000,
              },
              context("call_ordinary"),
            )
            expect(ordinary.output).toContain("blocked")
            expect(requests.length).toBe(seen)
          } finally {
            await KernelRuntime.release(identity)
          }
        }),
    })
  } finally {
    index.stop(true)
  }
}, 120_000)
