"""Render the Foundation-only puck contour as a reviewable SVG without AppKit."""

from math import cos, pi, sin, tan
from pathlib import Path


def segments(amount: float, boundary: float) -> list[tuple[tuple[float, float], ...]]:
    """Mirror PuckEdgeContour.segments so the checked-in review image has its exact points."""
    travel = min(max(amount, 0), 1)
    x = min(max((travel - .35) / .65, 0), 1)
    amount = x * x * x * (x * (x * 6 - 15) + 10)
    angles = [135, 157.5, 180, 202.5, 225, 270, 360, 450]
    terminal = [(boundary, 60), (boundary / 2, 30), (0, 0), (boundary / 2, -30),
                (boundary, -60), (80, -70), (200, 0), (80, 70)]
    controls = [((boundary, 45), (boundary * 3 / 4, 37.5)),
                ((boundary / 4, 22.5), (0, 15)), ((0, -15), (boundary / 4, -22.5)),
                ((boundary * 3 / 4, -37.5), (boundary, -45)), ((boundary, -75), (60, -70)),
                ((100, -70), (200, -38)), ((200, 38), (100, 70)), ((60, 70), (boundary, 75))]

    def circle(degrees: float) -> tuple[float, float]:
        radians = degrees * pi / 180
        return 99.5 + 99.5 * cos(radians), 99.5 * sin(radians)

    def tangent(degrees: float) -> tuple[float, float]:
        radians = degrees * pi / 180
        return -99.5 * sin(radians), 99.5 * cos(radians)

    def mix(circle_point: tuple[float, float], tab: tuple[float, float]) -> tuple[float, float]:
        return tuple(a + (b - a) * amount for a, b in zip(circle_point, tab))

    output = []
    for index, start in enumerate(angles):
        end = 495 if index == 7 else angles[index + 1]
        k = 4 / 3 * tan((end - start) * pi / 720)
        a, b = circle(start), circle(end)
        ta, tb = tangent(start), tangent(end)
        c1 = a[0] + ta[0] * k, a[1] + ta[1] * k
        c2 = b[0] - tb[0] * k, b[1] - tb[1] * k
        output.append((mix(a, terminal[index]), mix(c1, controls[index][0]),
                       mix(c2, controls[index][1]), mix(b, terminal[(index + 1) % 8])))
    return output


def path(amount: float, boundary: float, x: float, y: float) -> str:
    """Use right-edge coordinates, flipping the model's edge axis for SVG."""
    contour = segments(amount, boundary)
    point = lambda p: f"{x + p[0]:.2f} {y - p[1]:.2f}"
    first = contour[0][0]
    return "M" + point(first) + " " + " ".join(
        "C" + point(c1) + " " + point(c2) + " " + point(end)
        for _, c1, c2, end in contour
    ) + " Z"


stages = [(0, 206), (.3, 151), (.5, 114), (.7, 77), (1, 22)]
columns = []
for index, (travel, boundary) in enumerate(stages):
    left = 25 + index * 195
    edge = left + 24 + boundary
    columns.append(
        f'<clipPath id="visible{index}"><rect x="{left}" y="55" width="{boundary + 24}" height="230"/></clipPath>'
        f'<text x="{left}" y="35">travel {travel:.1f} · boundary {boundary:.0f}pt</text>'
        f'<path class="shape" d="{path(travel, boundary, left, 165)}" clip-path="url(#visible{index})"/>'
        f'<line class="edge" x1="{edge}" y1="55" x2="{edge}" y2="285"/>'
    )
svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="1000" height="360" viewBox="0 0 1000 360">
<rect width="1000" height="360" fill="#171719"/><style>text{{font:14px -apple-system,BlinkMacSystemFont,sans-serif;fill:#ddd}}.edge{{stroke:#898991;stroke-width:2;stroke-dasharray:5 5}}.shape{{fill:#1e1e21;stroke:#fff;stroke-opacity:.45;stroke-width:2}}.hint{{font-size:12px;fill:#aaa}}</style>
{''.join(columns)}
<text x="25" y="315" class="hint">The live visible-frame mask clips each stage at its moving boundary; the circle stays intact until contact, then symmetric shoulders meet the 22pt tab.</text>
</svg>'''
Path(__file__).resolve().parents[1].joinpath("docs/puck-edge-morph.svg").write_text(svg)
