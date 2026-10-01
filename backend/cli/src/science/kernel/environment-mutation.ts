import { Global } from "@/global"
import { OpenScience } from "@/openscience"
import { Instance } from "@/project/instance"
import { ManagedEnvironments } from "@/science/kernel/environment-manager"
import { pythonEnvironment } from "@/science/kernel/interpreter"
import type { KernelStartOptions } from "@/science/kernel/types"
import { createHash } from "node:crypto"
import { constants, mkdirSync } from "node:fs"
import { access, readdir, realpath } from "node:fs/promises"
import path from "node:path"
import z from "zod"

const PythonProbe = z.object({
  binary: z.string(),
  major: z.number().int().nonnegative(),
  minor: z.number().int().nonnegative(),
  packages: z.record(z.string(), z.boolean()),
})

type PythonProbe = z.infer<typeof PythonProbe>

const PYTHON_PACKAGES = [
  "numpy",
  "scipy",
  "pandas",
  "matplotlib",
  "sklearn",
  "statsmodels",
  "xarray",
  "torch",
  "Bio",
  "rdkit",
] as const

const pythonCache: { value?: Promise<string | undefined> } = {}

/** Prefer the broadest already-installed scientific stack, then the newest
 * stable interpreter. This avoids selecting a bleeding-edge PATH Python that
 * cannot run the packages already available in another trusted installation. */
export function rankPython(probes: PythonProbe[]) {
  return probes.toSorted((a, b) => {
    const count = (probe: PythonProbe) => Object.values(probe.packages).filter(Boolean).length
    const stable = (probe: PythonProbe) => (probe.major === 3 && probe.minor <= 13 ? 1 : 0)
    return count(b) - count(a) || stable(b) - stable(a) || b.major - a.major || b.minor - a.minor
  })[0]?.binary
}

async function pythonCandidates() {
  const names = process.platform === "win32" ? ["python.exe"] : ["python3", "python"]
  const versions =
    process.platform === "darwin"
      ? await readdir("/Library/Frameworks/Python.framework/Versions", { withFileTypes: true }).catch(() => [])
      : []
  const dirs = [
    ...(process.env.PATH ?? "").split(path.delimiter),
    ...versions
      .filter((item) => item.isDirectory())
      .map((item) => `/Library/Frameworks/Python.framework/Versions/${item.name}/bin`),
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "/opt/anaconda3/bin",
    "/opt/miniconda3/bin",
  ].filter(Boolean)
  const files = [...new Set(dirs.flatMap((dir) => names.map((name) => path.join(dir, name))))]
  const resolved = await Promise.all(
    files.map((file) =>
      access(file, process.platform === "win32" ? constants.F_OK : constants.X_OK)
        .then(() => realpath(file))
        .catch(() => undefined),
    ),
  )
  return [...new Set(resolved.filter((file): file is string => !!file))]
}

async function pythonProbe(binary: string): Promise<PythonProbe | undefined> {
  const code = [
    "import importlib.util, json, sys",
    `names = ${JSON.stringify(PYTHON_PACKAGES)}`,
    'print(json.dumps({"major": sys.version_info.major, "minor": sys.version_info.minor, "packages": {name: importlib.util.find_spec(name) is not None for name in names}}))',
  ].join("\n")
  const proc = Bun.spawn([binary, "-I", "-c", code], { stdout: "pipe", stderr: "ignore" })
  const timer = setTimeout(() => proc.kill(), 3_000)
  const output = await new Response(proc.stdout)
    .text()
    .then((text) => JSON.parse(text) as unknown)
    .catch(() => undefined)
  const exit = await proc.exited.catch(() => -1)
  clearTimeout(timer)
  if (exit !== 0) return
  const parsed = PythonProbe.safeParse({ ...(typeof output === "object" && output ? output : {}), binary })
  if (!parsed.success) return
  return parsed.data
}

async function hostPython() {
  const select = async () => {
    const candidates = await pythonCandidates()
    const probes = await Promise.all(candidates.map(pythonProbe))
    return rankPython(probes.filter((probe): probe is PythonProbe => !!probe)) ?? candidates[0]
  }
  const pending = pythonCache.value ?? select()
  pythonCache.value = pending
  return pending
}

export namespace KernelEnvironmentMutation {
  export type Language = "python" | "r"

  export const SubprocessEnvironment = z.object({
    target: z.enum(["local", "ssh", "modal"]),
    cwd: z.string().optional(),
    profile: z.string().optional(),
    python: z
      .object({
        role: z.enum(["selected_default", "capability"]),
        executable: z.string().optional(),
        version: z.string().optional(),
      })
      .optional()
      .describe("Selected runtime, not proof that arbitrary shell code used Python. Missing fields were not measured."),
  })
  export type SubprocessEnvironment = z.infer<typeof SubprocessEnvironment>

  export function subprocessIdentity(runtime: KernelStartOptions, cwd: string): SubprocessEnvironment {
    return {
      target: "local",
      cwd,
      profile: runtime.environmentName ?? "python",
      python: { role: "selected_default", ...(runtime.binary ? { executable: runtime.binary } : {}) },
    }
  }

  /** Restore only resolver-owned Python paths after the ordinary subprocess
   * filter. Reuse this final overlay for both the launched command and its
   * runtime-version probe; arbitrary ambient PYTHONPATH remains excluded. */
  export function subprocessEnv(runtime: Awaited<ReturnType<typeof pythonSubprocessRuntime>>, env: NodeJS.ProcessEnv) {
    return {
      // Python run by the agent is headless and should leave no trace in the
      // person's project: bytecode goes to OpenScience's cache instead of a
      // __pycache__ beside every script, and plots render off-screen rather
      // than opening windows or reading a stray interactive matplotlibrc.
      PYTHONPYCACHEPREFIX: env.PYTHONPYCACHEPREFIX ?? path.join(Global.Path.cache, "pycache"),
      MPLBACKEND: env.MPLBACKEND ?? "Agg",
      ...OpenScience.filterEnvForSubprocess({ ...env, ...runtime.env }),
      PYTHONPATH: runtime.env.PYTHONPATH,
      GIT_CONFIG_NOSYSTEM: env.GIT_CONFIG_NOSYSTEM,
      GIT_CONFIG_GLOBAL: env.GIT_CONFIG_GLOBAL,
      GIT_TERMINAL_PROMPT: env.GIT_TERMINAL_PROMPT,
    }
  }

  export type Plan = {
    language: Language
    environment: string
    operation: "package_install" | "package_remove" | "environment_update"
    manager: string
    digest: string
    restart: true
    /** The package names the command names, best effort, for the card. */
    packages?: string[]
    /** Whether the change may reach package repositories: only when the code
     * is nothing but the install command that the approval card names. */
    network: boolean
  }

  type Literal = string | { list: Literal[] } | { word: string }

  const word = /[A-Za-z_][\w.]*(?:::[A-Za-z_][\w.]*)?|\d+(?:\.\d+)?/y

  /** Source as tokens when it holds only names, plain string literals and call
   * punctuation, with statement breaks as ";". Anything else (operators,
   * escapes, f-strings, multi-line strings) is not a plain command. */
  function tokenize(code: string): string[] | undefined {
    const tokens: string[] = []
    let depth = 0
    let index = 0
    while (index < code.length) {
      const char = code[index]!
      if (char === "#") {
        while (index < code.length && code[index] !== "\n") index++
        continue
      }
      if (char === "\n" || char === ";") {
        if (!depth) tokens.push(";")
        index++
        continue
      }
      if (/\s/.test(char)) {
        index++
        continue
      }
      if (char === '"' || char === "'") {
        const end = code.indexOf(char, index + 1)
        if (end < 0) return
        const body = code.slice(index + 1, end)
        if (/[\\\n]/.test(body)) return
        tokens.push(char + body + char)
        index = end + 1
        continue
      }
      if ("[](),=".includes(char)) {
        depth += char === "[" || char === "(" ? 1 : char === "]" || char === ")" ? -1 : 0
        if (depth < 0) return
        tokens.push(char)
        index++
        continue
      }
      word.lastIndex = index
      const match = word.exec(code)
      if (!match) return
      tokens.push(match[0])
      index += match[0].length
    }
    return depth ? undefined : tokens
  }

  function statements(tokens: string[]) {
    const split: string[][] = [[]]
    for (const token of tokens) {
      if (token === ";") split.push([])
      else split[split.length - 1]!.push(token)
    }
    return split.filter((statement) => statement.length)
  }

  function literal(tokens: string[], at: number): [Literal, number] | undefined {
    const token = tokens[at]
    if (token === undefined) return
    if (token.startsWith('"') || token.startsWith("'")) return [token.slice(1, -1), at + 1]
    const vector = token === "c" && tokens[at + 1] === "("
    if (token === "[" || vector) {
      const close = vector ? ")" : "]"
      const items: Literal[] = []
      let cursor = at + (vector ? 2 : 1)
      while (tokens[cursor] !== close) {
        const item = literal(tokens, cursor)
        if (!item) return
        items.push(item[0])
        cursor = item[1]
        if (tokens[cursor] === ",") cursor++
        else if (tokens[cursor] !== close) return
      }
      return [{ list: items }, cursor + 1]
    }
    if (/^(?:True|False|None|TRUE|FALSE|NULL|\d+(?:\.\d+)?|sys\.executable)$/.test(token)) {
      return [{ word: token }, at + 1]
    }
  }

  /** One statement as a call whose arguments are all literals. */
  function call(statement: string[]) {
    const assigned = statement[1] === "=" && /^[A-Za-z_]\w*$/.test(statement[0] ?? "")
    const callee = statement[assigned ? 2 : 0]
    if (!callee || statement[assigned ? 3 : 1] !== "(") return
    const positional: Literal[] = []
    const named = new Map<string, Literal>()
    let cursor = assigned ? 4 : 2
    while (statement[cursor] !== ")") {
      const key =
        statement[cursor + 1] === "=" && /^[A-Za-z_][\w.]*$/.test(statement[cursor] ?? "")
          ? statement[cursor]
          : undefined
      const value = literal(statement, key ? cursor + 2 : cursor)
      if (!value) return
      if (key) named.set(key, value[0])
      else positional.push(value[0])
      cursor = value[1]
      if (statement[cursor] === ",") cursor++
      else if (statement[cursor] !== ")") return
    }
    if (cursor !== statement.length - 1) return
    return { callee, positional, named }
  }

  const pipCallers = new Set(["subprocess.run", "subprocess.check_call", "subprocess.call"])
  const pipOptions = new Set(["check", "capture_output", "text", "timeout", "universal_newlines", "encoding"])
  // Options that point an install at another interpreter or out of the managed
  // package root, toward the user's own Python, never get network, whatever the
  // sandbox would allow. pip accepts unambiguous prefixes of long options and
  // clustered short ones, so those count too.
  const relocating = ["--target", "--prefix", "--root", "--user", "--break-system-packages", "--python"]

  function relocates(item: string) {
    if (/^-[^-]/.test(item)) return item.slice(1).includes("t")
    const name = item.split("=")[0]!
    return name.startsWith("--") && name.length > 2 && relocating.some((option) => option.startsWith(name))
  }

  function pipCommands(parts: string[][]) {
    const packages: string[] = []
    let calls = 0
    for (const statement of parts) {
      if (statement[0] === "import") {
        const names = statement.slice(1)
        const valid = names.every((name, index) => (index % 2 ? name === "," : name === "subprocess" || name === "sys"))
        if (!valid || names.length % 2 === 0) return
        continue
      }
      const parsed = call(statement)
      if (!parsed || !pipCallers.has(parsed.callee) || parsed.positional.length !== 1) return
      if ([...parsed.named.keys()].some((key) => !pipOptions.has(key))) return
      const argv = parsed.positional[0]
      if (typeof argv !== "object" || !("list" in argv)) return
      const [interpreter, flag, module, ...args] = argv.list
      if (typeof interpreter !== "object" || !("word" in interpreter) || interpreter.word !== "sys.executable") return
      if (flag !== "-m" || module !== "pip") return
      if (!args.every((item): item is string => typeof item === "string")) return
      if (args.some(relocates)) return
      const verb = args.findIndex((item) => !item.startsWith("-"))
      if (args[verb] !== "install" && args[verb] !== "download") return
      packages.push(...args.slice(verb + 1).filter((item) => !item.startsWith("-")))
      calls++
    }
    return calls ? packages : undefined
  }

  const rInstallers = new Set(["install.packages", "BiocManager::install", "pak::pkg_install", "renv::install"])

  function rCommands(parts: string[][]) {
    const packages: string[] = []
    for (const statement of parts) {
      const parsed = call(statement)
      if (!parsed || !rInstallers.has(parsed.callee) || parsed.positional.length > 1) return
      if (["lib", "lib.loc", "destdir"].some((key) => parsed.named.has(key))) return
      const first = parsed.positional[0] ?? parsed.named.get("pkgs") ?? parsed.named.get("pkg")
      const names = typeof first === "string" ? [first] : typeof first === "object" && "list" in first ? first.list : []
      if (!names.length || !names.every((name): name is string => typeof name === "string")) return
      packages.push(...names)
    }
    return packages
  }

  // An approval card names packages, not code. A change may therefore reach
  // the network only when its execution is nothing but install commands with
  // literal arguments; otherwise a prompt-injected model could ship other code
  // under an install card. Returns the packages named, or undefined.
  function command(language: Language, code: string) {
    const tokens = tokenize(code)
    const parts = tokens ? statements(tokens) : []
    if (!parts.length) return
    return language === "python" ? pipCommands(parts) : rCommands(parts)
  }

  function normalized(code: string) {
    return code
      .replace(/[^A-Za-z0-9_.:+/@=-]+/g, " ")
      .replace(/\s+/g, " ")
      .trim()
      .toLowerCase()
  }

  function pipOperation(code: string): "install" | "remove" | undefined {
    const tokens = code.split(" ")
    const entrypoints = new Set(["pip", "pip._internal", "pip._internal.main"])
    for (let index = 0; index < tokens.length; index++) {
      if (!entrypoints.has(tokens[index] ?? "")) continue
      let cursor = index + 1
      while (cursor < tokens.length && /^--?[a-z0-9_.=+-]+$/.test(tokens[cursor] ?? "")) cursor++
      const operation = tokens[cursor]
      if (operation === "install" || operation === "download") return "install"
      if (operation === "uninstall") return "remove"
      index = cursor - 1
    }
  }

  /**
   * Conservatively recognize package and environment mutation submitted to a
   * plain interpreter. The normalized form also catches safe argv-based calls
   * such as `[sys.executable, "-m", "pip", "install", ...]` without asking
   * the model to use shell syntax or a notebook magic.
   */
  export function detect(input: { language: Language; environment: string; code: string }): Plan | undefined {
    const code = normalized(input.code)
    let operation: Plan["operation"] | undefined
    let manager: string | undefined

    if (input.language === "python") {
      const pip = pipOperation(code)
      if (pip === "install") {
        operation = "package_install"
        manager = "pip"
      } else if (pip === "remove") {
        operation = "package_remove"
        manager = "pip"
      } else if (/\b(?:uv\s+pip|conda|mamba)\s+(?:install|add)\b|\bpoetry\s+add\b/.test(code)) {
        operation = "package_install"
        manager = code.includes("uv pip") ? "uv" : code.includes("poetry") ? "poetry" : "conda"
      } else if (
        /\b(?:uv\s+pip|conda|mamba)\s+(?:uninstall|remove|update)\b|\bpoetry\s+(?:remove|update)\b/.test(code)
      ) {
        operation = "environment_update"
        manager = code.includes("uv pip") ? "uv" : code.includes("poetry") ? "poetry" : "conda"
      } else if (
        /\b(?:python(?:\d+(?:\.\d+)?)?|sys\.executable)\s+-m\s+(?:venv|virtualenv)\b|\bvenv\.envbuilder\b/.test(code)
      ) {
        operation = "environment_update"
        manager = "venv"
      }
    } else {
      if (/\binstall\.packages\b|\bbiocmanager::install\b|\bpak::pkg_install\b|\brenv::install\b/.test(code)) {
        operation = "package_install"
        manager = code.includes("renv::")
          ? "renv"
          : code.includes("pak::")
            ? "pak"
            : code.includes("biocmanager::")
              ? "BiocManager"
              : "install.packages"
      } else if (/\bremove\.packages\b|\bpak::pkg_remove\b|\brenv::remove\b/.test(code)) {
        operation = "package_remove"
        manager = code.includes("renv::") ? "renv" : code.includes("pak::") ? "pak" : "remove.packages"
      } else if (/\bupdate\.packages\b|\brenv::(?:update|restore|init|snapshot)\b/.test(code)) {
        operation = "environment_update"
        manager = code.includes("renv::") ? "renv" : "update.packages"
      }
    }

    if (!operation || !manager) return
    const digest = createHash("sha256")
      .update(JSON.stringify({ language: input.language, environment: input.environment, operation, code: input.code }))
      .digest("hex")
    const exact = operation === "package_install" ? command(input.language, input.code) : undefined
    const packages = exact?.length ? exact.slice(0, 8) : named(code, operation)
    return {
      language: input.language,
      environment: input.environment,
      operation,
      manager,
      digest,
      restart: true,
      ...(packages.length ? { packages } : {}),
      network: exact !== undefined,
    }
  }

  /** The package names after the install/remove verb, up to eight, without
   * flags, paths or R quoting: enough to say "Install pymupdf, pdfplumber". */
  function named(code: string, operation: Plan["operation"]) {
    if (operation === "environment_update") return []
    const tokens = code.split(" ")
    const verbs = new Set([
      "install",
      "download",
      "uninstall",
      "add",
      "remove",
      "install.packages",
      "pkg_install",
      "pkg_remove",
      "remove.packages",
    ])
    const at = tokens.findIndex((token) => verbs.has(token.replace(/^.*::/, "")))
    if (at < 0) return []
    const names: string[] = []
    for (const token of tokens.slice(at + 1)) {
      if (token.startsWith("-") || /^[a-z]+:\/\//.test(token)) continue
      const name = token.replace(/^c\(|\)$/g, "").replace(/^["']|["']$/g, "")
      if (!/^[a-z0-9][a-z0-9_.+-]*(?:\[[a-z0-9_,-]+\])?(?:[=<>!~]=?[a-z0-9_.*-]+)?$/i.test(name)) continue
      if (["python", "python3", "sys.executable", "-m", "pip", "uv", "conda", "mamba", "poetry"].includes(name))
        continue
      names.push(name)
      if (names.length === 8) break
    }
    return names
  }

  /** Stable, app-managed package root used when no project interpreter owns a
   * package directory. It is project + language + environment scoped, so
   * child sessions share installed packages but never interpreter state. */
  export function managedRoot(language: Language, environment: string) {
    return path.join(Global.Path.data, "kernel-environments", Instance.project.id, language, environment)
  }

  /** Resolve the complete Python start contract for both the canonical tool
   * and HTTP runtime surface. The starter is read-only with a project package
   * overlay; approved named task environments own their packages directly and
   * are reusable across projects on this machine. A mutation reaches the
   * network only when `network` is set, which detect() grants a bare command. */
  export async function pythonRuntime(
    environment: string,
    allowMutation = false,
    network = allowMutation,
  ): Promise<KernelStartOptions> {
    if (environment !== "python" && allowMutation) await ManagedEnvironments.ensureTask(environment)
    // Preserve the project's conventional .venv as the explicit local
    // runtime. The app-managed starter is the clean-install fallback, not an
    // override for a project that already owns its Python environment.
    let project: KernelStartOptions | undefined
    let projectError: unknown
    try {
      project = await pythonEnvironment(Instance.directory, environment)
    } catch (error) {
      projectError = error
    }
    const managed =
      environment === "python" && project?.binary
        ? project
        : await ManagedEnvironments.runtime("python", environment).catch(async (error) => {
            // Existing project-scoped named environments remain readable for
            // backward compatibility. New approved environments are created
            // in the shared app-owned store above.
            if (project) return project
            if (projectError) throw projectError
            throw error
          })
    const binary = managed.binary ?? (await hostPython())

    if (environment !== "python") {
      const prefix = managed.env?.CONDA_PREFIX
      return {
        ...managed,
        binary,
        ...(allowMutation && prefix
          ? {
              extraWritable: [prefix],
              captureProcessOutput: true,
              ...(network ? { sandboxNetwork: "allow" as const } : {}),
            }
          : {}),
      }
    }

    const packages = path.join(managedRoot("python", environment), "site-packages")
    // The default managed environment is a runtime primitive, not an
    // installation side effect. Recreate its empty package root on demand so
    // a cleaned data directory cannot make an otherwise valid host Python
    // fail to start. Package writes remain gated separately below.
    mkdirSync(packages, { recursive: true })
    return {
      ...managed,
      binary,
      extraReadable: [...(managed.extraReadable ?? []), packages],
      env: {
        ...(managed.env ?? {}),
        PIP_TARGET: packages,
        PYTHONPATH: [packages, managed.env?.PYTHONPATH, process.env.PYTHONPATH].filter(Boolean).join(path.delimiter),
      },
      ...(allowMutation
        ? {
            extraWritable: [packages],
            captureProcessOutput: true,
            ...(network ? { sandboxNetwork: "allow" as const } : {}),
          }
        : {}),
    }
  }

  /** Shells share the selected interpreter and project package directory, but
   * must not inherit arbitrary Python import paths from the server environment.
   * Keep this separate from kernel compatibility overlays: callers restore only
   * this derived path after their ordinary credential/environment filtering. */
  export async function pythonSubprocessRuntime(): Promise<
    KernelStartOptions & { env: Record<string, string> & { PYTHONPATH: string } }
  > {
    const runtime = await pythonRuntime("python")
    const packages = path.join(managedRoot("python", "python"), "site-packages")
    return {
      ...runtime,
      env: { ...(runtime.env ?? {}), PYTHONPATH: packages },
    }
  }

  /** Complete R start contract shared by all canonical entry points. */
  export async function rRuntime(allowMutation = false, network = allowMutation): Promise<KernelStartOptions> {
    const managed = await ManagedEnvironments.runtime("r")
    const packages = path.join(managedRoot("r", "r"), "library")
    // R warns or fails when R_LIBS_USER names a missing directory. Provision
    // the empty app-managed library for every default start and recover it
    // after cache cleanup; mutation authority is still required to write it.
    mkdirSync(packages, { recursive: true })
    return {
      ...managed,
      environmentName: "r",
      env: { ...(managed.env ?? {}), R_LIBS_USER: packages },
      ...(allowMutation ? { extraWritable: [packages], ...(network ? { sandboxNetwork: "allow" as const } : {}) } : {}),
    }
  }

  export function permission(plan: Plan) {
    return {
      permission: "environment_mutation",
      patterns: [plan.digest],
      always: [plan.digest],
      metadata: {
        environment_mutation: {
          language: plan.language,
          environment: plan.environment,
          operation: plan.operation,
          manager: plan.manager,
          plan_digest: plan.digest,
          restart: plan.restart,
          ...(plan.packages?.length ? { packages: plan.packages } : {}),
          warning: plan.network
            ? "This may contact package repositories and changes packages in the selected environment. The affected runtime restarts after a successful change, so in-memory variables are cleared."
            : "This changes packages in the selected environment without network access: package repositories are reachable only from an execution that is nothing but the install command. The affected runtime restarts after a successful change, so in-memory variables are cleared.",
        },
      },
    }
  }
}
