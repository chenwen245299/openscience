import { Show, createEffect, createSignal, onCleanup } from "solid-js"
import type { JSX } from "solid-js"
import { renderMolecule, responsive } from "../science/renderers/molecular/depict"

/**
 * A small structure thumbnail for the design-run pane.
 *
 * The pane used to identify a molecule by its generated id — `DES-NFOXDIN…` —
 * with the SMILES only in a hover title. For a workflow whose whole output is
 * molecules, that is the one thing a chemist cannot read past, so every row
 * that carries a SMILES draws it here.
 *
 * `Chem2D` already renders structures, but as a full artifact view: fixed
 * white canvas, 320px floor, its own status copy. This is the row-sized
 * version — it shares the same RDKit worker through `depict`, stays quiet
 * while loading, and renders nothing at all if RDKit cannot parse the string,
 * because a broken depiction must not cost the reader the text beneath it.
 */
export function MolStructure(props: { smiles?: string; height?: number; alt?: string }): JSX.Element {
  const [svg, setSvg] = createSignal("")
  let token = 0
  let disposed = false

  const height = () => props.height ?? 92

  createEffect(() => {
    const smiles = props.smiles?.trim()
    const mine = ++token
    if (!smiles) {
      setSvg("")
      return
    }
    renderMolecule({ input: smiles, width: 260, height: height() * 2 })
      .then((out) => {
        if (mine === token && !disposed) setSvg(responsive(out))
      })
      .catch(() => {
        if (mine === token && !disposed) setSvg("")
      })
  })

  onCleanup(() => {
    disposed = true
    token++
  })

  return (
    <Show when={svg()}>
      <div
        class="molpipe__structure"
        role="img"
        aria-label={props.alt ?? `Structure of ${props.smiles ?? "molecule"}`}
        style={{ height: `${height()}px` }}
        innerHTML={svg()}
      />
    </Show>
  )
}
