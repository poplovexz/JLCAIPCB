# Schematic Generation Stage

Use this reference after Spec Freeze and before any PCB, DSN, routing, DRC, fabrication, or JLCPCB output is generated.

## Contents

- Stage boundary and required sequence
- Pin, net-role, power-source, and path contracts
- Generated KiCad net and strict ERC gates
- Evidence, invalidation, and supported capability boundary

## Stage Boundary

The schematic is an independently gated artifact. A passing Spec is not permission to generate a PCB. The PCB stage unlocks only after the generated KiCad schematic, exported KiCad netlist, exact named net graph, and strict ERC report are SHA-bound in a current schematic-stage manifest.

Run in this order:

```bash
python3 <skill>/scripts/schematic_stage_gate.py --before-generation specs/<project>.yaml
python3 scripts/generate_project.py --schematic-only specs/<project>.yaml
python3 <skill>/scripts/schematic_stage_gate.py --after-generation --generator scripts/generate_project.py specs/<project>.yaml
python3 <skill>/scripts/schematic_stage_gate.py --check-evidence specs/<project>.yaml
python3 scripts/generate_project.py --board-only specs/<project>.yaml
```

The migrated `run_flow.py` performs this sequence automatically. Generated connectivity-batch candidates also use `--schematic-only`; temporary batch validation must not create PCB routing inputs.

## Pin Contract

Every symbol pin must have exactly one disposition before a required-stage schematic is generated:

- a key in `components[].pads`, meaning the pin is connected to the declared Spec net; or
- an entry in `schematic.no_connects`, meaning the generated KiCad schematic receives a real no-connect marker.

Connected and no-connect sets must not overlap. Pins absent from the bound symbol are errors. Pins omitted from both sets are errors. The gate reads the actual configured KiCad symbol library, including inherited symbols, instead of trusting a manually stated pin count.

The current generator deliberately rejects symbols with more than one functional unit. This is a fail-fast capability boundary, not permission to flatten or omit units. Add verified multi-unit generation before using such a symbol.

## Named Net Contract

Required-stage specs declare:

```yaml
schematic:
  connectivity:
    mode: net_labels
    label_scope: global
    default_label_shape: passive
```

`wires` remains valid for short readable local connections, but the generator must still place one global net label on every declared net. `global` scope is required because top-level KiCad local labels export as path-qualified names such as `/NET_NAME`. The default global-label shape is `passive`, so labels name nets without inventing driver direction; any per-net shape override is explicit Spec data.

The post-generation comparison requires both:

```text
Spec net name == generated KiCad net name
Spec exact component pins == generated KiCad component pins
```

Virtual source markers such as `PWR_FLAG` may add non-component pins to a declared net, but they cannot rename, split, or merge Spec component connectivity.

## Electrical Roles

Every power-class net in a required-stage exact graph declares its role. A supply identifies physical source and sink pins; a return identifies return pins.

```yaml
expected_net_graph:
  nets:
    SUPPLY_NET:
      exact: true
      role: supply
      required_pins: [SOURCE_REF.1, LOAD_REF.1]
      source_pins: [SOURCE_REF.1]
      sink_pins: [LOAD_REF.1]
    RETURN_NET:
      exact: true
      role: return
      required_pins: [SOURCE_REF.2, LOAD_REF.2]
      return_pins: [SOURCE_REF.2, LOAD_REF.2]
```

A `PWR_FLAG` is an ERC assertion, not a physical source. Each required-stage flag binds to source pins on the same net and records a rationale. Production pin-type overrides require source evidence, SHA256, and rationale; they must not be used merely to silence ERC.

## Functional Paths

Use `schematic.path_assertions` for topology that must not be bypassed:

- `series` proves an ordered source-to-sink path crosses one or more declared components and that each component creates a net boundary.
- `shunt` proves a protection component connects the declared line net to a distinct return net.
- `covers` binds assertions to architecture protection-intent IDs, while `boundaries` must match each intent's architecture boundary.

Production requires every architecture protection intent with `disposition: required` to be covered. Part ratings remain sourcing/requirements responsibilities; the schematic assertion proves only the required electrical topology.

## Strict ERC And Evidence

The after-generation gate exports the KiCad XML netlist, writes the parsed named graph, runs `kicad-cli sch erc --severity-all --exit-code-violations`, exports a non-empty schematic review PDF, and writes `schematic-stage-manifest.yaml`. The manifest binds:

- canonical Spec and current Spec Freeze;
- schematic-stage policy and executor;
- repository generator SHA256;
- KiCad version;
- generated schematic, netlist, named graph, ERC report, and review PDF hashes.

Any changed Spec, freeze, policy, executor, generator, schematic, netlist, graph, or ERC report relocks the PCB stage. `final_gate.py`, output binding, pre-final hooks, and production JLCPCB gates must check this evidence.

## Meaning Of PASS

PASS proves that the supported symbol set is fully dispositioned, the generated KiCad connectivity has exact Spec names and component pins, declared power/source/return and protection-path contracts are coherent, and strict ERC has zero violations. It does not prove component values, analog stability, firmware behavior, SI/PI/EMC/thermal performance, or laboratory function; those remain separate gates.
