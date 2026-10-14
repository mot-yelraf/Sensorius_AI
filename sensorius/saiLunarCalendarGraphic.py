"""Render a portable Lunar Calendar image for event email notifications.

The email snapshot uses the dashboard's Moon surface, colors, and observer-local
orientation at each phase's event time. Rendering runs in the SMTP worker.
"""

import io
import math
from datetime import datetime
from pathlib import Path


def render_lunar_calendar(event: dict) -> bytes:
    """Return a PNG showing the event phase, surrounding phases, and sky times."""
    from PIL import Image, ImageDraw, ImageFont

    background = "#061929"
    teal, gold, white = "#83d1d1", "#f0ba55", "#e5eeee"
    image = Image.new("RGB", (1200, 560), background)
    draw = ImageDraw.Draw(image)
    font = lambda size: ImageFont.load_default(size=size)
    draw.rounded_rectangle((1, 1, 1198, 558), radius=22, outline="#43808a", width=2)
    draw.text((28, 22), "Lunar Calendar", fill=white, font=font(32))
    draw.text((1170, 36), "OBSERVER-LOCAL VIEW", fill=teal, font=font(16), anchor="ra")
    draw.line((2, 80, 1198, 80), fill="#31545f")
    draw.text((600, 100), "LUNAR EVENT", fill="#9fe76c", font=font(18), anchor="mt")
    texture_path = Path(__file__).resolve().parents[1] / "ui_static/weather_forecast/moon-surface.png"
    with Image.open(texture_path) as source:
        surface = source.convert("RGB")

    def disk(cx, cy, size, phase):
        texture = surface.resize((size, size))
        pixels = texture.load()
        moon = Image.new("RGBA", (size, size))
        target = moon.load()
        angle = math.radians(phase["bright_limb_angle"])
        rotation = math.radians(phase["disk_rotation"])
        rotation_cosine, rotation_sine = math.cos(rotation), math.sin(rotation)
        light_z = 2 * max(0, min(100, phase["illumination"])) / 100 - 1
        light_across = math.sqrt(max(0, 1 - light_z * light_z))
        light_x, light_y = math.sin(angle) * light_across, math.cos(angle) * light_across
        radius = size / 2 - 1
        for y in range(size):
            for x in range(size):
                nx, ny = (x + .5 - size / 2) / radius, (size / 2 - y - .5) / radius
                distance = nx * nx + ny * ny
                if distance > 1:
                    continue
                z = math.sqrt(1 - distance)
                sunlight = nx * light_x + ny * light_y + z * light_z
                terminator = max(0, min(1, (sunlight + .018) / .036))
                brightness = .035 + terminator * (.9 + z * .065)
                texture_x = rotation_cosine * nx - rotation_sine * ny
                texture_y = rotation_sine * nx + rotation_cosine * ny
                source_x = max(0, min(size - 1, round(size / 2 + texture_x * radius)))
                source_y = max(0, min(size - 1, round(size / 2 - texture_y * radius)))
                target[x, y] = (*[round(channel * brightness) for channel in pixels[source_x, source_y]], 255)
        image.paste(moon, (int(cx - size / 2), int(cy - size / 2)), moon)

    draw.arc((65, 160, 1135, 300), 180, 360, fill="#957a43", width=2)
    draw.text((260, 155), "PREVIOUS PHASES", fill=teal, font=font(14), anchor="mt")
    draw.text((940, 155), "UPCOMING PHASES", fill=teal, font=font(14), anchor="mt")
    previous = list(event.get("previous") or [])[-4:]
    upcoming = list(event.get("upcoming") or [])[:4]
    for x, phase in zip([70, 200, 330, 460][-len(previous):] if previous else [], previous):
        disk(x, 235, 82, phase)
        draw.text((x, 290), phase["phase"].replace("_", " ").title(), fill=white, font=font(13), anchor="mt")
        draw.text((x, 315), datetime.fromisoformat(phase["transition_at"]).strftime("%b %d"), fill=gold, font=font(15), anchor="mt")
    for x, phase in zip([740, 870, 1000, 1130], upcoming):
        disk(x, 235, 82, phase)
        draw.text((x, 290), phase["phase"].replace("_", " ").title(), fill=white, font=font(13), anchor="mt")
        draw.text((x, 315), datetime.fromisoformat(phase["transition_at"]).strftime("%b %d"), fill=gold, font=font(15), anchor="mt")
    disk(600, 220, 148, event)
    draw.text((600, 310), event["phase"].replace("_", " ").title(), fill=white, font=font(22), anchor="mt")
    at = datetime.fromisoformat(event["transition_at"])
    draw.text((600, 350), at.strftime("%b %d, %Y · %I:%M %p (UTC%z)"), fill=gold, font=font(17), anchor="mt")
    context = str(event.get("context", "")).splitlines()
    location = next((line.split("; location ", 1)[-1] for line in context if "; location " in line), "")
    draw.text((600, 390), location, fill=teal, font=font(15), anchor="mt")
    for row, labels, color in ((440, ("Moonrise", "Moonset"), teal), (495, ("Sunrise", "Sunset"), gold)):
        draw.line((80, row + 26, 1120, row + 26), fill=color)
        for x, label in zip((100, 730), labels):
            text = next((line for line in context if line.startswith(label + ":")), label + ": unavailable")
            draw.text((x, row), text, fill=color, font=font(17))
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()
