"""Load and validate config.yaml. All thresholds and tag mappings come from here."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_TAXONOMY = "us-gaap"


class ConfigError(ValueError):
    pass


def qualify(tag: str) -> str:
    """'Revenues' -> 'us-gaap:Revenues'; 'dei:Foo' stays as is."""
    return tag if ":" in tag else f"{DEFAULT_TAXONOMY}:{tag}"


@dataclass(frozen=True)
class Strategy:
    """One way to obtain a concept: a single tag, or a sum of tags from one filing."""

    rank: int  # 1-based position in the concept's strategy list
    required: tuple[str, ...]
    optional: tuple[str, ...] = ()
    # (component, container): skip `component` when `container` is also reported,
    # because the container already includes it (e.g. CommercialPaper in ShortTermBorrowings).
    contained_in: tuple[tuple[str, str], ...] = ()

    @property
    def is_single(self) -> bool:
        return len(self.required) == 1 and not self.optional

    @property
    def tags(self) -> tuple[str, ...]:
        return self.required + self.optional

    @property
    def label(self) -> str:
        if self.is_single:
            return _short(self.required[0])
        parts = [_short(t) for t in self.required] + [f"{_short(t)}?" for t in self.optional]
        return "sum(" + " + ".join(parts) + ")"


def _short(tag: str) -> str:
    return tag.split(":", 1)[1] if tag.startswith(f"{DEFAULT_TAXONOMY}:") else tag


@dataclass(frozen=True)
class ConceptSpec:
    name: str
    period: str  # "duration" | "instant"
    unit: str
    strategies: tuple[Strategy, ...]
    not_applicable_before: dt.date | None = None
    na_reason: str | None = None
    absent_note: str | None = None

    @property
    def all_tags(self) -> tuple[str, ...]:
        seen: dict[str, None] = {}
        for s in self.strategies:
            for t in s.tags:
                seen[t] = None
        return tuple(seen)


@dataclass(frozen=True)
class Config:
    raw: dict[str, Any]
    root: Path
    concepts: dict[str, ConceptSpec] = field(default_factory=dict)

    def path(self, key: str) -> Path:
        p = Path(self.raw["paths"][key])
        return p if p.is_absolute() else self.root / p

    @property
    def sec(self) -> dict[str, Any]:
        return self.raw["sec"]

    @property
    def ingest(self) -> dict[str, Any]:
        return self.raw["ingest"]


def _parse_strategy(rank: int, item: Any, concept: str) -> Strategy:
    if isinstance(item, str):
        return Strategy(rank=rank, required=(qualify(item),))
    if isinstance(item, dict) and set(item) == {"sum"}:
        body = item["sum"] or {}
        req = tuple(qualify(t) for t in body.get("required") or [])
        opt = tuple(qualify(t) for t in body.get("optional") or [])
        if not req and not opt:
            raise ConfigError(f"{concept}: empty sum strategy #{rank}")
        contained = tuple((qualify(k), qualify(v)) for k, v in (body.get("contained_in") or {}).items())
        for k, v in contained:
            if k not in req + opt or v not in req + opt:
                raise ConfigError(f"{concept}: contained_in {k} -> {v} must name tags in the same sum")
            if k in req:
                raise ConfigError(f"{concept}: contained_in component {k} cannot be required")
        return Strategy(rank=rank, required=req, optional=opt, contained_in=contained)
    raise ConfigError(f"{concept}: strategy #{rank} must be a tag string or {{sum: ...}}, got {item!r}")


def parse_concepts(raw: dict[str, Any]) -> dict[str, ConceptSpec]:
    out: dict[str, ConceptSpec] = {}
    for name, body in raw.items():
        period = body.get("period")
        if period not in ("duration", "instant"):
            raise ConfigError(f"{name}: period must be 'duration' or 'instant'")
        strategies = tuple(_parse_strategy(i + 1, s, name) for i, s in enumerate(body.get("strategies") or []))
        if not strategies:
            raise ConfigError(f"{name}: no strategies")
        nab = body.get("not_applicable_before")
        if nab is not None and not isinstance(nab, dt.date):
            nab = dt.date.fromisoformat(str(nab))
        out[name] = ConceptSpec(
            name=name,
            period=period,
            unit=body.get("unit", "USD"),
            strategies=strategies,
            not_applicable_before=nab,
            na_reason=body.get("na_reason"),
            absent_note=body.get("absent_note"),
        )
    return out


def load_config(path: str | Path = "config.yaml") -> Config:
    path = Path(path).resolve()
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")
    raw = yaml.safe_load(path.read_text())
    for section in ("sec", "paths", "ingest", "concepts"):
        if section not in raw:
            raise ConfigError(f"config missing section '{section}'")
    ua = raw["sec"].get("user_agent", "")
    if "@" not in ua or "MY_EMAIL" in ua:
        raise ConfigError("sec.user_agent must contain a real contact email (SEC requirement)")
    rps = raw["sec"].get("max_requests_per_second", 0)
    if not 0 < rps < 10:
        raise ConfigError("sec.max_requests_per_second must be > 0 and < 10")
    return Config(raw=raw, root=path.parent, concepts=parse_concepts(raw["concepts"]))
