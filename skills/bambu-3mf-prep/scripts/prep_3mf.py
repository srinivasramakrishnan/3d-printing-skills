#!/usr/bin/env python3
"""
Bambu Lab X2D 3MF Inspector, Cleaner, Inventory Mapper & Headless Slice Validator.

Usage:
  python3 prep_3mf.py inspect <input.3mf>
  python3 prep_3mf.py prep <input.3mf> [--out <output.3mf>] [--process <preset>]
                                       [--bed <plate>] [--support keep|on|off]
                                       [--use-inventory-colors] [--no-slice]
  python3 prep_3mf.py inventory list
"""

from __future__ import annotations

import argparse
import colorsys
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from collections import defaultdict
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
PRINTER_PROFILE_PATH = SKILL_DIR / "config" / "printer_profile.json"
INVENTORY_PATH = SKILL_DIR / "config" / "filament_inventory.json"


def load_json(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def normalize_hex(h: str) -> str:
    h = (h or "#FFFFFF").strip().lstrip("#").upper()
    if len(h) == 8:
        h = h[:6]
    if len(h) != 6 or not re.fullmatch(r"[0-9A-F]{6}", h):
        return "#FFFFFF"
    return f"#{h}"


def hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = normalize_hex(h).lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def color_distance(h1: str, h2: str) -> float:
    """Hue-aware perceptual color distance (0..1000). Prevents matching chromatic colors to grays."""
    r1, g1, b1 = hex_to_rgb(h1)
    r2, g2, b2 = hex_to_rgb(h2)
    rmean = (r1 + r2) / 2.0
    dr, dg, db = r1 - r2, g1 - g2, b1 - b2
    rgb_dist = math.sqrt(
        (2 + rmean / 256.0) * dr * dr
        + 4 * dg * dg
        + (2 + (255 - rmean) / 256.0) * db * db
    )
    hue1, sat1, val1 = colorsys.rgb_to_hsv(r1 / 255.0, g1 / 255.0, b1 / 255.0)
    hue2, sat2, val2 = colorsys.rgb_to_hsv(r2 / 255.0, g2 / 255.0, b2 / 255.0)
    # Neutral vs chromatic mismatch penalty
    is_neutral1 = sat1 < 0.15 or val1 < 0.12
    is_neutral2 = sat2 < 0.15 or val2 < 0.12
    if is_neutral1 != is_neutral2:
        rgb_dist += 220.0
    elif not is_neutral1 and not is_neutral2:
        dh = min(abs(hue1 - hue2), 1.0 - abs(hue1 - hue2))
        rgb_dist += dh * 400.0
    return rgb_dist


def match_inventory(hex_color: str, material: str, inventory: dict) -> dict | None:
    spools = inventory.get("spools", [])
    if not spools:
        return None
    loaded = inventory.get("loaded", {})
    spool_to_slot = {v: k for k, v in loaded.items() if v}

    best = None
    best_score = 1e9
    best_raw_dist = 1e9
    for sp in spools:
        d = color_distance(hex_color, sp.get("hex", "#FFFFFF"))
        score = d
        # Slight penalty if material doesn't match (e.g. PLA vs PETG)
        if material and sp.get("material", "").upper() != material.upper():
            score += 120.0
        # Prefer spools currently on hand over in-transit or unconfirmed spools
        if sp.get("status") == "in_transit":
            score += 80.0
        elif sp.get("status") == "unconfirmed":
            score += 40.0
        if score < best_score:
            best_score = score
            best_raw_dist = d
            best = sp

    if best is None:
        return None
    quality = "exact" if best_raw_dist < 45 else ("close" if best_raw_dist < 130 else "distant")
    return {
        "spool": best,
        "distance": round(best_raw_dist, 1),
        "match_quality": quality,
        "loaded_slot": spool_to_slot.get(best["id"]),
    }


def is_ascii_clean(name: str) -> bool:
    return bool(name) and all(32 <= ord(c) < 127 for c in name)


def sanitize_name(name: str, fallback: str) -> str:
    if is_ascii_clean(name):
        return name
    # Keep any ASCII alphanumeric/dot/underscore/dash suffix like .053
    ascii_part = re.sub(r"[^A-Za-z0-9._-]+", "", name).strip("._-")
    if ascii_part and not ascii_part[0].isdigit():
        return ascii_part
    if ascii_part:
        return f"{fallback}_{ascii_part}"
    return fallback


def resolve_filament_preset_file(
    source_preset: str,
    source_type: str,
    matched_spool: dict | None,
    profile: dict,
) -> tuple[str, Path]:
    """Return (preset_name, preset_json_path) guaranteed to exist for X2D 0.4 nozzle."""
    fil_dir = Path(profile["bambu_profiles_dir"]) / "filament"
    candidates = []

    if matched_spool and matched_spool.get("status") == "owned" and matched_spool.get("preset"):
        candidates.append(matched_spool["preset"])

    # Convert e.g. 'Bambu PLA Matte @BBL A1' -> 'Bambu PLA Matte @BBL X2D 0.4 nozzle'
    if source_preset:
        base = source_preset.split("@")[0].strip()
        candidates.append(f"{base} @BBL X2D 0.4 nozzle")
        candidates.append(f"{base} @BBL X2D")

    if source_type:
        t = source_type.upper()
        if "PETG" in t:
            candidates.append("Bambu PETG HF @BBL X2D 0.4 nozzle")
            candidates.append("Generic PETG @BBL X2D 0.4 nozzle")
        elif "ABS" in t:
            candidates.append("Bambu ABS @BBL X2D 0.4 nozzle")
        elif "TPU" in t:
            candidates.append("Generic TPU @BBL X2D 0.4 nozzle")
        else:
            candidates.append("Bambu PLA Basic @BBL X2D 0.4 nozzle")

    candidates.append(profile["default_filament_preset"])

    for name in candidates:
        p = fil_dir / f"{name}.json"
        if p.exists():
            return name, p

    default_name = profile["default_filament_preset"]
    return default_name, fil_dir / f"{default_name}.json"


def resolve_process_preset(source_cfg: dict, override: str | None, profile: dict) -> tuple[str, Path]:
    proc_dir = Path(profile["bambu_profiles_dir"]) / "process"
    if override:
        p = proc_dir / f"{override}.json"
        if not p.exists():
            raise FileNotFoundError(f"Process preset not found: {p}")
        return override, p

    lh = str(source_cfg.get("layer_height", "0.16")).strip()
    lh_map = profile.get("layer_height_to_process", {})
    # Match nearest numeric layer height
    chosen = profile["default_process_preset"]
    try:
        lh_val = float(lh)
        best_k = min(lh_map.keys(), key=lambda k: abs(float(k) - lh_val))
        if abs(float(best_k) - lh_val) <= 0.03:
            chosen = lh_map[best_k]
    except ValueError:
        pass

    p = proc_dir / f"{chosen}.json"
    if p.exists():
        return chosen, p
    fallback = profile["default_process_preset"]
    return fallback, proc_dir / f"{fallback}.json"


def inspect_3mf(path: Path, profile: dict, inventory: dict) -> dict:
    info = {
        "file": str(path),
        "printer_model": None,
        "printer_settings_id": None,
        "print_settings_id": None,
        "curr_bed_type": None,
        "layer_height": None,
        "enable_support": None,
        "support_type": None,
        "brim_type": None,
        "sparse_infill_density": None,
        "wall_loops": None,
        "enable_prime_tower": None,
        "filaments": [],
        "objects": [],
        "mismatches": [],
        "geometry": {},
    }

    with zipfile.ZipFile(path, "r") as z:
        names = z.namelist()
        cfg = {}
        if "Metadata/project_settings.config" in names:
            cfg = json.loads(z.read("Metadata/project_settings.config"))
            for k in [
                "printer_model", "printer_settings_id", "print_settings_id",
                "curr_bed_type", "layer_height", "enable_support", "support_type",
                "brim_type", "sparse_infill_density", "wall_loops", "enable_prime_tower",
            ]:
                info[k] = cfg.get(k)

            colours = cfg.get("filament_colour", [])
            types = cfg.get("filament_type", [])
            presets = cfg.get("filament_settings_id", [])
            vendors = cfg.get("filament_vendor", [])
            n_fil = max(len(colours), len(presets), len(types))
            for i in range(n_fil):
                hx = normalize_hex(colours[i] if i < len(colours) else "#FFFFFF")
                ft = types[i] if i < len(types) else "PLA"
                fp = presets[i] if i < len(presets) else ""
                fv = vendors[i] if i < len(vendors) else ""
                inv_match = match_inventory(hx, ft, inventory)
                info["filaments"].append({
                    "slot": i + 1,
                    "hex": hx,
                    "type": ft,
                    "vendor": fv,
                    "source_preset": fp,
                    "inventory_match": inv_match,
                })

        if "Metadata/model_settings.config" in names:
            mroot = ET.fromstring(z.read("Metadata/model_settings.config"))
            for obj in mroot.findall("object"):
                oid = obj.attrib.get("id")
                oname = ""
                for md in obj.findall("metadata"):
                    if md.attrib.get("key") == "name":
                        oname = md.attrib.get("value", "")
                info["objects"].append({
                    "id": oid,
                    "name": oname,
                    "non_ascii": not is_ascii_clean(oname),
                })

        # Quick geometry bounding box check across 3D/Objects/*.model (or 3D/3dmodel.model)
        model_files = [n for n in names if n.startswith("3D/Objects/") and n.endswith(".model")]
        if not model_files and "3D/3dmodel.model" in names:
            model_files = ["3D/3dmodel.model"]

        min_xyz = [float("inf")] * 3
        max_xyz = [float("-inf")] * 3
        vert_count = 0
        tri_count = 0
        for mf in model_files:
            data = z.read(mf).decode("utf-8", errors="ignore")
            for m in re.finditer(r'<vertex\s+x="([^"]+)"\s+y="([^"]+)"\s+z="([^"]+)"', data):
                x, y, zc = float(m.group(1)), float(m.group(2)), float(m.group(3))
                if x < min_xyz[0]: min_xyz[0] = x
                if y < min_xyz[1]: min_xyz[1] = y
                if zc < min_xyz[2]: min_xyz[2] = zc
                if x > max_xyz[0]: max_xyz[0] = x
                if y > max_xyz[1]: max_xyz[1] = y
                if zc > max_xyz[2]: max_xyz[2] = zc
                vert_count += 1
            tri_count += data.count("<triangle ")

        if vert_count > 0:
            dims = [round(max_xyz[i] - min_xyz[i], 2) for i in range(3)]
            bv = profile["build_volume_mm"]
            fits = dims[0] <= bv["x"] and dims[1] <= bv["y"] and dims[2] <= bv["z"]
            info["geometry"] = {
                "vertices": vert_count,
                "triangles": tri_count,
                "dimensions_mm": {"x": dims[0], "y": dims[1], "z": dims[2]},
                "fits_build_volume": fits,
            }
            if not fits:
                info["mismatches"].append(
                    f"Model size {dims[0]}x{dims[1]}x{dims[2]}mm exceeds X2D build volume {bv['x']}x{bv['y']}x{bv['z']}mm"
                )

    # Check mismatches against X2D profile
    if info["printer_model"] != profile["printer_name"]:
        info["mismatches"].append(
            f"Printer mismatch: '{info['printer_model']}' -> '{profile['printer_name']}'"
        )
    if info["curr_bed_type"] != profile["default_bed_type"]:
        info["mismatches"].append(
            f"Build plate mismatch: '{info['curr_bed_type']}' -> '{profile['default_bed_type']}'"
        )
    if any(o["non_ascii"] for o in info["objects"]):
        bad = [o["name"] for o in info["objects"] if o["non_ascii"]]
        info["mismatches"].append(f"Non-ASCII object name(s) that render as '???': {bad}")
    if any("X2D" not in (f["source_preset"] or "") for f in info["filaments"]):
        info["mismatches"].append("Filament presets are not targeting Bambu Lab X2D 0.4 nozzle")

    return info


def sanitize_3mf_names(src_path: Path, dst_path: Path, stem: str) -> list[tuple[str, str]]:
    """Copy 3MF and replace any non-ASCII object/part names in Metadata/model_settings.config."""
    renames = []
    with zipfile.ZipFile(src_path, "r") as zin, zipfile.ZipFile(dst_path, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "Metadata/model_settings.config":
                root = ET.fromstring(data)
                idx = 1
                for elem in root.iter():
                    if elem.tag in ("object", "part"):
                        for md in elem.findall("metadata"):
                            if md.attrib.get("key") == "name":
                                old = md.attrib.get("value", "")
                                if not is_ascii_clean(old):
                                    new = sanitize_name(old, f"{stem}_obj{idx}")
                                    md.attrib["value"] = new
                                    if (old, new) not in renames:
                                        renames.append((old, new))
                                    idx += 1
                data = ET.tostring(root, encoding="utf-8", xml_declaration=True)
            zout.writestr(item, data)
    return renames


def patch_3mf_project_settings(
    path: Path,
    source_cfg: dict,
    profile: dict,
    filament_colours: list[str],
    bed_type: str,
    support_mode: str,
) -> None:
    """Apply final X2D hardware overrides and preserve important author print settings."""
    with tempfile.NamedTemporaryFile(suffix=".3mf", delete=False) as tf:
        tmp_path = Path(tf.name)
    try:
        with zipfile.ZipFile(path, "r") as zin, zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                data = zin.read(item.filename)
                if item.filename == "Metadata/project_settings.config":
                    cfg = json.loads(data)
                    cfg["curr_bed_type"] = bed_type
                    cfg["extruder_ams_count"] = profile["extruder_ams_count"]
                    if filament_colours:
                        cfg["filament_colour"] = filament_colours
                    # Preserve key author print settings unless explicitly overridden
                    for keep_key in ["sparse_infill_density", "wall_loops", "brim_type", "enable_prime_tower", "support_type"]:
                        if keep_key in source_cfg and source_cfg[keep_key] is not None:
                            cfg[keep_key] = source_cfg[keep_key]
                    if support_mode == "on":
                        cfg["enable_support"] = "1"
                    elif support_mode == "critical":
                        cfg["enable_support"] = "1"
                        cfg["support_type"] = "tree(auto)"
                        cfg["support_on_build_plate_only"] = "1"
                        cfg["support_critical_regions_only"] = "1"
                        cfg["support_threshold_angle"] = "30"
                    elif support_mode == "off":
                        cfg["enable_support"] = "0"
                    elif "enable_support" in source_cfg:
                        cfg["enable_support"] = source_cfg["enable_support"]
                    data = json.dumps(cfg, indent=4).encode("utf-8")
                zout.writestr(item, data)
        shutil.move(str(tmp_path), str(path))
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def run_slice_validation(target_3mf: Path, profile: dict) -> dict:
    """Run headless Bambu Studio CLI slice to catch warnings, time, and filament usage."""
    bs_bin = profile["bambu_studio_bin"]
    result = {
        "success": False,
        "warnings": [],
        "errors": [],
        "prediction_seconds": None,
        "prediction_human": None,
        "total_weight_g": None,
        "support_used": None,
        "filaments": [],
        "dual_nozzle_recommendation": None,
    }

    with tempfile.TemporaryDirectory(prefix="bs_slice_") as tmpdir:
        cmd = [
            bs_bin,
            "--slice", "0",
            "--debug", "2",
            "--outputdir", tmpdir,
            "--export-3mf", "sliced_check.3mf",
            str(target_3mf),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        combined = proc.stdout + "\n" + proc.stderr

        for line in combined.splitlines():
            if "slicing warnings:" in line:
                msg = line.split("slicing warnings:", 1)[1].strip()
                result["warnings"].append(msg)
            elif "[error]" in line and "the parent path" not in line:
                result["errors"].append(line.strip())

        sliced_3mf = Path(tmpdir) / "sliced_check.3mf"
        if sliced_3mf.exists():
            result["success"] = True
            with zipfile.ZipFile(sliced_3mf, "r") as z:
                if "Metadata/slice_info.config" in z.namelist():
                    sroot = ET.fromstring(z.read("Metadata/slice_info.config"))
                    plate = sroot.find("plate")
                    if plate is not None:
                        meta = {m.attrib["key"]: m.attrib["value"] for m in plate.findall("metadata")}
                        if "prediction" in meta:
                            sec = int(float(meta["prediction"]))
                            result["prediction_seconds"] = sec
                            hrs, rem = divmod(sec, 3600)
                            mins = rem // 60
                            result["prediction_human"] = f"{hrs}h {mins:02d}m"
                        if "weight" in meta:
                            result["total_weight_g"] = float(meta["weight"])
                        if "support_used" in meta:
                            result["support_used"] = meta["support_used"] == "true"

                        for fil in plate.findall("filament"):
                            result["filaments"].append({
                                "id": int(fil.attrib.get("id", "0")),
                                "type": fil.attrib.get("type", ""),
                                "color": normalize_hex(fil.attrib.get("color", "#FFFFFF")),
                                "used_g": float(fil.attrib.get("used_g", "0")),
                                "used_m": float(fil.attrib.get("used_m", "0")),
                            })

                        # X2D dual-nozzle grouping recommendation:
                        # Put the single heaviest filament on Auxiliary Nozzle (no AMS)
                        # and up to 4 remaining filaments on Main Nozzle (AMS).
                        used_fils = [f for f in result["filaments"] if f["used_g"] > 0]
                        if len(used_fils) >= 2:
                            heaviest = max(used_fils, key=lambda f: f["used_g"])
                            ams_fils = [f for f in used_fils if f["id"] != heaviest["id"]]
                            result["dual_nozzle_recommendation"] = {
                                "auxiliary_nozzle": heaviest,
                                "main_nozzle_ams": ams_fils,
                                "fits_without_manual_swap": len(ams_fils) <= 4,
                            }
    return result


def print_report(inspection: dict, prep_meta: dict | None = None, slice_res: dict | None = None) -> None:
    print("=" * 70)
    print(f"3MF REPORT: {Path(inspection['file']).name}")
    print("=" * 70)
    print(f"  Source Printer : {inspection['printer_model']} ({inspection['printer_settings_id']})")
    print(f"  Source Process : {inspection['print_settings_id']} (layer={inspection['layer_height']}mm, support={inspection['enable_support']})")
    print(f"  Source Plate   : {inspection['curr_bed_type']}")
    geom = inspection.get("geometry", {})
    if geom:
        d = geom["dimensions_mm"]
        print(f"  Dimensions     : {d['x']} x {d['y']} x {d['z']} mm ({geom['triangles']:,} tris) | Fits X2D: {geom['fits_build_volume']}")

    if inspection["mismatches"]:
        print("\n[!] Mismatches Found:")
        for m in inspection["mismatches"]:
            print(f"    - {m}")

    print("\nFilaments & Inventory Match:")
    for f in inspection["filaments"]:
        m = f["inventory_match"]
        if m:
            sp = m["spool"]
            slot_str = f"LOADED in {m['loaded_slot']}" if m["loaded_slot"] else "not loaded"
            status_tag = f" [{sp['status']}]" if sp.get("status") and sp.get("status") != "owned" else ""
            match_str = f"-> {sp['brand']} {sp['material']} {sp['finish']} '{sp['color_name']}' ({sp['hex']}, {m['match_quality']}, {slot_str}){status_tag}"
        else:
            match_str = "-> NO INVENTORY MATCH"
        print(f"  #{f['slot']}: {f['hex']} ({f['type']}) [{f['source_preset']}] {match_str}")

    if prep_meta:
        print("\n[+] Prepared X2D File:")
        print(f"    Output   : {prep_meta['output_path']}")
        print(f"    Process  : {prep_meta['process_preset']}")
        print(f"    Plate    : {prep_meta['bed_type']}")
        print(f"    Filaments: {prep_meta['filament_presets']}")
        if prep_meta["renames"]:
            print(f"    Renamed  : {prep_meta['renames']}")

    if slice_res:
        print("\n[*] Headless Bambu Studio Slice Check:")
        if slice_res["success"]:
            print(f"    Est. Time   : {slice_res['prediction_human']} | Model Weight: {slice_res['total_weight_g']}g | Supports: {slice_res['support_used']}")
            for sf in slice_res["filaments"]:
                print(f"      Slot #{sf['id']} ({sf['color']} {sf['type']}): {sf['used_g']}g ({sf['used_m']}m)")
            rec = slice_res.get("dual_nozzle_recommendation")
            if rec:
                aux = rec["auxiliary_nozzle"]
                ams = ", ".join(f"#{x['id']} ({x['color']}, {x['used_g']}g)" for x in rec["main_nozzle_ams"])
                print("    X2D Dual-Nozzle Grouping Suggestion:")
                print(f"      Auxiliary Nozzle (1 spool) : #{aux['id']} ({aux['color']}, {aux['used_g']}g)")
                print(f"      Main Nozzle AMS (<=4)      : {ams}")
        else:
            print("    Slice check failed!")
        if slice_res["warnings"]:
            print("    [!] Slicer Warnings:")
            for w in slice_res["warnings"]:
                print(f"        - {w}")
        if slice_res["errors"]:
            print("    [!] Slicer Errors:")
            for e in slice_res["errors"]:
                print(f"        - {e}")
    print("=" * 70)


def cmd_prep(args, profile: dict, inventory: dict) -> int:
    src = Path(args.input).resolve()
    if not src.exists():
        print(f"Error: File not found: {src}", file=sys.stderr)
        return 1

    inspection = inspect_3mf(src, profile, inventory)
    with zipfile.ZipFile(src, "r") as z:
        source_cfg = json.loads(z.read("Metadata/project_settings.config")) if "Metadata/project_settings.config" in z.namelist() else {}

    out_path = Path(args.out).resolve() if args.out else src.with_name(f"{src.stem}{profile['output_suffix']}.3mf")
    bed_type = args.bed or profile["default_bed_type"]
    process_name, process_path = resolve_process_preset(source_cfg, args.process, profile)
    machine_path = Path(profile["bambu_profiles_dir"]) / "machine" / profile["machine_preset_file"]

    filament_presets = []
    filament_paths = []
    filament_colours = []
    for f in inspection["filaments"]:
        m = f["inventory_match"]
        matched_spool = m["spool"] if m else None
        pname, ppath = resolve_filament_preset_file(f["source_preset"], f["type"], matched_spool, profile)
        filament_presets.append(pname)
        filament_paths.append(str(ppath))
        if args.use_inventory_colors and matched_spool and m["match_quality"] in ("exact", "close"):
            filament_colours.append(normalize_hex(matched_spool["hex"]))
        else:
            filament_colours.append(f["hex"])

    if not filament_paths:
        default_name = profile["default_filament_preset"]
        filament_presets = [default_name]
        filament_paths = [str(Path(profile["bambu_profiles_dir"]) / "filament" / f"{default_name}.json")]

    with tempfile.TemporaryDirectory(prefix="bs_prep_") as tmpdir:
        sanitized_in = Path(tmpdir) / "sanitized_in.3mf"
        renames = sanitize_3mf_names(src, sanitized_in, re.sub(r"[^A-Za-z0-9_-]+", "_", src.stem))

        cmd = [
            profile["bambu_studio_bin"],
            "--load-settings", f"{machine_path};{process_path}",
            "--load-filaments", ";".join(filament_paths),
            "--debug", "2",
            "--outputdir", str(out_path.parent),
            "--export-3mf", out_path.name,
            str(sanitized_in),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if proc.returncode != 0 or not out_path.exists():
            print("Error running Bambu Studio CLI conversion:", file=sys.stderr)
            print(proc.stdout + "\n" + proc.stderr, file=sys.stderr)
            return 1

    patch_3mf_project_settings(
        out_path,
        source_cfg=source_cfg,
        profile=profile,
        filament_colours=filament_colours,
        bed_type=bed_type,
        support_mode=args.support,
    )

    slice_res = None if args.no_slice else run_slice_validation(out_path, profile)
    prep_meta = {
        "output_path": str(out_path),
        "process_preset": process_name,
        "bed_type": bed_type,
        "filament_presets": filament_presets,
        "renames": renames,
    }
    print_report(inspection, prep_meta=prep_meta, slice_res=slice_res)
    return 0


def cmd_inventory(args, inventory: dict) -> int:
    if args.inv_action == "list":
        loaded = inventory.get("loaded", {})
        print("=== CURRENTLY LOADED ===")
        for slot, sid in loaded.items():
            print(f"  {slot:12s}: {sid or '(empty)'}")
        print("\n=== FILAMENT SPOOLS ===")
        for sp in inventory.get("spools", []):
            tag = f" [{sp['status']}]" if sp.get("status") else ""
            print(f"  - {sp['id']}: {sp['brand']} {sp['material']} {sp['finish']} '{sp['color_name']}' ({sp['hex']}) -> {sp['preset']}{tag}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Bambu Lab X2D 3MF Prep & Validation Tool")
    sub = parser.add_subparsers(dest="command", required=True)

    p_ins = sub.add_parser("inspect", help="Inspect a 3MF without modifying it")
    p_ins.add_argument("input", help="Path to input .3mf file")

    p_prep = sub.add_parser("prep", help="Clean, re-target to X2D, map inventory, and slice-validate a 3MF")
    p_prep.add_argument("input", help="Path to input .3mf file")
    p_prep.add_argument("--out", help="Output .3mf path (default: <stem>_x2d_ready.3mf)")
    p_prep.add_argument("--process", help="Override X2D process preset name (e.g. '0.16mm High Quality @BBL X2D')")
    p_prep.add_argument("--bed", help="Override build plate (default: Textured PEI Plate)")
    p_prep.add_argument("--support", choices=["keep", "on", "critical", "off"], default="keep", help="Support setting policy ('critical' = tree auto, build-plate only, critical regions only)")
    p_prep.add_argument("--use-inventory-colors", action="store_true", help="Replace 3MF hex colors with matched inventory hex colors")
    p_prep.add_argument("--no-slice", action="store_true", help="Skip headless Bambu Studio slice validation")

    p_inv = sub.add_parser("inventory", help="Manage filament inventory")
    p_inv.add_argument("inv_action", choices=["list"])

    args = parser.parse_args()
    profile = load_json(PRINTER_PROFILE_PATH)
    inventory = load_json(INVENTORY_PATH)

    if args.command == "inspect":
        info = inspect_3mf(Path(args.input).resolve(), profile, inventory)
        print_report(info)
        return 0
    if args.command == "prep":
        return cmd_prep(args, profile, inventory)
    if args.command == "inventory":
        return cmd_inventory(args, inventory)
    return 1


if __name__ == "__main__":
    sys.exit(main())
