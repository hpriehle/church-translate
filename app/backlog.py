"""Keeps the French audio from drifting behind the speaker.

backlog = seconds of French audio waiting to be heard (+ an estimate for chunks still being
translated/synthesised). Three levers, gentlest first:
  1. speak faster (TTS rate ramps from 1.0× to max_speed),
  2. ask the translator to condense (with hysteresis),
  3. hard cap: the mixer drops the oldest waiting clips (captions are still shown).
"""
from __future__ import annotations

from .config import BacklogCfg


class BacklogPolicy:
    def __init__(self, cfg: BacklogCfg):
        self.cfg = cfg
        self.condense = False
        self.condense_count = 0
        self.max_seen = 0.0

    def speed_for(self, backlog_s: float) -> float:
        c = self.cfg
        if backlog_s <= c.speedup_start_s:
            return 1.0
        if backlog_s >= c.speedup_full_s:
            return c.max_speed
        frac = (backlog_s - c.speedup_start_s) / max(0.1, c.speedup_full_s - c.speedup_start_s)
        return round(1.0 + frac * (c.max_speed - 1.0), 3)

    def update_condense(self, backlog_s: float) -> bool:
        self.max_seen = max(self.max_seen, backlog_s)
        if not self.condense and backlog_s >= self.cfg.condense_on_s:
            self.condense = True
        elif self.condense and backlog_s <= self.cfg.condense_off_s:
            self.condense = False
        if self.condense:
            self.condense_count += 1
        return self.condense

    @property
    def drop_above_s(self) -> float:
        return self.cfg.drop_above_s

    @property
    def drop_to_s(self) -> float:
        return self.cfg.speedup_full_s
