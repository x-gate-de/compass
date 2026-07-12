# -----------------------------------------------------------------------------
# Skript: src/scoring.py
# Autor: Torben <github@x-gate.de>
# Version: 1.0.0
# Lizenz: AGPL-3.0-or-later — siehe LICENSE.
# Zweck:
# - Reine Rechenlogik fuer das Ranking: Prioritaets-Gewichte, Basisscore aus der
#   LLM-Dringlichkeit, deterministischer Zeit-Decay und Override.
# Ablauf:
# - base_score = urgency * weight(group_prio) * weight(feed_prio)
# - final_score = base_score * decay_factor(...) (+ override_floor bei gueltigem Override)
# Betriebs- und Wartungshinweise:
# - Keine Seiteneffekte, keine I/O -> isoliert testbar (siehe __main__-Selbsttest).
# - Parameter stammen aus config.yaml (Abschnitt scoring) und sind Startwerte.
# -----------------------------------------------------------------------------

import math
from dataclasses import dataclass


# Tunbare Parameter des Scorings; Defaults entsprechen SPEC.md.
@dataclass
class ScoringParams:
    override_threshold: int = 90       # ab dieser urgency darf Override greifen
    override_floor: float = 1000.0     # Mindest-final_score bei aktivem Override
    boost_max: float = 1.5             # max. Zusatzgewicht bei Termin-Naehe
    lead_window: float = 72 * 3600.0   # Vorlauf, ab dem der Termin-Boost anlaeuft (s)
    overdue_window: float = 24 * 3600.0  # Zeitfenster, in dem der Boost nach Faelligkeit abfaellt (s)
    half_life: float = 48 * 3600.0     # Halbwertszeit des Alters-Decays (s)
    decay_floor: float = 0.5           # untere Schranke des Alters-Decays


def clamp(value, low, high):
    return low if value < low else high if value > high else value


# Gewicht einer Prioritaetsstufe 1..5: weight(3) = 1.0 (neutral).
def weight(priority):
    return clamp(int(priority), 1, 5) / 3.0


# Basisscore vor Decay/Override.
def base_score(urgency, group_prio, feed_prio):
    u = clamp(float(urgency), 0.0, 100.0)
    return u * weight(group_prio) * weight(feed_prio)


# Deterministischer Zeitfaktor:
# - mit ts_due (Termin/Frist): Boost steigt bei Annaeherung, faellt nach Faelligkeit ab.
# - ohne ts_due (Mail/Chat/News): sanfter Alters-Decay bis decay_floor.
def decay_factor(now, ts_source, ts_due, params):
    if ts_due is not None:
        remaining = ts_due - now
        if remaining <= 0:
            overdue = -remaining
            return 1.0 + params.boost_max * clamp(1.0 - overdue / params.overdue_window, 0.0, 1.0)
        proximity = clamp(1.0 - remaining / params.lead_window, 0.0, 1.0)
        return 1.0 + params.boost_max * proximity
    if ts_source is None:
        return 1.0
    age = max(0.0, now - ts_source)
    return params.decay_floor + (1.0 - params.decay_floor) * math.pow(2.0, -age / params.half_life)


# Endgueltiger Sortier-Score. Override hebt echte Notfaelle ueber alle regulaeren
# Items (additiver Bonus), erhaelt aber die Reihenfolge untereinander -> kein Plateau,
# bei dem viele Eskalationen denselben Wert teilen.
def final_score(base, urgency, override, now, ts_source, ts_due, params):
    score = base * decay_factor(now, ts_source, ts_due, params)
    if override and urgency >= params.override_threshold:
        score += params.override_floor
    return score


# Bequemer Komplettpfad: aus urgency + Prioritaeten + Zeitbezug beide Scores berechnen.
def compute_scores(urgency, override, group_prio, feed_prio, now, ts_source, ts_due, params):
    base = base_score(urgency, group_prio, feed_prio)
    final = final_score(base, urgency, override, now, ts_source, ts_due, params)
    return base, final


if __name__ == "__main__":
    # Kleiner Selbsttest der Rechenlogik (ohne externe Abhaengigkeiten).
    p = ScoringParams()
    assert weight(3) == 1.0
    assert weight(1) < weight(5)
    assert abs(base_score(80, 3, 3) - 80.0) < 1e-9
    _, f_over = compute_scores(95, True, 1, 1, now=1000.0, ts_source=1000.0, ts_due=None, params=p)
    assert f_over >= p.override_floor
    _, f_low = compute_scores(85, True, 3, 3, now=1000.0, ts_source=1000.0, ts_due=None, params=p)
    assert f_low < p.override_floor
    _, f_o95 = compute_scores(95, True, 3, 3, now=1000.0, ts_source=1000.0, ts_due=None, params=p)
    _, f_o99 = compute_scores(99, True, 3, 3, now=1000.0, ts_source=1000.0, ts_due=None, params=p)
    assert f_o99 > f_o95 > p.override_floor
    _, f_due_now = compute_scores(50, False, 3, 3, now=1000.0, ts_source=0.0, ts_due=1000.0, params=p)
    _, f_due_far = compute_scores(50, False, 3, 3, now=1000.0, ts_source=0.0, ts_due=1000.0 + p.lead_window, params=p)
    assert f_due_now > f_due_far
    _, f_fresh = compute_scores(50, False, 3, 3, now=1000.0, ts_source=1000.0, ts_due=None, params=p)
    _, f_old = compute_scores(50, False, 3, 3, now=1000.0 + 10 * p.half_life, ts_source=1000.0, ts_due=None, params=p)
    assert f_old < f_fresh
    print("scoring-Selbsttest OK")
