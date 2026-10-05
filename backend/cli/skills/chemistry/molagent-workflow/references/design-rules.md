# Design rules for theranostic luminogens

The rules the scripts encode, with their sources and their limits. Read this
before trusting a score.

## 1. The three channels

Absorption puts the molecule in S1. It leaves by one of three routes, and
`Phi_F + Phi_ISC + Phi_IC` is approximately 1:

| Route | Function | Structural levers |
|---|---|---|
| Radiative decay | fluorescence imaging | rigidity, planarity, fused rings, AIE on packing, shielding units, J-aggregation |
| ISC to T1 | PDT (ROS), phosphorescence | heavy atoms, small Delta E_ST, thiocarbonyl, twisted D-A (SOCT-ISC), radical-enhanced ISC |
| Internal conversion | PTT, photoacoustic | rotors, flexibility, TICT, strong push-pull, narrow gap |

Because they compete, **a multimodal agent is a compromise and a single-channel
maximum is the wrong target**. Reported one-for-all agents usually win by
controlling the *state*: rotors restricted on aggregation give emission,
rotors free in solution give heat.

Source: Feng, Zhang and Ding, *Chem. Soc. Rev.* 49, 8179–8234 (2020),
"Design of superior phototheranostic agents guided by Jablonski diagrams".

## 2. Reaching the red

Narrowing the gap is done by lengthening conjugation and by strengthening the
donor–acceptor pair. Windows: NIR-I 700–900 nm, NIR-II 1000–1700 nm.

The acceptor does most of the work at the deep end. Benzothiadiazole reaches
the visible to NIR-I; benzobisthiadiazole and thiadiazoloquinoxaline are what
take a D-A-D dye into NIR-II. `photophysics.spectral_band` weights acceptor
strength heavily for exactly this reason — at equal substitution a BBTD dye
must outrank a BTD one, and an earlier version of the proxy failed that test.

**The energy gap law is the cost.** Nonradiative decay rises roughly
exponentially as the gap narrows, so NIR-II emitters are dim. Counter-measures:
rigidification, deuteration, J-aggregation (exciton delocalisation suppresses
the reorganisation energy), and shielding units that keep water and neighbours
off the backbone.

**Brightness is `epsilon x Phi`, not `Phi`.** A dye with Phi = 0.62 and
epsilon = 6,000 M^-1 cm^-1 is five times dimmer than a standard with the same
Phi and epsilon > 30,000. `photophysics` has no epsilon proxy; take oscillator
strength from TD-DFT.

Sources: shielding-unit engineering, *Front. Chem.* 9, 739802 (2021);
energy-gap-law design principles for SWIR chromophores (ChemRxiv);
multi-dimensional modulation of NIR-II nanoaggregates, *Natl. Sci. Rev.* (2025).

## 3. Cyanines are a separate case

A cyanine's colour sits on its polymethine bridge, not on a donor–acceptor
pair: each added vinylene red-shifts by roughly 100 nm, which is how Cy3, Cy5,
Cy7 and ICG walk from visible to NIR-I. A fragment vocabulary built for
push-pull dyes scores them as plain hydrocarbons, so `photophysics.polymethine`
counts the bridge directly. They remain photochemically fragile — ICG's
photostability is the usual benchmark to beat.

## 4. Type I versus Type II

| | Type II | Type I |
|---|---|---|
| Mechanism | energy transfer to O2 | electron transfer |
| Product | singlet oxygen | superoxide, hydroxyl radical |
| Oxygen dependence | high | low |
| Hypoxic tumour | poor | good |
| T1 requirement | above ~0.98 eV | below ~0.98 eV blocks the Type II channel |

Two design routes, from Ma et al., *Adv. Sci.* (2024):

- **Indirect** — push T1 below the singlet-oxygen threshold (~0.98 eV) while
  keeping an S1–Tn gap under about 0.4 eV so ISC stays efficient. Achieved by
  maximising HOMO–LUMO spatial overlap, extending conjugation, chalcogen
  substitution.
- **Direct** — strengthen electron transfer: an electron-rich microenvironment
  (protein, supramolecular host, heteroatoms); an electron-deficient
  intermediate (pyridinium cationisation shrinks Delta E_ST from about 0.8 eV
  to about 0.2 eV; perylenediimide acceptors); or a quinone mediator whose
  reduction potential sits well below the −0.33 V of O2/O2•−.

**The trade-off**: a stronger donor builds the electron-rich environment but
also lowers the photosensitizer's oxidation potential, weakening the
photo-oxidation of biomolecules. More donor is not simply better.

`photophysics.type_i_propensity` reports structural signatures only. The two
criteria that settle it — T1 energy and reduction potential — need TD-DFT and
cyclic voltammetry, and the function says so rather than estimating them.

## 5. ISC without heavy atoms

Iodine and selenium buy ISC at the cost of dark toxicity and short triplet
lifetimes. The heavy-atom-free routes, from Xiao et al., *Molecules* (2023):

| Route | Mechanism | Typical performance |
|---|---|---|
| Thionation (C=O to C=S) | El-Sayed-allowed n-pi*/pi-pi* channel | works under 1% O2 |
| SOCT-ISC | orthogonal donor–acceptor dyad | — |
| Twisted pi-conjugation | twist raises the SOC matrix element | Phi_Delta 36%, triplet 492 us |
| Small S1/Tn gap | upper triplets matched to S1 | Phi_Delta 50–80%, triplet up to 505 us |
| Radical-enhanced ISC | TEMPO three-spin system | Phi_Delta 14–56% |
| Fullerene C60 | intrinsic spin converter via FRET | Phi_Delta up to 88.5% |

El-Sayed's rule is the common thread: ISC is fast when the two states differ
in orbital character. That is what thionation buys.

## 6. Aggregate state is part of the design

Traditional dyes quench on aggregation (ACQ); AIE luminogens brighten. The
agent works as a nanoparticle, so **dilute-solution numbers do not carry
over**. The same rotor gives heat in solution and emission in the solid.

Packing mode matters on its own: J-aggregation narrows the S–T gap and favours
delayed fluorescence; H-aggregation accelerates ISC and suppresses radiative
decay. Single-crystal packing is often what explains a structure–property
result that descriptors cannot.

## 7. Delivery and safety constraints

- ROS has a very short lifetime and radius, so **localisation sets efficacy**.
  Lipophilic cations (pyridinium, triphenylphosphonium) target mitochondria;
  morpholine targets lysosomes.
- Renal clearance needs a hydrodynamic diameter under about 5.5 nm.
- Clinical photosensitizers are permanently photoactive, which causes skin
  phototoxicity; activatable designs (pH, GSH, H2O2, enzyme, hypoxia,
  viscosity) switch on at the lesion and cut imaging background too.
- Laser power must stay inside the skin exposure limit for the wavelength.
  Figures near 0.33 W/cm2 at 808 nm and 1.0 W/cm2 at 1064 nm circulate widely;
  check ANSI Z136.1 directly before designing an experiment rather than citing
  a secondary source.

## 8. Measurement pitfalls

| Quantity | Method | The trap |
|---|---|---|
| NIR-II quantum yield | integrating sphere, or relative to IR-26 | published IR-26 values span 0.05% to 0.5%; recent redeterminations land near 0.03%. State which you used |
| ROS identity | DHR123, HPF (Type I); SOSG, ABDA (Type II); ESR with DMPO/BMPO | fluorescent probes cross-react — confirm with ESR |
| Singlet oxygen yield | ABDA/DPBF relative, or direct 1270 nm phosphorescence | match absorbance and excitation between sample and reference |
| Photothermal efficiency | Roper heat balance | concentration, irradiation time and which cooling segment is fitted all move the answer |
| Phototoxicity | dark versus irradiated controls | keep the power density inside the skin limit |

## 9. Clinical reference points

Approved imaging agents: ICG, fluorescein, 5-ALA, hexaminolevulinate,
pafolacianine (Cytalux), pegulicianine (Lumisight, FDA April 2024).
Approved photosensitizers: Photofrin, verteporfin, temoporfin (Foscan),
talaporfin (Laserphyrin), padeliporfin (Tookad). Photoimmunotherapy:
cetuximab sarotalocan (Akalux, Japan 2020), an EGFR antibody conjugated to
IR700.

Almost all of these are **single-function**. Multimodal AIE and NIR-II agents
remain preclinical, held back by penetration depth, long-term safety,
degradability and scale-up. A design that is interesting in a paper is a long
way from one that is usable.

## 10. What AI can and cannot do here yet

Generative workflows for fluorophores exist and are starting to be validated
experimentally — SyntheFluor-RL (arXiv:2601.07145) generates synthesis-aware
scaffolds by assembling catalogue building blocks through known reactions, and
AAPSI closes a loop on singlet-oxygen yield. Both optimise a narrow property
set, and the public datasets behind them (ChemFluor, Deep4Chem) are
**dilute-solution, visible to NIR-I** measurements.

That is the binding constraint for theranostics: there is no standardised
dataset covering NIR-II emission, ROS yields, photothermal efficiency and
aggregate-state behaviour together. Until there is, a model can rank
candidates but cannot predict them, which is why this skill ships calibrated
structural proxies and labels them as such rather than a trained predictor
presented as one.
