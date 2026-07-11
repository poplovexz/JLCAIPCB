#!/usr/bin/env python3
"""Minimal structured S-expression reader for KiCad symbol pins and footprint pads."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    index = 0
    while index < len(text):
        character = text[index]
        if character.isspace():
            index += 1
            continue
        if character == ";":
            newline = text.find("\n", index)
            index = len(text) if newline < 0 else newline + 1
            continue
        if character in "()":
            tokens.append(character)
            index += 1
            continue
        if character == '"':
            index += 1
            value: list[str] = []
            while index < len(text):
                character = text[index]
                if character == "\\" and index + 1 < len(text):
                    value.append(text[index + 1])
                    index += 2
                    continue
                if character == '"':
                    index += 1
                    break
                value.append(character)
                index += 1
            else:
                raise ValueError("unterminated quoted string in KiCad S-expression")
            tokens.append("".join(value))
            continue
        end = index
        while end < len(text) and not text[end].isspace() and text[end] not in "();":
            end += 1
        if end == index:
            raise ValueError(f"unexpected character in KiCad S-expression at offset {index}")
        tokens.append(text[index:end])
        index = end
    return tokens


def parse(text: str) -> list[Any]:
    root: list[Any] = []
    stack: list[list[Any]] = [root]
    for token in tokenize(text):
        if token == "(":
            child: list[Any] = []
            stack[-1].append(child)
            stack.append(child)
        elif token == ")":
            if len(stack) == 1:
                raise ValueError("unexpected closing parenthesis in KiCad S-expression")
            stack.pop()
        else:
            stack[-1].append(token)
    if len(stack) != 1:
        raise ValueError("unclosed parenthesis in KiCad S-expression")
    if len(root) != 1 or not isinstance(root[0], list):
        raise ValueError("KiCad file must contain exactly one root S-expression")
    return root[0]


def node_head(node: Any) -> str:
    return str(node[0]) if isinstance(node, list) and node else ""


def direct_child(node: list[Any], head: str) -> list[Any] | None:
    return next(
        (item for item in node[1:] if isinstance(item, list) and node_head(item) == head),
        None,
    )


def walk(node: Any):
    if not isinstance(node, list):
        return
    yield node
    for item in node[1:]:
        if isinstance(item, list):
            yield from walk(item)


def symbol_pin_numbers(path: Path, symbol_id: str) -> list[str]:
    root = parse(path.read_text(encoding="utf-8"))
    if node_head(root) != "kicad_symbol_lib":
        raise ValueError(f"not a KiCad symbol library: {path}")
    symbols = {
        str(node[1]): node
        for node in root[1:]
        if isinstance(node, list) and node_head(node) == "symbol" and len(node) > 1
    }
    requested = symbol_id.split(":", 1)[-1]
    target_name = requested if requested in symbols else next(
        (name for name in symbols if name.split(":")[-1] == requested),
        "",
    )
    if not target_name:
        raise ValueError(f"symbol {symbol_id} is not present in {path}")

    visited: set[str] = set()
    numbers: list[str] = []

    def collect(name: str) -> None:
        if name in visited:
            return
        visited.add(name)
        symbol = symbols[name]
        extends = direct_child(symbol, "extends")
        if extends is not None and len(extends) > 1:
            base = str(extends[1])
            if base not in symbols:
                raise ValueError(f"symbol {name} extends missing base symbol {base}")
            collect(base)
        for node in walk(symbol):
            if node_head(node) != "pin":
                continue
            number = direct_child(node, "number")
            if number is not None and len(number) > 1 and str(number[1]):
                numbers.append(str(number[1]))

    collect(target_name)
    return numbers


def symbol_pin_records(path: Path, symbol_id: str) -> list[dict[str, str]]:
    """Return semantic pin records from a symbol, including inherited pins."""
    root = parse(path.read_text(encoding="utf-8"))
    if node_head(root) != "kicad_symbol_lib":
        raise ValueError(f"not a KiCad symbol library: {path}")
    symbols = {
        str(node[1]): node
        for node in root[1:]
        if isinstance(node, list) and node_head(node) == "symbol" and len(node) > 1
    }
    requested = symbol_id.split(":", 1)[-1]
    target_name = requested if requested in symbols else next(
        (name for name in symbols if name.split(":")[-1] == requested),
        "",
    )
    if not target_name:
        raise ValueError(f"symbol {symbol_id} is not present in {path}")

    visited: set[str] = set()
    records: list[dict[str, str]] = []

    def collect(name: str) -> None:
        if name in visited:
            return
        visited.add(name)
        symbol = symbols[name]
        extends = direct_child(symbol, "extends")
        if extends is not None and len(extends) > 1:
            base = str(extends[1])
            if base not in symbols:
                raise ValueError(f"symbol {name} extends missing base symbol {base}")
            collect(base)
        for node in walk(symbol):
            if node_head(node) != "pin" or len(node) < 3:
                continue
            number = direct_child(node, "number")
            pin_name = direct_child(node, "name")
            if number is None or len(number) < 2 or not str(number[1]):
                continue
            records.append(
                {
                    "number": str(number[1]),
                    "name": str(pin_name[1]) if pin_name is not None and len(pin_name) > 1 else "",
                    "electrical_type": str(node[1]),
                    "graphic_style": str(node[2]),
                }
            )

    collect(target_name)
    return records


def symbol_unit_numbers(path: Path, symbol_id: str) -> list[int]:
    root = parse(path.read_text(encoding="utf-8"))
    if node_head(root) != "kicad_symbol_lib":
        raise ValueError(f"not a KiCad symbol library: {path}")
    symbols = {
        str(node[1]): node
        for node in root[1:]
        if isinstance(node, list) and node_head(node) == "symbol" and len(node) > 1
    }
    requested = symbol_id.split(":", 1)[-1]
    target_name = requested if requested in symbols else next(
        (name for name in symbols if name.split(":")[-1] == requested),
        "",
    )
    if not target_name:
        raise ValueError(f"symbol {symbol_id} is not present in {path}")

    visited: set[str] = set()
    units: set[int] = set()

    def collect(name: str) -> None:
        if name in visited:
            return
        visited.add(name)
        symbol = symbols[name]
        extends = direct_child(symbol, "extends")
        if extends is not None and len(extends) > 1:
            base = str(extends[1])
            if base not in symbols:
                raise ValueError(f"symbol {name} extends missing base symbol {base}")
            collect(base)
        for child in symbol[1:]:
            if not isinstance(child, list) or node_head(child) != "symbol" or len(child) < 2:
                continue
            parts = str(child[1]).rsplit("_", 2)
            if len(parts) == 3 and parts[1].isdigit() and int(parts[1]) > 0:
                units.add(int(parts[1]))

    collect(target_name)
    return sorted(units or {1})


def footprint_pad_numbers(path: Path, footprint_id: str) -> list[str]:
    root = parse(path.read_text(encoding="utf-8"))
    if node_head(root) != "footprint":
        raise ValueError(f"not a KiCad footprint file: {path}")
    expected_name = footprint_id.split(":", 1)[-1]
    if len(root) < 2 or str(root[1]) != expected_name:
        raise ValueError(f"footprint {footprint_id} is not present in {path}")
    return [
        str(node[1])
        for node in walk(root)
        if node_head(node) == "pad" and len(node) > 1 and str(node[1])
    ]


def footprint_pad_records(path: Path, footprint_id: str) -> list[dict[str, Any]]:
    """Return normalized physical pad geometry without assuming a package family."""
    root = parse(path.read_text(encoding="utf-8"))
    if node_head(root) != "footprint":
        raise ValueError(f"not a KiCad footprint file: {path}")
    expected_name = footprint_id.split(":", 1)[-1]
    if len(root) < 2 or str(root[1]) != expected_name:
        raise ValueError(f"footprint {footprint_id} is not present in {path}")

    def numeric_values(node: list[Any] | None) -> list[float]:
        if node is None:
            return []
        values: list[float] = []
        for value in node[1:]:
            if isinstance(value, list):
                break
            try:
                values.append(float(value))
            except (TypeError, ValueError):
                break
        return values

    records: list[dict[str, Any]] = []
    for node in walk(root):
        if node_head(node) != "pad" or len(node) < 4 or not str(node[1]):
            continue
        at = numeric_values(direct_child(node, "at"))
        size = numeric_values(direct_child(node, "size"))
        layers = direct_child(node, "layers")
        records.append(
            {
                "number": str(node[1]),
                "type": str(node[2]),
                "shape": str(node[3]),
                "at_mm": at,
                "size_mm": size,
                "layers": [str(value) for value in layers[1:]] if layers is not None else [],
            }
        )
    return records


def footprint_feature_summary(path: Path, footprint_id: str) -> dict[str, Any]:
    """Summarize manufacturing layers, paste behavior, and 3D bindings."""
    root = parse(path.read_text(encoding="utf-8"))
    if node_head(root) != "footprint":
        raise ValueError(f"not a KiCad footprint file: {path}")
    expected_name = footprint_id.split(":", 1)[-1]
    if len(root) < 2 or str(root[1]) != expected_name:
        raise ValueError(f"footprint {footprint_id} is not present in {path}")

    graphics_by_layer: dict[str, int] = {}
    models: list[str] = []
    paste_pad_count = 0
    custom_pad_count = 0
    through_hole_pad_count = 0
    for node in root[1:]:
        if not isinstance(node, list):
            continue
        head = node_head(node)
        if head.startswith("fp_"):
            layer = direct_child(node, "layer")
            if layer is not None and len(layer) > 1:
                name = str(layer[1])
                graphics_by_layer[name] = graphics_by_layer.get(name, 0) + 1
        elif head == "model" and len(node) > 1 and str(node[1]):
            models.append(str(node[1]))
        elif head == "pad" and len(node) > 3:
            layers = direct_child(node, "layers")
            layer_names = {str(value) for value in layers[1:]} if layers is not None else set()
            if "F.Paste" in layer_names or "B.Paste" in layer_names or "*.Paste" in layer_names:
                paste_pad_count += 1
            if str(node[3]) == "custom":
                custom_pad_count += 1
            if str(node[2]) in {"thru_hole", "np_thru_hole"}:
                through_hole_pad_count += 1
    return {
        "graphics_by_layer": {key: graphics_by_layer[key] for key in sorted(graphics_by_layer)},
        "models": sorted(models),
        "paste_pad_count": paste_pad_count,
        "custom_pad_count": custom_pad_count,
        "through_hole_pad_count": through_hole_pad_count,
    }
