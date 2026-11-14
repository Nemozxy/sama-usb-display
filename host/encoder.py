"""Image encoding and panel geometry.

The panel firmware accepts PNG (`display png full`, `display png compress`,
`display png differ init`, `display png differ seq` -- strings extracted from
`fw/4inch_1.91`).  PNG is therefore the primary, evidence-backed encoding; the
RGB565 path is provided only as a clearly-marked alternative because the
firmware strings argue against it.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image


# --------------------------------------------------------------------------
# Panel geometry  [CONFIRMED] evidence/device-table.md (from Version.cs)
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Panel:
    """Geometry of one panel variant.

    Attributes
    ----------
    width, height
        The addressable resolution the vendor software renders at
        (`ScreenDev.width` / `.height`).
    width_real, height_real
        Present only when the physical panel is larger than the addressable
        area (`ScreenDev.width_real` / `.height_real`).
    res
        The vendor's resolution key (`ScreenDev.res`), which also names the
        `theme/<res>` and `video/<res>` asset directories.
    """

    key: str
    model_field: str
    width: int
    height: int
    res: str
    width_real: int | None = None
    height_real: int | None = None
    portrait: bool = True

    @property
    def physical_width(self) -> int:
        return self.width_real or self.width

    @property
    def physical_height(self) -> int:
        return self.height_real or self.height

    @property
    def cropped_x(self) -> int:
        """Pixels of a full-width source image that the panel cannot show, per side."""
        return max(0, (self.physical_width - self.width) // 2)

    @property
    def cropped_y(self) -> int:
        return max(0, (self.physical_height - self.height) // 2)

    def describe(self) -> str:
        extra = ""
        if self.width_real or self.height_real:
            extra = (f"  physical={self.physical_width}x{self.physical_height}"
                     f"  cropped {self.cropped_x}px per side"
                     f"{f', {self.cropped_y}px top/bottom' if self.cropped_y else ''}")
        return f"{self.key:34s} addressable={self.width}x{self.height}  res={self.res}{extra}"


SIX_TWO_PANELS: dict[str, Panel] = {
    "A062": Panel("A062", "Dev_62inch_win207lcd", 368, 960, "368960"),
    "B062": Panel("B062", "Dev_62inchV2_win207lcd", 448, 1280, "4481280"),
    "C062": Panel("C062", "Dev_62inchV3_win207lcd", 448, 1280, "4481280",
                  width_real=480),
    "0062": Panel("0062", "Dev_62inch_winlcd", 368, 960, "368960"),
}


#: The panel actually in hand: `Dev_65inch`, `VID_1D6B&PID_A065`, confirmed on real
#: hardware on 2026-10-14 (a 720x1568 white frame displayed correctly).
#: `res` = "7201568", matching the `theme/7201568` and `video/7201568` asset
#: directories in the installer.  `width_real` is EMPTY for this model in the
#: vendor table, which is exactly why the vendor's encoder performs no resize.
SERIAL_PANELS: dict[str, Panel] = {
    "65inch": Panel("65inch", "Dev_65inch", 720, 1568, "7201568"),
    # The OTHER vendor geometry for this panel family.  `Dev_65inch_win207lcd`
    # (VID_1CBE&PID_A065) is declared 720x1472 with res "7201472".
    #
    # This exists to answer one question: when we send a SHORTER frame, does the
    # device display it 1:1 (in which case the rows we lose from a 1568-row frame
    # really are outside the addressable area), or does it scale/pad it (in which
    # case the lost rows are merely behind the enclosure bezel)?
    "65inch1472": Panel("65inch1472", "Dev_65inch_win207lcd", 720, 1472, "7201472"),
}

#: Every geometry this project knows about (both protocol families).
ALL_PANELS: dict[str, Panel] = {**SIX_TWO_PANELS, **SERIAL_PANELS}


def get_panel(key: str) -> Panel:
    """Look a panel up across both families."""
    if key in ALL_PANELS:
        return ALL_PANELS[key]
    raise KeyError(f"unknown panel {key!r}; have {sorted(ALL_PANELS)}")


# --------------------------------------------------------------------------
# Measured safe area for the 6.5-inch unit in hand  [HARDWARE-CONFIRMED]
# --------------------------------------------------------------------------
# FINAL VALUES, accepted by the operator on 2026-10-14 after reviewing a
# visualisation (`crop_map`) in which the cropped rows are painted pure red and
# the visible rows pure white:
#
#       TOP CROP    = 22 px
#       BOTTOM CROP = 42 px
#       SIDES       = 0 px  (no measurable loss)
#
#       visible rows 22..1525  ->  1504 of 1568 rows = 95.9 %
#
# How it was derived, and why it is trustworthy -- six independent readings, all
# agreeing within their resolution:
#
#   edge_bands  (16 px colour bands flush to each edge, legend in the middle so
#                nothing is read at the occluded edge)
#                   top    "about half of the ORANGE band" (rows 16-31)
#                          -> visible from row ~24
#                   bottom "about a third of the YELLOW band" (rows 1520-1535)
#                          -> last visible row ~1525
#   vertical ruler  row 0 hidden, row 20 marginal, last white row 1567 hidden
#   safe_box 32  top corner markers half eaten, bottom ones invisible
#   safe_box 48  top complete, bottom "still a little short"
#   corners      top cleared at ~16, bottom needed ~40
#   crop_map     operator reviewed the drawn 22/42 and accepted it
#
# The operator then reduced the top figure from 24 to 22 after seeing the drawing,
# so 22 is a DIRECTLY REVIEWED value rather than a purely computed one.  Bottom 42
# was confirmed unchanged.
#
# CAVEAT: the enclosure has ROUNDED CORNERS and those clip deeper AT THE CORNERS
# than along the middle of an edge.  These figures are edge measurements, so
# content destined for a corner needs more margin than this.
SAFE_TOP = 22
SAFE_BOTTOM = 42
SAFE_SIDES = 0

#: Extra margin applied by `safe_area()` / `fit_safe()` on top of the measured
#: crop, to absorb the +-8 px resolution of the band measurement.  This is a
#: SAFETY PADDING, not a measurement -- keep it separate from the constants above.
SAFE_PAD = 8


def safe_area(panel: Panel, pad: int = 0) -> tuple[int, int, int, int]:
    """(left, top, right, bottom) inclusive box of rows/cols known to be visible.

    `pad` moves the box INWARD on all four sides (it never shrinks a margin).
    """
    l = SAFE_SIDES + pad
    t = SAFE_TOP + pad
    r = panel.width - 1 - SAFE_SIDES - pad
    b = panel.height - 1 - SAFE_BOTTOM - pad
    if not (0 <= l < r < panel.width and 0 <= t < b < panel.height):
        raise ValueError(f"pad {pad} leaves no safe area on {panel.key}")
    return l, t, r, b


def fit_safe(image, panel: Panel, pad: int = SAFE_PAD, background=(0, 0, 0),
             resample=None):
    """Scale `image` to fit inside the measured safe area, centred, on a full frame.

    Nothing important ends up in the margin the bezel/crop eats, and the aspect
    ratio is preserved (letterboxed, never stretched).

    `resample` defaults to **BILINEAR**, not LANCZOS.  Profiling the real
    720x1568 pipeline showed the resize was the single largest host-side cost --
    19.95 ms of a 261 ms frame, i.e. 7.6 % -- and LANCZOS was most of it.  For
    downscaling a dashboard to panel size the two are visually
    indistinguishable, so the cheaper filter is the better default.  Pass
    ``resample=PIL.Image.LANCZOS`` when quality matters more than latency (e.g.
    one-off photo display).
    """
    from PIL import Image as _I
    if resample is None:
        resample = _I.BILINEAR
    canvas = _I.new("RGB", (panel.width, panel.height), background)
    left, top, right, bottom = safe_area(panel, pad)
    aw, ah = right - left + 1, bottom - top + 1
    src = image.convert("RGB")
    # `draft` lets the JPEG decoder downscale during decode -- a large extra
    # saving for camera-sized sources, and a no-op for everything else.
    try:
        src.draft("RGB", (aw, ah))
    except Exception:                                         # noqa: BLE001
        pass
    scale = min(aw / src.width, ah / src.height)
    nw, nh = max(1, int(src.width * scale)), max(1, int(src.height * scale))
    resized = src.resize((nw, nh), resample)
    canvas.paste(resized, (left + (aw - nw) // 2, top + (ah - nh) // 2))
    return canvas


# --------------------------------------------------------------------------
# Fitting a source image onto a panel
# --------------------------------------------------------------------------

FIT_MODES = ("crop", "pad", "stretch", "contain")


def fit_to_panel(img: Image.Image, panel: Panel, mode: str = "crop",
                 background: tuple[int, int, int] = (0, 0, 0)) -> Image.Image:
    """Scale/position `img` so it matches the panel's addressable area.

    Parameters
    ----------
    mode
        ``"crop"``    -- scale to cover, then centre-crop.  This reproduces the
                         behaviour a user hits when supplying art sized for the
                         *physical* panel: for ``C062`` (addressable 448 wide,
                         physical 480) exactly ``cropped_x`` pixels are lost per
                         side.  This is the default because it matches the
                         reported "background is cropped on both sides" symptom.
        ``"pad"``     -- scale to fit inside, then centre on a background.
        ``"stretch"`` -- ignore aspect ratio and resize exactly.
        ``"contain"`` -- alias of ``"pad"``.
    """
    if mode not in FIT_MODES:
        raise ValueError(f"mode must be one of {FIT_MODES}, got {mode!r}")

    target = (panel.width, panel.height)
    src = img.convert("RGB")

    if mode == "stretch":
        return src.resize(target, Image.LANCZOS)

    if mode in ("pad", "contain"):
        copy = src.copy()
        copy.thumbnail(target, Image.LANCZOS)
        canvas = Image.new("RGB", target, background)
        canvas.paste(copy, ((panel.width - copy.width) // 2,
                            (panel.height - copy.height) // 2))
        return canvas

    # "crop": scale to cover then centre-crop.
    scale = max(panel.width / src.width, panel.height / src.height)
    new = (max(panel.width, int(round(src.width * scale))),
           max(panel.height, int(round(src.height * scale))))
    resized = src.resize(new, Image.LANCZOS)
    left = (resized.width - panel.width) // 2
    top = (resized.height - panel.height) // 2
    return resized.crop((left, top, left + panel.width, top + panel.height))


def rotate_for_panel(img: Image.Image, degrees: int) -> Image.Image:
    """Rotate a frame.  [INFERENCE] The device rotates in hardware
    (`hwG2dRotateVideoPicture` in the firmware), so a host-side rotation is only
    needed when pre-rendering assets to upload."""
    if degrees % 360 == 0:
        return img
    return img.rotate(-degrees % 360, expand=True)


def scale_brightness(img: Image.Image, percent: int) -> Image.Image:
    """Simulate what a lower backlight looks like -- for previewing only.

    The real brightness command is opcode 14 (`protocol.Cmd.SET_BRIGHTNESS`) and
    is handled by the device's backlight, not by scaling pixel data.
    """
    from PIL import ImageEnhance
    return ImageEnhance.Brightness(img.convert("RGB")).enhance(max(0, min(100, percent)) / 100.0)


# --------------------------------------------------------------------------
# Encoders
# --------------------------------------------------------------------------

def to_png(img: Image.Image, optimize: bool = False) -> bytes:
    """Encode as PNG -- the format the firmware explicitly names.  [CONFIRMED]"""
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG", optimize=optimize)
    return buf.getvalue()


def to_jpeg(img: Image.Image, quality: int = 80) -> bytes:
    """Encode as JPEG.

    [UNKNOWN] No JPEG string appears in the shipped firmware, so the device may
    not accept JPEG for stills.  It is likely used only inside the video
    container.  Present for experimentation; not used by default.
    """
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def to_rgb565(img: Image.Image, byte_order: str = "little") -> bytes:
    """Encode raw RGB565.

    [UNKNOWN -- LIKELY WRONG FOR THIS DEVICE] The firmware's image path is
    PNG-based.  This is included only so a capture can be diffed against it.
    Byte order is a guess.
    """
    rgb = img.convert("RGB")
    out = bytearray()
    for r, g, b in rgb.getdata():
        v = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
        out += v.to_bytes(2, byte_order)
    return bytes(out)


ENCODERS = {
    "png": to_png,
    "jpeg": to_jpeg,
    "rgb565": to_rgb565,
}


def encode_frame(img: Image.Image, panel: Panel, fmt: str = "png",
                 fit: str = "crop") -> bytes:
    """Fit `img` to `panel` and encode it.  This is the one function the rest of
    the prototype uses."""
    try:
        encoder = ENCODERS[fmt]
    except KeyError:
        raise ValueError(f"unknown format {fmt!r}; choose from {sorted(ENCODERS)}")
    fitted = fit_to_panel(img, panel, mode=fit)
    data = encoder(fitted)
    return data


def explain_brightness(req_percent: int, brightness_limit: float = 1.0) -> tuple[int, str]:
    """Reproduce `꽝.cs:6412`.

    The vendor code is::

        level = (byte)((double)level / 2.55 * brightnessLimit);

    so a 0-100 UI value becomes a 0-255 device byte, scaled by the model's
    `brightnessLimit` (e.g. 0.8 for the 12-inch model).
    """
    level = int(req_percent / 2.55 * brightness_limit)
    level = max(0, min(255, level))
    return level, (f"{req_percent}% -> device byte {level} "
                   f"(/2.55 * brightnessLimit={brightness_limit})")
