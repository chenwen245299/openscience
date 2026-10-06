import path from "node:path"
import { fileURLToPath } from "node:url"
import { Language, type Node as SyntaxNode } from "web-tree-sitter"
import { lazy } from "@synsci/util/lazy"
import { PermissionJudge } from "./judge"

function asset(value: string) {
  if (value.startsWith("file://")) return fileURLToPath(value)
  if (path.isAbsolute(value) || /^[a-z]:/i.test(value)) return value
  return fileURLToPath(new URL(value, import.meta.url))
}

export const shellParser = lazy(async () => {
  const { Parser } = await import("web-tree-sitter")
  const { default: tree } = await import("web-tree-sitter/tree-sitter.wasm" as string, { with: { type: "wasm" } })
  await Parser.init({ locateFile: () => asset(tree) })
  const { default: bash } = await import("tree-sitter-bash/tree-sitter-bash.wasm" as string, { with: { type: "wasm" } })
  const parser = new Parser()
  parser.setLanguage(await Language.load(asset(bash)))
  return parser
})

function literal(node: SyntaxNode): string | undefined {
  if (node.type === "command_name") return node.firstNamedChild ? literal(node.firstNamedChild) : undefined
  if (node.type === "raw_string") return node.text.slice(1, -1)
  if (node.type === "string" && node.namedChildren.every((child) => child?.type === "string_content"))
    return node.text.slice(1, -1).replace(/\\([\\"$`])/g, "$1")
  if (node.type === "word" && !/[~$*?{}\[\]]/.test(node.text)) return node.text.replace(/\\(.)/g, "$1")
  return undefined
}

/** Track shell control flow without executing it. A failed cd and a subshell
 * must not hide scripts in the original directory from the risk reviewer. */
export async function shellSources(root: SyntaxNode, cwd: string): Promise<string[]> {
  type Directory = string | undefined
  type Outcome = { success: Directory[]; failure: Directory[] }
  const files = new Set<string>()
  const union = (...groups: Directory[][]) => [...new Set(groups.flat())]
  const walk = async (node: SyntaxNode, directories: Directory[]): Promise<Outcome> => {
    if (node.type === "command") {
      const words = node.namedChildren.filter(
        (child): child is SyntaxNode =>
          !!child && ["command_name", "word", "string", "raw_string", "concatenation"].includes(child.type),
      )
      const command = words.map((word) => literal(word) ?? word.text.replace(/^["']|["']$/g, ""))
      if (command[0] === "cd") {
        const target = words.filter((word) => !word.text.startsWith("-")).at(1)
        const value = target ? literal(target) : undefined
        return {
          success: union(
            directories.map((directory) => (value && directory ? path.resolve(directory, value) : undefined)),
          ),
          failure: directories,
        }
      }
      for (const file of PermissionJudge.files([command])) {
        const argument = words[command.indexOf(file)]
        if (argument && literal(argument) === undefined) throw new Error("script path cannot be resolved")
        if (!path.isAbsolute(file) && directories.includes(undefined))
          throw new Error("script working directory cannot be resolved")
        const candidates = union(directories).map((directory) => path.resolve(directory ?? cwd, file))
        const existing = await Promise.all(
          candidates.map(async (candidate) => ((await Bun.file(candidate).exists()) ? candidate : undefined)),
        )
        for (const candidate of existing.some(Boolean) ? existing : candidates) if (candidate) files.add(candidate)
      }
      // Command substitutions run in their own shell environment.
      for (const child of node.descendantsOfType("command_substitution")) if (child) await walk(child, directories)
      return { success: directories, failure: directories }
    }
    if (node.type === "list") {
      const left = node.namedChildren[0]
      const right = node.namedChildren[1]
      if (!left || !right) return { success: directories, failure: directories }
      const first = await walk(left, directories)
      const operator = node.children.find((child) => child?.type === "&&" || child?.type === "||")?.type
      const second = await walk(
        right,
        operator === "&&" ? first.success : operator === "||" ? first.failure : union(first.success, first.failure),
      )
      return operator === "&&"
        ? { success: second.success, failure: union(first.failure, second.failure) }
        : operator === "||"
          ? { success: union(first.success, second.success), failure: second.failure }
          : second
    }
    if (["subshell", "command_substitution", "pipeline"].includes(node.type)) {
      if (node.type === "pipeline") {
        for (const child of node.namedChildren) if (child) await walk(child, directories)
      } else await sequence(node.namedChildren, directories)
      return { success: directories, failure: directories }
    }
    if (["program", "compound_statement", "redirected_statement"].includes(node.type))
      return sequence(node.namedChildren, directories)
    // Conditional/loop cwd changes cannot be inferred by textual ordering.
    const changed = node.descendantsOfType("command").some((child) => child?.childForFieldName("name")?.text === "cd")
    for (const child of node.namedChildren) if (child) await walk(child, changed ? [undefined] : directories)
    return { success: changed ? [undefined] : directories, failure: changed ? [undefined] : directories }
  }
  const sequence = async (nodes: (SyntaxNode | null)[], directories: Directory[]): Promise<Outcome> => {
    const outcome: Outcome = { success: directories, failure: directories }
    for (const node of nodes) {
      if (!node) continue
      const next = await walk(node, union(outcome.success, outcome.failure))
      outcome.success = next.success
      outcome.failure = next.failure
    }
    return outcome
  }
  await walk(root, [cwd])
  return [...files]
}
