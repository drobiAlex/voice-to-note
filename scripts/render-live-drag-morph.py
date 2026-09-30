"""Render the production held-drag cubics at five real proximity gaps."""

from math import cos, pi, sin, tan
from pathlib import Path


def segments(amount: float, boundary: float):
    """Mirror the eight Foundation-only production cubics before edge rotation."""
    angles = [135, 157.5, 180, 202.5, 225, 270, 360, 450]
    terminal = [(boundary, 60), (boundary / 2, 30), (0, 0), (boundary / 2, -30),
                (boundary, -60), (80, -70), (200, 0), (80, 70)]
    controls = [((boundary, 45), (boundary * 3 / 4, 37.5)),
                ((boundary / 4, 22.5), (0, 15)), ((0, -15), (boundary / 4, -22.5)),
                ((boundary * 3 / 4, -37.5), (boundary, -45)), ((boundary, -75), (60, -70)),
                ((100, -70), (200, -38)), ((200, 38), (100, 70)), ((60, 70), (boundary, 75))]

    def circle(degrees):
        radians = degrees * pi / 180
        return 99.5 + 99.5 * cos(radians), 99.5 * sin(radians)

    def tangent(degrees):
        radians = degrees * pi / 180
        return -99.5 * sin(radians), 99.5 * cos(radians)

    def mix(left, right):
        return tuple(a + (b - a) * amount for a, b in zip(left, right, strict=True))

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


def held_segments(amount: float, boundary: float):
    """Translate the terminal 22-point tab to the current physical boundary."""
    circle = segments(0, 0)
    terminal = segments(1, 22)
    shift = boundary - 22
    return [tuple(tuple(a + ((b + (shift if axis == 0 else 0)) - a) * amount
                             for axis, (a, b) in enumerate(zip(left_point, right_point, strict=True)))
                  for left_point, right_point in zip(left, right, strict=True))
            for left, right in zip(circle, terminal, strict=True)]


def sample(cubics, count=24):
    """Flatten every production cubic densely enough for both SVG and PNG review."""
    points = []
    for start, control1, control2, end in cubics:
        for index in range(count):
            t = index / count
            inverse = 1 - t
            points.append(tuple(inverse ** 3 * start[axis]
                                + 3 * inverse ** 2 * t * control1[axis]
                                + 3 * inverse * t ** 2 * control2[axis]
                                + t ** 3 * end[axis] for axis in (0, 1)))
    return points


def clip_right(points, boundary):
    """Clip a sampled closed contour instead of projecting hidden points."""
    output = []
    prior = points[-1]
    for point in points:
        prior_inside = prior[0] <= boundary
        point_inside = point[0] <= boundary
        if prior_inside != point_inside:
            portion = (boundary - prior[0]) / (point[0] - prior[0])
            output.append((boundary, prior[1] + (point[1] - prior[1]) * portion))
        if point_inside:
            output.append(point)
        prior = point
    return output


def render():
    """Write review artifacts without adding Pillow to the project dependencies."""
    gaps = [24, 18, 13, 7, 2]
    scale = 3
    column_width = 300
    width, height = column_width * len(gaps), 330
    rows = []
    polygons = []
    for index, gap in enumerate(gaps):
        amount = (24 - gap) / 22
        boundary = 200 + gap
        # Production subtracts the half-point stroke inset before constructing
        # the cubics, then adds it back at the local path origin.
        points = [(index * column_width + 25 + 24.5 + x, 160 - y)
                  for x, y in sample(held_segments(amount, boundary - 0.5))]
        edge = index * column_width + 25 + 24 + boundary
        clipped = clip_right(points, min(edge, index * column_width + 25 + 248))
        polygons.append((clipped, edge, index * column_width + 25, gap, amount))
        path = " ".join(("M" if point == clipped[0] else "L") + f"{point[0]:.2f},{point[1]:.2f}"
                        for point in clipped) + " Z"
        rows.append(
            f'<text x="{index * column_width + 25}" y="28">gap {gap}pt · morph {amount:.2f}</text>'
            f'<rect class="window" x="{index * column_width + 25}" y="36" width="248" height="248"/>'
            f'<path class="shape" d="{path}"/><line class="edge" x1="{edge}" y1="36" x2="{edge}" y2="284"/>'
        )
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">
<rect width="100%" height="100%" fill="#171719"/><style>text{{font:14px sans-serif;fill:#ddd}}.window{{fill:none;stroke:#555;stroke-dasharray:3 5}}.edge{{stroke:#8ba4ff;stroke-width:2}}.shape{{fill:#1e1e21;stroke:#ddd;stroke-width:1.5}}</style>
{''.join(rows)}<text x="25" y="315">Sampled production cubics; blue line is visible-frame clip boundary, dashed box is the 248pt window.</text></svg>'''
    artifacts = Path(__file__).resolve().parents[1] / "artifacts"
    artifacts.mkdir(exist_ok=True)
    (artifacts / "live-bridge-preview.svg").write_text(svg)

    from PIL import Image, ImageDraw
    image = Image.new("RGB", (width * scale, height * scale), "#171719")
    draw = ImageDraw.Draw(image)
    for points, edge, left, gap, amount in polygons:
        scaled = [(round(x * scale), round(y * scale)) for x, y in points]
        draw.polygon(scaled, fill="#1e1e21", outline="#dddddd", width=4)
        draw.rectangle((left * scale, 36 * scale, (left + 248) * scale, 284 * scale),
                       outline="#555555", width=2)
        draw.line((edge * scale, 36 * scale, edge * scale, 284 * scale), fill="#8ba4ff", width=5)
        draw.text((left * scale, 10 * scale), f"gap {gap}pt  morph {amount:.2f}", fill="#dddddd")
    image.resize((width, height), Image.Resampling.LANCZOS).save(artifacts / "live-bridge-preview.png")


if __name__ == "__main__":
    render()
