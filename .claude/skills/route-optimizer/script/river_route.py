#!/usr/bin/env python3
"""River-routing geometry: split one trunk into two matched tracks.

Why this exists
---------------
`special-tracing-pattern.md` entry 1 buys a matched net pair equal R and
equal C by drawing both wires from one path: route a trunk, then offset it
to two parallel tracks. The appeal is that equality should hold BY
CONSTRUCTION rather than by measurement.

It does not, quite, and the exception is the whole reason this is a module
and not four lines inline. Offsetting a rectilinear trunk by +/- pitch/2
gives two tracks whose lengths differ by

    dL = 2 * pitch * T,   T = (left turns) - (right turns) along the trunk

so they are equal only when the trunk's turns balance (T = 0). A trunk with
one net corner -- an L around a macro, the commonest shape there is --
mismatches the pair by 2 * pitch, which is the same order as the mismatch
the pattern was invoked to remove. Asserting equality here would have
produced a pass that reports success while making the pair no better.

Reflection has no such defect: a mirror image is an isometry, so mirror mode
is exactly equal whatever the trunk does. Hence two modes, and a `plan()`
that says which one it used and what the residual is.

What this module is
-------------------
The geometry kernel only: paths in, paths out, plus the measurements the
acceptance gate needs. It knows nothing about GDS, layers, PDKs, obstacles
or nets -- callers hold those. Wiring it into `shorten_routes.py` (searching
the trunk on `ConnectGrid`, turning tracks into `Item`s, the DRC revert
loop) is NOT done here; see the .md.

Paths are lists of (x, y) in um, axis-aligned, in travel order.

    python river_route.py --self-test     # verify the laws above
    python river_route.py --demo          # worked example, both modes
"""
import argparse
import random

EPS = 1e-9


# --- measurements -----------------------------------------------------------
def length(path):
    return sum(abs(b[0] - a[0]) + abs(b[1] - a[1])
               for a, b in zip(path, path[1:]))


def _dirs(path):
    out = []
    for a, b in zip(path, path[1:]):
        dx, dy = b[0] - a[0], b[1] - a[1]
        if abs(dx) > EPS and abs(dy) > EPS:
            raise ValueError(f"not axis-aligned: {a} -> {b}")
        n = abs(dx) + abs(dy)
        if n <= EPS:
            raise ValueError(f"zero-length segment at {a}")
        out.append((dx / n, dy / n))
    return out


def corners(path):
    """Turns in the path -- what the acceptance gate compares."""
    d = _dirs(path)
    return sum(1 for u, v in zip(d, d[1:]) if abs(u[0] * v[0] + u[1] * v[1]) < 0.5)


def net_turning(path):
    """(left turns) - (right turns). The quantity that decides whether an
    offset pair comes out equal: dL = 2 * pitch * net_turning."""
    d = _dirs(path)
    return sum(round(u[0] * v[1] - u[1] * v[0]) for u, v in zip(d, d[1:]))


# --- the two constructions --------------------------------------------------
def offset(path, delta):
    """The trunk shifted `delta` to the left of travel (negative = right).

    Corners are taken at the intersections of the offset segment lines,
    which is what makes the result parallel to the trunk everywhere.
    """
    d = _dirs(path)
    left = [(-uy, ux) for ux, uy in d]
    q = [(path[0][0] + delta * left[0][0], path[0][1] + delta * left[0][1])]
    for i in range(len(d) - 1):
        horiz = abs(d[i][1]) < EPS
        y = (path[i][1] + delta * left[i][1]) if horiz else \
            (path[i + 1][1] + delta * left[i + 1][1])
        x = (path[i + 1][0] + delta * left[i + 1][0]) if horiz else \
            (path[i][0] + delta * left[i][0])
        q.append((x, y))
    q.append((path[-1][0] + delta * left[-1][0],
              path[-1][1] + delta * left[-1][1]))
    return q


def folded(trunk, track):
    """True if the offset turned a segment inside out.

    A trunk jog shorter than the offset distance makes the offset polyline
    double back on itself: the two tracks then cross, which between two nets
    is a short. Cheap to detect -- compare each offset segment's direction
    with the trunk segment it came from -- and not optional.
    """
    for (a, b), (c, e) in zip(zip(trunk, trunk[1:]), zip(track, track[1:])):
        for i in (0, 1):
            if (b[i] - a[i]) * (e[i] - c[i]) < -EPS:
                return True
    return False


def split(trunk, pitch):
    """Trunk -> (left track, right track), one net each, `pitch` apart."""
    a, b = offset(trunk, pitch / 2.0), offset(trunk, -pitch / 2.0)
    if folded(trunk, a) or folded(trunk, b):
        raise ValueError(f"trunk has a jog shorter than pitch {pitch}: the "
                         f"two tracks would cross")
    return a, b


def mirror(path, axis, vertical=True):
    """The path reflected about x = axis (or y = axis). An isometry: same
    length, same corner count, whatever the path does."""
    return [((2 * axis - x, y) if vertical else (x, 2 * axis - y))
            for x, y in path]


# --- picking the mode -------------------------------------------------------
def fit_transform(pairs, tol=0.05):
    """What maps net A's landings onto net B's?

    `pairs` is [(pointA, pointB), ...], one per paired landing. Returns
    ('mirror', axis, vertical) or ('translate', dx, dy) or None -- and None
    is a real answer: the placement is not symmetric and entry 1 declines.
    """
    if len(pairs) < 2:
        return None
    dx = {round(b[0] - a[0], 6) for a, b in pairs}
    dy = {round(b[1] - a[1], 6) for a, b in pairs}
    if max(dx) - min(dx) <= tol and max(dy) - min(dy) <= tol:
        return ('translate', sum(b[0] - a[0] for a, b in pairs) / len(pairs),
                sum(b[1] - a[1] for a, b in pairs) / len(pairs))
    for vertical in (True, False):
        i = 0 if vertical else 1
        j = 1 - i
        axes = [(a[i] + b[i]) / 2.0 for a, b in pairs]
        if (max(axes) - min(axes) <= tol and
                all(abs(a[j] - b[j]) <= tol for a, b in pairs)):
            return ('mirror', sum(axes) / len(axes), vertical)
    return None


def plan(trunk, pitch, transform, tol=0.05):
    """Both tracks plus the numbers the acceptance gate wants.

    Mirror mode reflects; translate mode offsets. `matched` is the verdict:
    in translate mode it is False exactly when the trunk's turns do not
    balance, and `residual_um` is then the mismatch to answer for -- reroute
    the trunk for T = 0, or decline.
    """
    mode = transform[0] if transform else 'translate'
    if mode == 'mirror':
        _, axis, vertical = transform
        half = pitch / 2.0
        a = [((x - half, y) if vertical else (x, y - half)) for x, y in trunk]
        b = mirror(a, axis, vertical)
    else:
        a, b = split(trunk, pitch)
    la, lb = length(a), length(b)
    residual = abs(la - lb)
    return {
        'mode': mode,
        'track_a': a,
        'track_b': b,
        'len_a_um': la,
        'len_b_um': lb,
        'residual_um': residual,
        'corners_a': corners(a),
        'corners_b': corners(b),
        'net_turning': net_turning(trunk),
        'matched': residual <= tol and corners(a) == corners(b),
    }


# --- self-test --------------------------------------------------------------
def _random_path(rng, n):
    p = [(0.0, 0.0)]
    horiz = rng.random() < 0.5
    for _ in range(n):
        step = rng.choice([-1, 1]) * rng.uniform(4.0, 20.0)
        x, y = p[-1]
        p.append((x + step, y) if horiz else (x, y + step))
        horiz = not horiz
    return p


def self_test():
    rng = random.Random(20260906)
    pitch = 1.6
    bad = 0

    # 1. the offset law, and 2. equality exactly when the turns balance
    for _ in range(4000):
        trunk = _random_path(rng, rng.randint(2, 9))
        try:
            a, b = split(trunk, pitch)
        except ValueError:
            continue
        T = net_turning(trunk)
        if abs((length(b) - length(a)) - 2 * pitch * T) > 1e-6:
            bad += 1
        if (abs(length(a) - length(b)) < 1e-9) != (T == 0):
            bad += 1
        if corners(a) != corners(trunk) or corners(b) != corners(trunk):
            bad += 1
    print(f"  offset law dL = 2*pitch*T, and equality iff T == 0 "
          f"[{'ok' if bad == 0 else f'{bad} FAILURES'}]")

    # 3. reflection is exact whatever the path does
    bad2 = 0
    for _ in range(2000):
        p = _random_path(rng, rng.randint(2, 9))
        m = mirror(p, 37.5)
        if abs(length(m) - length(p)) > 1e-9 or corners(m) != corners(p):
            bad2 += 1
    print(f"  mirror is an isometry, any net turning "
          f"[{'ok' if bad2 == 0 else f'{bad2} FAILURES'}]")

    # 4. a fold is caught, not drawn. It takes two same-handed turns with a
    #    leg shorter than the offset between them -- a U with a short back.
    #    A single perpendicular jog does NOT fold however short it is: both
    #    tracks step across it together and stay one pitch apart.
    ok4 = True
    try:
        split([(0, 0), (10, 0), (10, 0.4), (0, 0.4)], 4.0)
        ok4 = False
    except ValueError:
        pass
    try:
        split([(0, 0), (10, 0), (10, 0.4), (20, 0.4)], 4.0)
    except ValueError:
        ok4 = False
    print(f"  fold (short U back) rejected, plain short jog allowed "
          f"[{'ok' if ok4 else 'FAILURE'}]")

    # 5. transform fitting, including the answer "not symmetric"
    t = fit_transform([((0, 0), (0, 12)), ((5, 3), (5, 15))])
    ok = t and t[0] == 'translate' and abs(t[2] - 12) < 1e-6
    t = fit_transform([((-4, 0), (14, 0)), ((-2, 9), (12, 9))])
    ok = ok and t and t[0] == 'mirror' and abs(t[1] - 5) < 1e-6 and t[2]
    ok = ok and fit_transform([((0, 0), (0, 12)), ((5, 3), (9, 40))]) is None
    print(f"  transform fit: translate / mirror / not-symmetric "
          f"[{'ok' if ok else 'FAILURE'}]")

    # 6. plan() agrees with the laws on an L trunk -- the shape that fails
    L = [(0, 0), (30, 0), (30, 20)]
    tr = plan(L, pitch, ('translate', 0, 0))
    mr = plan(L, pitch, ('mirror', 15.0, True))
    ok = (not tr['matched'] and abs(tr['residual_um'] - 2 * pitch) < 1e-6
          and mr['matched'] and mr['residual_um'] < 1e-9)
    print(f"  plan(): L-shaped trunk fails in translate mode by 2*pitch, "
          f"exact in mirror mode [{'ok' if ok else 'FAILURE'}]")

    return 0 if (bad == 0 and bad2 == 0 and ok and ok4) else 1


def demo():
    pitch = 1.6
    for name, trunk in [("straight", [(0, 0), (40, 0)]),
                        ("L (one turn)", [(0, 0), (30, 0), (30, 20)]),
                        ("Z (turns balance)",
                         [(0, 0), (18, 0), (18, 12), (40, 12)])]:
        r = plan(trunk, pitch, ('translate', 0, 0))
        print(f"  {name:20s} T={r['net_turning']:+d}  "
              f"a={r['len_a_um']:7.2f}  b={r['len_b_um']:7.2f}  "
              f"residual={r['residual_um']:5.2f} um  "
              f"{'MATCHED' if r['matched'] else 'not matched'}")
    r = plan([(0, 0), (30, 0), (30, 20)], pitch, ('mirror', 15.0, True))
    print(f"  {'L, mirror mode':20s} T={r['net_turning']:+d}  "
          f"a={r['len_a_um']:7.2f}  b={r['len_b_um']:7.2f}  "
          f"residual={r['residual_um']:5.2f} um  "
          f"{'MATCHED' if r['matched'] else 'not matched'}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--demo", action="store_true")
    a = ap.parse_args()
    if a.demo:
        demo()
    raise SystemExit(self_test() if a.self_test or not a.demo else 0)
