"""Editable Gaussian images: Gaussifier initialisation, MiraGe fitting, triangle-space edits.

An image becomes a set of flat 3D Gaussians on the plane y = 0 (MiraGe). Each Gaussian is
controlled by a triangle (its centre and two scaled axes), so every edit is a function on
triangle vertices; the renderer rebuilds position, scale and rotation from the triangles.
Every edit takes a strength t in [0, 1] (0 = no change, 1 = full edit), which is what lets
an edit be animated or recorded as it is applied.
"""

from __future__ import annotations

import colorsys
import json
import math
import os
import shutil
import subprocess
import sys
import time
from argparse import Namespace
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import torch
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
MIRAGE_DIR = Path(os.environ.get("MIRAGE_DIR", ROOT / "third_party" / "MiraGe")).resolve()
WORKSPACE = Path(os.environ.get("MGMCP_WORKSPACE", ROOT / "workspace")).resolve()
_FUSED = ROOT / "third_party" / "gaussifier" / "cpp_inference" / "build" / "fused" / "libgaussifier_fused.so.1"
if _FUSED.exists():
    os.environ.setdefault("GAUSSIFIER_FUSED_LIBRARY", str(_FUSED))


def _mirage_path() -> None:
    if str(MIRAGE_DIR) not in sys.path:
        sys.path.insert(0, str(MIRAGE_DIR))


# ------------------------------------------------------------------ fitting


def _camera_info(data_dir: Path, name: str, distance: float):
    """MiraGe's own front camera for the image (R, T, FovX, FovY, width, height)."""
    _mirage_path()
    from models.scenes.dataset_readers import CreateCamerasTransforms

    return CreateCamerasTransforms(str(data_dir), name, False, [-distance])[0]


def _plane_points(info, u: np.ndarray, v: np.ndarray) -> np.ndarray:
    """Pixel coordinates (u right, v down, in pixels) -> points on the plane y = 0."""
    W, H = info.width, info.height
    w2c = np.eye(4)
    w2c[:3, :3] = info.R.T  # MiraGe stores R transposed
    w2c[:3, 3] = info.T
    c2w = np.linalg.inv(w2c)
    fx = W / (2 * math.tan(info.FovX / 2))
    fy = H / (2 * math.tan(info.FovY / 2))
    d_cam = np.stack([(u + 0.5 - W / 2) / fx, (v + 0.5 - H / 2) / fy, np.ones_like(u)], -1)
    d = d_cam @ c2w[:3, :3].T
    o = c2w[:3, 3]
    s = -o[1] / d[:, 1]  # ray o + s d meets y = 0
    return o[None] + s[:, None] * d


def _write_ply(path: Path, xyz: np.ndarray, rgb: np.ndarray) -> None:
    from plyfile import PlyData, PlyElement

    dtype = [(k, "f4") for k in ("x", "y", "z", "nx", "ny", "nz")]
    dtype += [(k, "u1") for k in ("red", "green", "blue")]
    el = np.empty(len(xyz), dtype=dtype)
    el["x"], el["y"], el["z"] = xyz.T
    el["nx"] = el["ny"] = el["nz"] = 0
    rgb8 = np.clip(rgb * 255, 0, 255).astype(np.uint8)
    el["red"], el["green"], el["blue"] = rgb8.T
    PlyData([PlyElement.describe(el, "vertex")]).write(str(path))


def gaussifier_init(image: np.ndarray, info, out_ply: Path) -> dict:
    """Gaussifier placement and colours -> MiraGe's initial point cloud on the image plane."""
    from gaussifier_sampler import GaussifierSampler

    t0 = time.time()
    result = GaussifierSampler.bundled().sample(image)
    dt = time.time() - t0
    H, W = image.shape[1:]
    pts = np.asarray(result.final_points, np.float64)  # normalised [x, y]
    xyz = _plane_points(info, pts[:, 0] * W - 0.5, pts[:, 1] * H - 0.5)
    rgb = np.clip(np.asarray(result.final_unweighted_color), 0, 1)
    _write_ply(out_ply, xyz, rgb)
    mse = float(((np.asarray(result.final_rendered) - image) ** 2).mean())
    return {"gaussians": int(len(pts)), "seconds": round(dt, 3),
            "gaussifier_psnr_db": round(-10 * math.log10(max(mse, 1e-12)), 2)}


def gaussify(image_path: str | Path, name: str | None = None, *, init: str = "gaussifier",
             iterations: int = 3000, gs_type: str = "amorphous", max_side: int = 768,
             distance: float = 1.0, reuse: bool = True) -> dict:
    """Turn an image into an editable MiraGe Gaussian image (one model per name). With reuse,
    an existing fit of the same image and settings is loaded instead of refitted."""
    image_path = Path(image_path).expanduser().resolve()
    name = name or image_path.stem
    work = WORKSPACE / name
    import hashlib

    settings = {"source": image_path.name,
                "source_sha256": hashlib.sha256(image_path.read_bytes()).hexdigest(),
                "init": init, "iterations": iterations, "gs_type": gs_type,
                "max_side": max_side, "distance": distance}
    if reuse and (work / "report.json").exists():
        old = json.loads((work / "report.json").read_text())
        if old.get("settings") == settings:
            return old
    data, model = work / "data", work / "model"
    shutil.rmtree(work, ignore_errors=True)
    data.mkdir(parents=True)
    im = Image.open(image_path).convert("RGB")
    if max(im.size) > max_side:
        s = max_side / max(im.size)
        im = im.resize((round(im.width * s) // 4 * 4, round(im.height * s) // 4 * 4),
                       Image.Resampling.LANCZOS)
    im.save(data / f"{name}.png")
    arr = np.asarray(im, np.float32).transpose(2, 0, 1) / 255
    report = {"name": name, "width": im.width, "height": im.height, "init": init,
              "gs_type": gs_type, "iterations": iterations, "settings": settings}
    if init == "gaussifier":
        report["gaussifier"] = gaussifier_init(arr, _camera_info(data, name, distance),
                                               data / "points3d.ply")
    t0 = time.time()
    cmd = [sys.executable, "train.py", "-s", str(data), "-m", str(model), "--gs_type", gs_type,
           "--camera", "mirror", "--distance", str(distance), "--iterations", str(iterations),
           "--save_iterations", str(iterations), "--test_iterations", str(iterations), "--quiet"]
    proc = subprocess.run(cmd, cwd=MIRAGE_DIR, capture_output=True, text=True)
    if proc.returncode:
        raise RuntimeError(f"MiraGe training failed:\n{proc.stderr[-3000:]}")
    report["fit_seconds"] = round(time.time() - t0, 1)
    img = EditableImage.load(name)
    report["gaussians"] = img.n
    report["psnr_db"] = round(img.psnr(), 2)
    (work / "report.json").write_text(json.dumps(report, indent=1))
    return report


# ------------------------------------------------------------------ editing


def _rgb_to_sh(rgb: torch.Tensor) -> torch.Tensor:
    return (rgb - 0.5) / 0.28209479177387814


def _sh_to_rgb(sh: torch.Tensor) -> torch.Tensor:
    return sh * 0.28209479177387814 + 0.5


def _hue_matrix(degrees: float, device) -> torch.Tensor:
    """Rotation of RGB about the grey axis (hue shift)."""
    a = math.radians(degrees)
    k = torch.tensor([1.0, 1.0, 1.0], device=device) / math.sqrt(3)
    K = torch.tensor([[0, -k[2], k[1]], [k[2], 0, -k[0]], [-k[1], k[0], 0]], device=device)
    return torch.eye(3, device=device) + math.sin(a) * K + (1 - math.cos(a)) * (K @ K)


@dataclass
class Edit:
    op: str
    params: dict
    selection: str = "all"
    t: float = 1.0

    def describe(self) -> str:
        args = ", ".join(f"{k}={v}" for k, v in self.params.items())
        where = "" if self.selection == "all" else f" on '{self.selection}'"
        return f"{self.op}({args}){where}"


@dataclass
class EditableImage:
    name: str
    tri: torch.Tensor          # [N, 3, 3] base triangles
    dc: torch.Tensor           # [N, 1, 3] SH DC
    rest: torch.Tensor         # [N, 15, 3] higher SH
    opacity: torch.Tensor      # [N, 1] activated opacity
    gaussians: object
    camera: object
    target: torch.Tensor       # [3, H, W]
    edits: list = field(default_factory=list)
    selections: dict = field(default_factory=dict)
    redo: list = field(default_factory=list)

    # ---------------------------------------------------------- loading / rendering

    @classmethod
    def load(cls, name: str) -> EditableImage:
        _mirage_path()
        from scene import Scene
        from models.flat_splatting.scene.points_gaussian_model import PointsGaussianModel

        model = WORKSPACE / name / "model"
        cfg = eval(open(model / "cfg_args").read(), {"Namespace": Namespace})  # noqa: S307
        cfg.model_path, cfg.data_device = str(model), "cuda"
        with torch.no_grad():
            g = PointsGaussianModel(cfg.sh_degree)
            scene = Scene(cfg, g, load_iteration=-1, shuffle=False)
            g.prepare_vertices()
            cam = scene.getTrainCameras()[0]  # the front (non-mirrored) view
            return cls(name, g.triangles.clone(), g._features_dc.clone(), g._features_rest.clone(),
                       g.get_opacity.clone(), g, cam, cam.original_image.cuda())

    @property
    def n(self) -> int:
        return int(self.tri.shape[0])

    @property
    def size(self) -> tuple[int, int]:
        return int(self.camera.image_width), int(self.camera.image_height)

    def _project(self, pts: torch.Tensor) -> torch.Tensor:
        """World points [..., 3] -> pixel coordinates [..., 2] in the front view."""
        W, H = self.size
        p = torch.cat([pts, torch.ones_like(pts[..., :1])], -1) @ self.camera.full_proj_transform
        ndc = p[..., :2] / p[..., 3:4]
        return torch.stack([((ndc[..., 0] + 1) * W - 1) / 2, ((ndc[..., 1] + 1) * H - 1) / 2], -1)

    def _unproject(self, uv: torch.Tensor, y: torch.Tensor) -> torch.Tensor:
        """Pixel coordinates + plane offset y -> world points (inverse of _project on y planes)."""
        W, H = self.size
        c2w = torch.inverse(self.camera.world_view_transform.T)
        fx = W / (2 * math.tan(self.camera.FoVx / 2))
        fy = H / (2 * math.tan(self.camera.FoVy / 2))
        d = torch.stack([(uv[..., 0] + 0.5 - W / 2) / fx, (uv[..., 1] + 0.5 - H / 2) / fy,
                         torch.ones_like(uv[..., 0])], -1) @ c2w[:3, :3].T
        o = c2w[:3, 3]
        s = (y - o[1]) / d[..., 1]
        return o + s[..., None] * d

    def towards_camera(self) -> torch.Tensor:
        """Unit vector from the image plane toward the front camera."""
        cam = torch.inverse(self.camera.world_view_transform.T)[:3, 3]
        centre = self._unproject(torch.tensor([[self.size[0] / 2 - 0.5, self.size[1] / 2 - 0.5]],
                                              device="cuda"), torch.zeros(1, device="cuda"))[0]
        v = cam - centre
        return v / v.norm()

    def plane_width(self) -> torch.Tensor:
        """Width of the image on the plane, in world units."""
        z = torch.zeros(1, device="cuda")
        left = self._unproject(torch.tensor([[-0.5, 0.0]], device="cuda"), z)
        right = self._unproject(torch.tensor([[self.size[0] - 0.5, 0.0]], device="cuda"), z)
        return (right - left).norm()

    def state(self, upto: int | None = None, partial: Edit | None = None):
        """Triangles, SH and opacity after the edit stack (optionally plus one partial edit)."""
        tri, dc, rest, op = self.tri.clone(), self.dc.clone(), self.rest.clone(), self.opacity.clone()
        edits = list(self.edits if upto is None else self.edits[:upto])
        if partial is not None:
            edits.append(partial)
        for e in edits:
            tri, dc, rest, op = apply_edit(self, e, tri, dc, rest, op)
        return tri, dc, rest, op

    def view(self, partial: Edit | None = None) -> dict:
        """Camera after the edit stack: `camera` edits set yaw, pitch (degrees) and zoom, each
        eased from the previous view by the edit's strength."""
        v = {"yaw": 0.0, "pitch": 0.0, "zoom": 1.0}
        for e in self.edits + ([partial] if partial is not None else []):
            if e.op == "camera":
                for k in v:
                    if k in e.params:
                        v[k] = v[k] + (float(e.params[k]) - v[k]) * e.t
        return v

    @torch.no_grad()
    def render(self, yaw: float = 0.0, pitch: float = 0.0, partial: Edit | None = None,
               highlight: str | None = None, state=None, splat_scale: float = 1.0) -> torch.Tensor:
        _mirage_path()
        from renderer.gaussian_points_animated_renderer import render

        tri, dc, rest, op = state if state is not None else self.state(partial=partial)
        if highlight:
            m = self.mask(highlight, tri)
            rgb = _sh_to_rgb(dc[:, 0])
            rgb = torch.where(m[:, None], 0.5 * rgb + 0.5 * torch.tensor([1.0, 0.2, 0.6], device=rgb.device), 0.35 * rgb)
            dc = _rgb_to_sh(rgb)[:, None]
            rest = rest * 0
        v = self.view(partial) if state is None else {"yaw": 0.0, "pitch": 0.0, "zoom": 1.0}
        yaw, pitch, zoom = yaw + v["yaw"], pitch + v["pitch"], v["zoom"]
        if yaw or pitch or zoom != 1.0:  # turn / zoom the scene about the image centre
            c = self._unproject(torch.tensor([[self.size[0] / 2 - 0.5, self.size[1] / 2 - 0.5]], device="cuda"),
                                torch.zeros(1, device="cuda"))[0]
            Ry = _axis_rotation(torch.tensor([0.0, 0.0, 1.0]), yaw)
            Rx = _axis_rotation(torch.tensor([1.0, 0.0, 0.0]), pitch)
            R = (Rx @ Ry).cuda()
            tri = ((tri - c) @ R.T) * zoom + c
        g = self.gaussians
        g._features_dc, g._features_rest = dc, rest
        g._opacity = torch.logit(op.clamp(1e-6, 1 - 1e-6))
        pipe = Namespace(debug=False, compute_cov3D_python=False, convert_SHs_python=False)
        img = render(tri.contiguous(), self.camera, g, pipe, torch.zeros(3, device="cuda"),
                     scaling_modifier=splat_scale)["render"]
        return img.clamp(0, 1)

    def psnr(self) -> float:
        img = self.render(state=(self.tri, self.dc, self.rest, self.opacity))
        return -10 * math.log10(max(((img - self.target) ** 2).mean().item(), 1e-12))

    # ---------------------------------------------------------- selections

    def centres_px(self, tri: torch.Tensor | None = None) -> torch.Tensor:
        tri = self.state()[0] if tri is None else tri
        return self._project(tri[:, 0])

    def mask(self, selection: str, tri: torch.Tensor | None = None) -> torch.Tensor:
        n = self.n if tri is None else tri.shape[0]
        if selection == "all":
            return torch.ones(n, dtype=torch.bool, device="cuda")
        m = self.selections[selection]
        if m.shape[0] < n:  # Gaussians added later (duplicate) are not in an older selection
            m = torch.cat([m, torch.zeros(n - m.shape[0], dtype=torch.bool, device="cuda")])
        return m[:n]

    def select(self, name: str, shape: str, params: dict, mode: str = "replace") -> int:
        tri, dc, _, op = self.state()
        uv = self._project(tri[:, 0])
        W, H = self.size
        if shape == "all":
            m = torch.ones(tri.shape[0], dtype=torch.bool, device="cuda")
        elif shape == "box":  # x0, y0, x1, y1 as fractions of width / height
            x0, y0, x1, y1 = (params[k] for k in ("x0", "y0", "x1", "y1"))
            m = (uv[:, 0] >= x0 * W) & (uv[:, 0] <= x1 * W) & (uv[:, 1] >= y0 * H) & (uv[:, 1] <= y1 * H)
        elif shape == "ellipse":  # cx, cy, rx, ry as fractions
            dx = (uv[:, 0] - params["cx"] * W) / (params["rx"] * W)
            dy = (uv[:, 1] - params["cy"] * H) / (params["ry"] * H)
            m = dx * dx + dy * dy <= 1
        elif shape == "polygon":  # points: [[x, y], ...] as fractions
            from matplotlib.path import Path as MplPath

            poly = MplPath(np.asarray(params["points"]) * [W, H])
            m = torch.from_numpy(poly.contains_points(uv.cpu().numpy())).cuda()
        elif shape == "color":  # rgb in [0, 1] and tolerance (Euclidean)
            rgb = _sh_to_rgb(dc[:, 0]).clamp(0, 1)
            ref = torch.tensor(params["rgb"], device="cuda", dtype=rgb.dtype)
            m = (rgb - ref).norm(dim=1) <= params.get("tolerance", 0.15)
        elif shape == "mask_image":  # white = selected, resized to the image
            mk = Image.open(params["path"]).convert("L").resize((W, H))
            mk = torch.from_numpy(np.asarray(mk) > 127).cuda()
            px = uv.round().long()
            ok = (px[:, 0] >= 0) & (px[:, 0] < W) & (px[:, 1] >= 0) & (px[:, 1] < H)
            m = torch.zeros(tri.shape[0], dtype=torch.bool, device="cuda")
            m[ok] = mk[px[ok, 1], px[ok, 0]]
        else:
            raise ValueError(f"unknown selection shape {shape!r}")
        m = m & (op[:, 0] > 0.01)
        if mode != "replace" and name in self.selections:
            old = self.mask(name, tri)
            m = {"add": old | m, "subtract": old & ~m, "intersect": old & m}[mode]
        self.selections[name] = m
        return int(m.sum())

    # ---------------------------------------------------------- history

    def push(self, edit: Edit) -> None:
        self.edits.append(edit)
        self.redo.clear()

    def undo(self) -> Edit | None:
        if self.edits:
            self.redo.append(self.edits.pop())
            return self.redo[-1]
        return None


def _axis_rotation(axis: torch.Tensor, degrees: float) -> torch.Tensor:
    a = math.radians(degrees)
    k = axis / axis.norm()
    K = torch.tensor([[0, -k[2], k[1]], [k[2], 0, -k[0]], [-k[1], k[0], 0]])
    return torch.eye(3) + math.sin(a) * K + (1 - math.cos(a)) * (K @ K)


# ------------------------------------------------------------------ edit operations

OPS = {
    "translate": "move: dx, dy (fractions of width / height)",
    "rotate": "in-plane rotation: degrees, optional cx, cy (fractions; default selection centre)",
    "scale": "resize: factor (or fx, fy), optional cx, cy",
    "bulge": "magnify (strength > 0) or pinch (< 0) inside a circle: cx, cy, radius, strength",
    "swirl": "twist inside a circle: cx, cy, radius, degrees",
    "wave": "ripple: amplitude (fraction of height), wavelength (fraction of width), angle (deg), phase (deg)",
    "tilt3d": "turn the selection out of the image plane: yaw, pitch (degrees), optional cx, cy",
    "lift": "move toward (+) or away from (-) the viewer: amount (fraction of width)",
    "recolor": "hue (degrees), saturation (x), brightness (x); tint [r,g,b] with tint_strength mixes "
               "toward tint x 2 x grey level (0.5 is neutral, values near 1 brighten strongly, "
               "above 1 make dark parts glow)",
    "fade": "opacity multiplier: opacity (0 hides)",
    "duplicate": "copy the selection: dx, dy (fractions)",
    "flip": "mirror the selection: axis 'x' or 'y'",
    "scatter": "Gaussians fly apart one by one in 3D (dissolve): strength (fraction of width), "
               "toward (share of motion toward the viewer), gravity (fraction of height), fade "
               "(0..1 opacity loss), seed. Undo or animate it back to reassemble",
    "shrink_splats": "shrink every selected Gaussian about its own centre, revealing the splats: "
                     "factor (0.2 turns the picture into dots)",
    "fold": "fold the selection out of the plane like paper along a line: axis 'x' (vertical "
            "line at position) or 'y' (horizontal line), position (fraction), side ('after' or "
            "'before' the line), degrees",
    "ripple3d": "water-drop ripple in depth: cx, cy, amplitude (fraction of width), wavelength "
                "(fraction of width), radius (falloff, fraction of width), phase (degrees)",
    "camera": "move the view (applies to everything, not a selection): yaw, pitch (degrees), zoom "
              "(>1 closer; about 1.15 hides the image edges when the view is turned)",
}


def apply_edit(img: EditableImage, e: Edit, tri, dc, rest, op):
    t, p = float(e.t), e.params
    m = img.mask(e.selection, tri)
    if not m.any():
        return tri, dc, rest, op
    W, H = img.size
    verts = tri[m]                                   # [k, 3, 3]
    uv = img._project(verts)                         # [k, 3, 2]
    yv = verts[..., 1]
    centre = uv[:, 0].mean(0)

    def ctr():
        return torch.tensor([p["cx"] * W, p["cy"] * H], device="cuda") if "cx" in p else centre

    def to_world(new_uv):
        return img._unproject(new_uv, yv)

    if e.op == "translate":
        d = torch.tensor([p.get("dx", 0) * W, p.get("dy", 0) * H], device="cuda") * t
        tri[m] = to_world(uv + d)
    elif e.op == "rotate":
        c = ctr()
        a = math.radians(p["degrees"] * t)
        R = torch.tensor([[math.cos(a), -math.sin(a)], [math.sin(a), math.cos(a)]], device="cuda")
        tri[m] = to_world((uv - c) @ R.T + c)
    elif e.op == "scale":
        c = ctr()
        f = torch.tensor([p.get("fx", p.get("factor", 1.0)), p.get("fy", p.get("factor", 1.0))], device="cuda")
        tri[m] = to_world((uv - c) * (1 + (f - 1) * t) + c)
    elif e.op in ("bulge", "swirl"):
        c = torch.tensor([p["cx"] * W, p["cy"] * H], device="cuda")
        r = p["radius"] * max(W, H)
        d = uv - c
        dist = d.norm(dim=-1, keepdim=True)
        w = (1 - (dist / r).clamp(max=1)) ** 2       # 1 at the centre, 0 at the rim
        if e.op == "bulge":
            # local scale 1 + k at the centre, 1 at the rim: k > 0 magnifies, k < 0 pinches
            k = p["strength"] * t
            new = c + d * (1 + k * w).clamp(min=0.05)
        else:
            a = math.radians(p["degrees"] * t) * w[..., 0]
            cs, sn = torch.cos(a), torch.sin(a)
            new = c + torch.stack([cs * d[..., 0] - sn * d[..., 1], sn * d[..., 0] + cs * d[..., 1]], -1)
        tri[m] = to_world(new)
    elif e.op == "wave":
        ang = math.radians(p.get("angle", 0))
        dirv = torch.tensor([math.cos(ang), math.sin(ang)], device="cuda")
        perp = torch.tensor([-math.sin(ang), math.cos(ang)], device="cuda")
        s = (uv @ dirv) / (p["wavelength"] * W) * 2 * math.pi + math.radians(p.get("phase", 0))
        tri[m] = to_world(uv + (p["amplitude"] * H * t * torch.sin(s))[..., None] * perp)
    elif e.op == "tilt3d":
        c3 = img._unproject(ctr()[None], torch.zeros(1, device="cuda"))[0]
        R = (_axis_rotation(torch.tensor([1.0, 0.0, 0.0]), p.get("pitch", 0) * t)
             @ _axis_rotation(torch.tensor([0.0, 0.0, 1.0]), p.get("yaw", 0) * t)).cuda()
        tri[m] = (verts - c3) @ R.T + c3
    elif e.op == "lift":
        towards = img.towards_camera()
        tri[m] = verts + towards * p["amount"] * img.plane_width() * t
    elif e.op == "recolor":
        rgb = _sh_to_rgb(dc[m, 0])
        M = _hue_matrix(p.get("hue", 0) * t, rgb.device)
        grey = rgb.mean(1, keepdim=True)
        new = (rgb - 0.5) @ M.T + 0.5
        sat = 1 + (p.get("saturation", 1.0) - 1) * t
        new = grey + (new - grey) * sat
        new = new * (1 + (p.get("brightness", 1.0) - 1) * t)
        if "tint" in p:
            k = p.get("tint_strength", 0.6) * t
            new = new * (1 - k) + torch.tensor(p["tint"], device="cuda") * grey.clamp(0.15) * 2 * k
        dc[m, 0] = _rgb_to_sh(new)
        rest[m] = (rest[m] @ M.T) * sat
    elif e.op == "fade":
        op[m] = op[m] * (1 + (p.get("opacity", 0.0) - 1) * t)
    elif e.op == "duplicate":
        d = torch.tensor([p.get("dx", 0) * W, p.get("dy", 0) * H], device="cuda") * t
        new = _in_front(img, tri, to_world(uv + d))  # the copy covers whatever it lands on
        tri = torch.cat([tri, new])
        dc, rest, op = torch.cat([dc, dc[m]]), torch.cat([rest, rest[m]]), torch.cat([op, op[m] * min(1.0, t * 1.5)])
    elif e.op == "camera":
        pass  # handled by EditableImage.view
    elif e.op == "scatter":
        k = int(m.sum())
        gen = torch.Generator(device="cuda").manual_seed(int(p.get("seed", 0)))
        r = torch.randn(k, 3, device="cuda", generator=gen)
        speed = 0.5 + torch.rand(k, 1, device="cuda", generator=gen)
        z = torch.zeros(1, device="cuda")
        c = img._unproject(torch.tensor([[W / 2, H / 2]], device="cuda"), z)
        right = img._unproject(torch.tensor([[W / 2 + 1, H / 2]], device="cuda"), z) - c
        down = img._unproject(torch.tensor([[W / 2, H / 2 + 1]], device="cuda"), z) - c
        right, down = right / right.norm(), down / down.norm()
        n = img.towards_camera()
        span = img.plane_width()
        d = (right * r[:, :1] + down * r[:, 1:2]) + n * r[:, 2:].abs() * p.get("toward", 0.3)
        disp = d * speed * p.get("strength", 0.4) * span * t
        disp = disp + down * p.get("gravity", 0.0) * span * H / W * t * t
        tri[m] = verts + disp[:, None, :]
        op[m] = op[m] * (1 - p.get("fade", 0.0) * t)
    elif e.op == "shrink_splats":
        s = 1 + (p.get("factor", 0.2) - 1) * t
        v1 = verts[:, :1]
        tri[m] = v1 + (verts - v1) * s
    elif e.op == "fold":
        z = torch.zeros(1, device="cuda")
        pos = p.get("position", 0.5)
        if p.get("axis", "x") == "x":
            ends = torch.tensor([[pos * W, 0.0], [pos * W, float(H)]], device="cuda")
            coord = uv[:, 0, 0] - pos * W
        else:
            ends = torch.tensor([[0.0, pos * H], [float(W), pos * H]], device="cuda")
            coord = uv[:, 0, 1] - pos * H
        flap = coord > 0 if p.get("side", "after") == "after" else coord < 0
        if flap.any():
            a, b = img._unproject(ends, torch.zeros(2, device="cuda"))
            R = _axis_rotation((b - a).cpu(), p.get("degrees", 60) * t).cuda()
            fv = verts[flap]
            rot = (fv - a) @ R.T + a
            n = img.towards_camera()
            if ((rot - fv).reshape(-1, 3) @ n).mean() < 0:  # fold toward the viewer
                R = _axis_rotation((b - a).cpu(), -p.get("degrees", 60) * t).cuda()
                rot = (fv - a) @ R.T + a
            nv = verts.clone()
            nv[flap] = rot
            tri[m] = nv
    elif e.op == "ripple3d":
        c = torch.tensor([p["cx"] * W, p["cy"] * H], device="cuda")
        r = (uv - c).norm(dim=-1) / W                  # [k, 3] distance in widths
        wl, rad = p.get("wavelength", 0.06), p.get("radius", 0.3)
        h = torch.sin(2 * math.pi * r / wl - math.radians(p.get("phase", 0))) * torch.exp(-r / rad)
        tri[m] = verts + img.towards_camera() * (p.get("amplitude", 0.01) * img.plane_width() * t * h)[..., None]
    elif e.op == "flip":
        c = centre
        sgn = torch.tensor([-1.0, 1.0] if p.get("axis", "x") == "x" else [1.0, -1.0], device="cuda")
        f = 1 + (sgn - 1) * t
        tri[m] = to_world((uv - c) * f + c)
    else:
        raise ValueError(f"unknown edit {e.op!r}; available: {', '.join(OPS)}")
    if e.op in ("translate", "rotate", "scale", "flip") and e.selection != "all":
        # a moved part covers what it lands on instead of mixing with it
        tri[m] = _in_front(img, tri[~m], tri[m], t)
    return tri, dc, rest, op


def _in_front(img: EditableImage, others: torch.Tensor, part: torch.Tensor, t: float = 1.0) -> torch.Tensor:
    """`part` moved in depth (along each vertex's viewing ray, so it looks the same from the
    front) until it is in front of the `others` under its on-screen footprint.

    The fitted Gaussians are spread slightly in depth, so a part moved sideways would otherwise
    keep some of its Gaussians behind the ones it lands on and look half transparent. A high
    quantile, not the maximum, ignores the few stray Gaussians far off the plane."""
    s = torch.sign(img.towards_camera()[1])          # depth axis: +s*y is toward the camera
    uv = img._project(part)                          # [k, 3, 2]
    lo, hi = uv.reshape(-1, 2).amin(0), uv.reshape(-1, 2).amax(0)
    c = img._project(others[:, 0])
    under = ((c >= lo) & (c <= hi)).all(1)
    near = s * part[..., 1]
    if under.any():
        target = torch.quantile((s * others[under, 0, 1]).float(), 0.995)
        gap = (target - near.min()).clamp(min=0)
    else:
        gap = torch.zeros((), device=part.device)
    dy = (gap + 1e-3 * img.plane_width()) * t
    return img._unproject(uv, part[..., 1] + s * dy)


# ------------------------------------------------------------------ output helpers


def to_pil(img: torch.Tensor) -> Image.Image:
    return Image.fromarray((img.permute(1, 2, 0).cpu().numpy() * 255 + 0.5).astype(np.uint8))
