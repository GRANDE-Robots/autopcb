"""make_icon.py: draws the 1024x1024 AutoPCB app icon PNG.

Copyright: GRANDE Robots and Aaron John Sabu
"""

import sys
from pathlib import Path

from PIL import Image, ImageDraw


class IconDrawer:
    """Draws a green board with copper traces and pads."""

    SIZE_PX = 1024

    def __call__(self, output: Path) -> None:
        """Write the icon to a PNG file.

        Inputs: output, the PNG path to create.
        Outputs: an opaque 1024x1024 RGB PNG without an alpha channel.
        """
        image = Image.new("RGB", (self.SIZE_PX, self.SIZE_PX), (16, 82, 56))
        draw = ImageDraw.Draw(image)
        copper = (230, 168, 76)
        trace_px = 36
        for points in (
            [(220, 220), (220, 512), (512, 512), (512, 804), (804, 804)],
            [(220, 804), (360, 804), (360, 660)],
            [(804, 220), (660, 220), (660, 360)],
        ):
            draw.line(points, fill=copper, width=trace_px, joint="curve")
        for x, y in ((220, 220), (220, 804), (804, 220), (804, 804), (512, 512)):
            draw.ellipse((x - 62, y - 62, x + 62, y + 62), fill=copper)
            draw.ellipse((x - 26, y - 26, x + 26, y + 26), fill=(16, 82, 56))
        image.save(output)


if __name__ == "__main__":
    IconDrawer()(Path(sys.argv[1]))
