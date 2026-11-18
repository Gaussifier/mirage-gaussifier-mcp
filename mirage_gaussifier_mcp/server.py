"""MCP server: turn images into editable Gaussian images and edit them.

Run with `uv run mirage-gaussifier-mcp` (stdio). Every tool that changes or shows an image
returns a preview, so the client can see what each step did.
"""

from __future__ import annotations

import io
import json
import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from mcp.server.fastmcp import FastMCP, Image as McpImage
from PIL import Image, ImageDraw, ImageFont

from . import engine as E

mcp = FastMCP("mirage-gaussifier")
_open: dict[str, E.EditableImage] = {}


@dataclass
class Recording:
    frames: list = field(default_factory=list)
    steps: list = field(default_factory=list)
    frames_per_edit: int = 14
    hold_frames: int = 8
    captions: bool = True


_rec: dict[str, Recording] = {}


# ------------------------------------------------------------------ helpers


def _img(name: str) -> E.EditableImage:
    if name not in _open:
        if not (E.WORKSPACE / name / "model").is_dir():
            raise ValueError(f"no Gaussian image named {name!r}; call gaussify_image first")
        _open[name] = E.EditableImage.load(name)
    return _open[name]


def _preview(pil: Image.Image, max_side: int = 640) -> McpImage:
    pil = pil.copy()
    pil.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    pil.save(buf, "PNG")
    return McpImage(data=buf.getvalue(), format="png")


def _font(size: int):
    for path in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                 "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf"):
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def _caption(pil: Image.Image, text: str) -> Image.Image:
    """Image with a caption bar underneath."""
    w, h = pil.size
    bar = max(28, h // 14)
    out = Image.new("RGB", (w, h + bar), (18, 18, 22))
    out.paste(pil, (0, 0))
    draw = ImageDraw.Draw(out)
    size = bar // 2
    while size > 8 and draw.textlength(text, font=_font(size)) > w - 20:  # fit the width
        size -= 1
    draw.text((10, h + bar // 2), text, fill=(240, 240, 240), font=_font(size), anchor="lm")
    return out


def _ease(x: float) -> float:
    return 0.5 - 0.5 * math.cos(math.pi * x)


def _record_frames(name: str, frames: list[Image.Image], caption: str, hold: int | None = None):
    rec = _rec.get(name)
    if rec is None:
        return
    hold = rec.hold_frames if hold is None else hold
    for f in frames + [frames[-1]] * hold:
        rec.frames.append(_caption(f, caption) if rec.captions else f)


def _out_path(name: str, out_path: str | None, suffix: str) -> Path:
    p = Path(out_path).expanduser() if out_path else E.WORKSPACE / name / f"{name}_{suffix}"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _save_gif(frames: list[Image.Image], path: Path, fps: int, width: int = 512) -> None:
    """Animation at `width` pixels wide. A `.webp` path writes animated WebP (full colour, lossy,
    much smaller); otherwise a GIF with one shared palette built from frames across the whole
    recording."""
    small = [f.resize((width, round(f.height * width / f.width)), Image.Resampling.LANCZOS)
             if f.width > width else f for f in frames]
    if path.suffix.lower() == ".webp":
        small[0].save(path, save_all=True, append_images=small[1:], duration=round(1000 / fps),
                      loop=0, quality=80, method=4)
        return
    picks = [small[round(i * (len(small) - 1) / 15)] for i in range(16)] if len(small) > 16 else small
    tw = 160
    tiles = [f.resize((tw, round(f.height * tw / f.width))) for f in picks]
    mosaic = Image.new("RGB", (tw * len(tiles), max(t.height for t in tiles)))
    for i, t in enumerate(tiles):
        mosaic.paste(t, (i * tw, 0))
    palette = mosaic.quantize(colors=255, method=Image.Quantize.MEDIANCUT)
    quant = [f.quantize(palette=palette, dither=Image.Dither.NONE) for f in small]
    quant[0].save(path, save_all=True, append_images=quant[1:], duration=round(1000 / fps), loop=0,
                  optimize=True, disposal=1)


# ------------------------------------------------------------------ tools


@mcp.tool()
def gaussify_image(image_path: str, name: str | None = None, init: str = "gaussifier",
                   iterations: int = 3000, max_side: int = 768, refit: bool = False) -> list:
    """Turn an image into an editable Gaussian image.

    Gaussifier places the initial Gaussians in a fraction of a second (init="gaussifier";
    "random" uses MiraGe's random start), then MiraGe fits flat Gaussians on the image plane
    (about 15 s for 3000 iterations at 768 px). Returns the fit report and a render.
    """
    report = E.gaussify(image_path, name, init=init, iterations=iterations, max_side=max_side,
                        reuse=not refit)
    _open.pop(report["name"], None)
    img = _img(report["name"])
    return [json.dumps(report), _preview(E.to_pil(img.render()))]


@mcp.tool()
def list_images() -> str:
    """Gaussian images in the workspace, with their size, Gaussian count and edit count."""
    out = []
    for d in sorted(E.WORKSPACE.glob("*/report.json")):
        r = json.loads(d.read_text())
        r["edits"] = len(_open[r["name"]].edits) if r["name"] in _open else 0
        out.append(r)
    return json.dumps(out, indent=1)


@mcp.tool()
def list_operations() -> str:
    """Edit operations and their parameters (positions and sizes are fractions of the image)."""
    return json.dumps(E.OPS, indent=1)


@mcp.tool()
def render(name: str, yaw: float = 0.0, pitch: float = 0.0, save_path: str | None = None) -> list:
    """Render the current edited image, optionally with the image plane turned in 3D
    (yaw / pitch in degrees) to show the depth of lifted or tilted parts."""
    pil = E.to_pil(_img(name).render(yaw=yaw, pitch=pitch))
    if save_path:
        Path(save_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        pil.save(Path(save_path).expanduser())
    return [_preview(pil)]


@mcp.tool()
def select_region(name: str, selection: str, shape: str, x0: float = 0, y0: float = 0,
                  x1: float = 1, y1: float = 1, cx: float = 0.5, cy: float = 0.5,
                  rx: float = 0.1, ry: float = 0.1, points: list[list[float]] | None = None,
                  rgb: list[float] | None = None, tolerance: float = 0.15,
                  mask_path: str | None = None, mode: str = "replace") -> list:
    """Select Gaussians by where they are or what colour they are, under a name used by edit.

    shape: "box" (x0, y0, x1, y1), "ellipse" (cx, cy, rx, ry), "polygon" (points [[x, y], ...]),
    all as fractions of width / height from the top-left; "color" (rgb in 0..1, tolerance);
    "mask_image" (mask_path, white = selected); "all". mode: replace, add, subtract, intersect.
    Returns the count and a preview with the selection highlighted.
    """
    params = {"x0": x0, "y0": y0, "x1": x1, "y1": y1, "cx": cx, "cy": cy, "rx": rx, "ry": ry,
              "points": points, "rgb": rgb, "tolerance": tolerance, "path": mask_path}
    img = _img(name)
    count = img.select(selection, shape, params, mode)
    return [f"{selection}: {count} Gaussians selected",
            _preview(E.to_pil(img.render(highlight=selection)))]


@mcp.tool()
def edit(name: str, op: str, params: dict, selection: str = "all", caption: str | None = None) -> list:
    """Apply one edit to the selection (see list_operations) and return the result.

    While a recording is running, the edit is also recorded as a short animation from no
    change to the full edit, with `caption` (default: the edit itself) under it.
    """
    img = _img(name)
    if selection != "all" and selection not in img.selections:
        raise ValueError(f"no selection named {selection!r}; call select_region first")
    e = E.Edit(op, params, selection)
    rec = _rec.get(name)
    if rec is not None:
        n = rec.frames_per_edit
        frames = [E.to_pil(img.render(partial=E.Edit(op, params, selection, _ease(i / n))))
                  for i in range(1, n + 1)]
        rec.steps.append(caption or e.describe())
        _record_frames(name, frames, caption or e.describe())
    img.push(e)
    return [f"step {len(img.edits)}: {e.describe()}", _preview(E.to_pil(img.render()))]


@mcp.tool()
def undo(name: str, caption: str | None = None) -> list:
    """Undo the last edit. While recording, the undo is recorded as the edit running backwards
    (for example a `scatter` dissolve reassembling), with `caption` under it."""
    img = _img(name)
    e = img.undo()
    rec = _rec.get(name)
    if e is not None and rec is not None:
        n = rec.frames_per_edit
        frames = [E.to_pil(img.render(partial=E.Edit(e.op, e.params, e.selection, _ease(1 - i / n))))
                  for i in range(1, n + 1)]
        text = caption or f"undo {e.describe()}"
        rec.steps.append(text)
        _record_frames(name, frames, text)
    return [f"undid {e.describe()}" if e else "nothing to undo", _preview(E.to_pil(img.render()))]


@mcp.tool()
def reset(name: str) -> list:
    """Drop every edit and selection."""
    img = _img(name)
    img.edits.clear()
    img.selections.clear()
    return ["reset", _preview(E.to_pil(img.render()))]


@mcp.tool()
def history(name: str) -> str:
    """The edit stack, in order."""
    return json.dumps([e.describe() for e in _img(name).edits], indent=1)


@mcp.tool()
def start_recording(name: str, frames_per_edit: int = 14, hold_frames: int = 8,
                    captions: bool = True, intro: bool = True) -> str:
    """Start recording the editing process. With intro, the recording opens with the source
    image and the Gaussians growing from dots into the picture."""
    img = _img(name)
    _rec[name] = Recording(frames_per_edit=frames_per_edit, hold_frames=hold_frames, captions=captions)
    if intro:
        _record_frames(name, [E.to_pil(img.target)], "the photo", hold=10)
        frames = [E.to_pil(img.render(splat_scale=max(0.02, _ease(i / 20)))) for i in range(1, 21)]
        _record_frames(name, frames, f"as {img.n:,} Gaussians (Gaussifier + MiraGe)", hold=10)
    return f"recording {name}"


@mcp.tool()
def stop_recording(name: str, out_path: str | None = None, fps: int = 20,
                   orbit_finale: bool = True, width: int = 512) -> str:
    """Stop recording and write the GIF (optionally ending with a 3D turn of the result).
    Returns the path and the list of recorded steps."""
    rec = _rec.pop(name, None)
    if rec is None:
        raise ValueError(f"{name!r} is not being recorded")
    img = _img(name)
    if orbit_finale:
        frames = [E.to_pil(img.render(yaw=28 * math.sin(2 * math.pi * i / 40),
                                      pitch=10 * math.sin(4 * math.pi * i / 40))) for i in range(40)]
        rec.frames += [_caption(f, "it is all 3D Gaussians") if rec.captions else f for f in frames]
    path = _out_path(name, out_path, "editing.gif")
    _save_gif(rec.frames, path, fps, width)
    return json.dumps({"gif": str(path), "frames": len(rec.frames), "steps": rec.steps}, indent=1)


@mcp.tool()
def animate_edit(name: str, op: str, params: dict, selection: str = "all", frames: int = 30,
                 pingpong: bool = True, fps: int = 20, out_path: str | None = None) -> str:
    """Write a looping GIF of one edit going from no change to full (and back with pingpong),
    on top of the current edits, without adding it to the stack."""
    img = _img(name)
    ts = [_ease(i / (frames - 1)) for i in range(frames)]
    if pingpong:
        ts += ts[::-1]
    out = [E.to_pil(img.render(partial=E.Edit(op, params, selection, t))) for t in ts]
    path = _out_path(name, out_path, f"{op}.gif")
    _save_gif(out, path, fps)
    return str(path)


@mcp.tool()
def orbit(name: str, yaw: float = 30.0, pitch: float = 10.0, frames: int = 48, fps: int = 20,
          out_path: str | None = None) -> str:
    """Write a GIF that turns the edited image plane in 3D, showing lifted and tilted parts."""
    img = _img(name)
    out = [E.to_pil(img.render(yaw=yaw * math.sin(2 * math.pi * i / frames),
                               pitch=pitch * math.sin(4 * math.pi * i / frames))) for i in range(frames)]
    path = _out_path(name, out_path, "orbit.gif")
    _save_gif(out, path, fps)
    return str(path)


@mcp.tool()
def export_pseudomesh(name: str, out_path: str | None = None) -> str:
    """Save the current triangles (one per Gaussian) as an OBJ, e.g. to deform in Blender."""
    import trimesh

    img = _img(name)
    tri = img.state()[0].cpu().numpy()
    mesh = trimesh.Trimesh(vertices=tri.reshape(-1, 3), faces=np.arange(len(tri) * 3).reshape(-1, 3),
                           process=False)
    path = _out_path(name, out_path, "pseudomesh.obj")
    mesh.export(path)
    return str(path)


@mcp.tool()
def import_pseudomesh(name: str, obj_path: str) -> list:
    """Replace the triangles with a deformed copy of an exported pseudomesh (same face count)."""
    import torch
    import trimesh

    img = _img(name)
    mesh = trimesh.load(obj_path, force="mesh", process=False)
    tri = torch.tensor(np.asarray(mesh.triangles), dtype=torch.float32, device="cuda")
    if tri.shape[0] != img.n:
        raise ValueError(f"{tri.shape[0]} faces, expected {img.n}")
    img.tri = tri
    img.edits.clear()
    return ["imported", _preview(E.to_pil(img.render()))]


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
