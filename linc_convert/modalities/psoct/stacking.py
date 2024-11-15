"""
Converts Matlab files generated by the MGH in-house OCT pipeline
into a OME-ZARR pyramid.
"""

import ast
import itertools
import json
import math
import os
import re
from contextlib import contextmanager
from functools import wraps
from itertools import product
from typing import Optional, List, Literal
from warnings import warn

import cyclopts
import h5py
import nibabel as nib
import numpy as np
import zarr
from scipy.io import loadmat

from linc_convert.modalities.psoct.cli import psoct
from linc_convert.utils.math import ceildiv
from linc_convert.utils.orientation import orientation_to_affine, center_affine
from linc_convert.utils.unit import convert_unit, to_ome_unit, to_nifti_unit
from linc_convert.utils.zarr import make_compressor

stacking = cyclopts.App(name="stacking", help_format="markdown")
psoct.command(stacking)


def automap(func):
    """Decorator to automatically map the array in the mat file"""

    @wraps(func)
    def wrapper(inp, out=None, **kwargs):
        if out is None:
            out = os.path.splitext(inp[0])[0]
            out += ".nii.zarr" if kwargs.get("nii", False) else ".ome.zarr"
        kwargs["nii"] = kwargs.get("nii", False) or out.endswith(".nii.zarr")
        with mapmat(inp, kwargs.get("key", None)) as dat:
            return func(dat, out, **kwargs)

    return wrapper


@stacking.default
@automap
def convert(
        inp: List[str],
        out: Optional[str] = None,
        *,
        key: Optional[str] = None,
        meta: str = None,
        chunk: int = 128,
        compressor: str = "blosc",
        compressor_opt: str = "{}",
        max_load: int = 128,
        max_levels: int = 5,
        no_pool: Optional[int] = None,
        nii: bool = False,
        orientation: str = "RAS",
        center: bool = True,
) -> None:
    """
    This command converts OCT volumes stored in raw matlab files
    into a pyramidal OME-ZARR (or NIfTI-Zarr) hierarchy.

    Parameters
    ----------
    inp
        Path to the input mat file
    out
        Path to the output Zarr directory [<INP>.ome.zarr]
    key
        Key of the array to be extracted, default to first key found
    meta
        Path to the metadata file
    chunk
        Output chunk size
    compressor : {blosc, zlib, raw}
        Compression method
    compressor_opt
        Compression options
    max_load
        Maximum input chunk size
    max_levels
        Maximum number of pyramid levels
    no_pool
        Index of dimension to not pool when building pyramid
    nii
        Convert to nifti-zarr. True if path ends in ".nii.zarr"
    orientation
        Orientation of the volume
    center
        Set RAS[0, 0, 0] at FOV center
    """

    if isinstance(compressor_opt, str):
        compressor_opt = ast.literal_eval(compressor_opt)

    # Write OME-Zarr multiscale metadata
    if meta:
        print("Write JSON")
        with open(meta, "r") as f:
            meta_txt = f.read()
            meta_json = make_json(meta_txt)
        path_json = ".".join(out.split(".")[:-2]) + ".json"
        with open(path_json, "w") as f:
            json.dump(meta_json, f, indent=4)
        vx = meta_json["PixelSize"]
        unit = meta_json["PixelSizeUnits"]
    else:
        vx = [1] * 3
        unit = "um"

    # Prepare Zarr group
    omz = zarr.storage.DirectoryStore(out)
    omz = zarr.group(store=omz, overwrite=True)

    if not hasattr(inp, "dtype"):
        raise Exception("Input is not a numpy array. This is likely unexpected")
    if len(inp.shape) < 3:
        raise Exception("Input array is not 3d")
    # Prepare chunking options
    opt = {
        "dimension_separator": r"/",
        "order": "F",
        "dtype": np.dtype(inp.dtype).str,
        "fill_value": None,
        "compressor": make_compressor(compressor, **compressor_opt),
    }

    inp_chunk = [min(x, max_load) for x in inp.shape]
    nk = ceildiv(inp.shape[0], inp_chunk[0])
    nj = ceildiv(inp.shape[1], inp_chunk[1])
    ni = ceildiv(inp.shape[2], inp_chunk[2])

    nblevels = min(
        [int(math.ceil(math.log2(x))) for i, x in enumerate(inp.shape) if i != no_pool]
    )
    nblevels = min(nblevels, int(math.ceil(math.log2(max_load))))
    nblevels = min(nblevels, max_levels)

    opt["chunks"] = [min(x, chunk) for x in inp.shape]

    omz.create_dataset(str(0), shape=inp.shape, **opt)

    # iterate across input chunks
    for i, j, k in product(range(ni), range(nj), range(nk)):
        loaded_chunk = inp[
                       k * inp_chunk[0]: (k + 1) * inp_chunk[0],
                       j * inp_chunk[1]: (j + 1) * inp_chunk[1],
                       i * inp_chunk[2]: (i + 1) * inp_chunk[2],
                       ]

        print(
            f"[{i + 1:03d}, {j + 1:03d}, {k + 1:03d}]",
            "/",
            f"[{ni:03d}, {nj:03d}, {nk:03d}]",
            # f"({1 + level}/{nblevels})",
            end="\r",
        )

        # save current chunk
        omz["0"][
        k * inp_chunk[0]: k * inp_chunk[0] + loaded_chunk.shape[0],
        j * inp_chunk[1]: j * inp_chunk[1] + loaded_chunk.shape[1],
        i * inp_chunk[2]: i * inp_chunk[2] + loaded_chunk.shape[2],
        ] = loaded_chunk
    # TODO: no_pool is ignored for now, should add back
    generate_pyramid(omz, nblevels - 1, mode="mean")

    print("")

    # Write OME-Zarr multiscale metadata
    print("Write metadata")
    print(unit)
    ome_unit = to_ome_unit(unit)
    multiscales = [
        {
            "version": "0.4",
            "axes": [
                {"name": "z", "type": "space", "unit": ome_unit},
                {"name": "y", "type": "space", "unit": ome_unit},
                {"name": "x", "type": "space", "unit": ome_unit},
            ],
            "datasets": [],
            "type": ("2x2x2" if no_pool is None else "2x2") + "mean window",
            "name": "",
        }
    ]

    for n in range(nblevels):
        multiscales[0]["datasets"].append({})
        level = multiscales[0]["datasets"][-1]
        level["path"] = str(n)

        # With a moving window, the scaling factor is exactly 2, and
        # the edges of the top-left voxel are aligned
        level["coordinateTransformations"] = [
            {
                "type": "scale",
                "scale": [
                    (1 if no_pool == 0 else 2 ** n) * vx[0],
                    (1 if no_pool == 1 else 2 ** n) * vx[1],
                    (1 if no_pool == 2 else 2 ** n) * vx[2],
                ],
            },
            {
                "type": "translation",
                "translation": [
                    (0 if no_pool == 0 else (2 ** n - 1)) * vx[0] * 0.5,
                    (0 if no_pool == 1 else (2 ** n - 1)) * vx[1] * 0.5,
                    (0 if no_pool == 2 else (2 ** n - 1)) * vx[2] * 0.5,
                ],
            },
        ]
    multiscales[0]["coordinateTransformations"] = [
        {"scale": [1.0] * 3, "type": "scale"}
    ]
    omz.attrs["multiscales"] = multiscales

    if not nii:
        print("done.")
        return

    # Write NIfTI-Zarr header
    # NOTE: we use nifti2 because dimensions typically do not fit in a short
    # TODO: we do not write the json zattrs, but it should be added in
    #       once the nifti-zarr package is released
    shape = list(reversed(omz["0"].shape))
    affine = orientation_to_affine(orientation, *vx[::-1])
    if center:
        affine = center_affine(affine, shape[:3])
    niftizarr_write_header(omz,shape,affine,omz["0"].dtype,to_nifti_unit(unit),nifti_version=2)
    # header = nib.Nifti2Header()
    # header.set_data_shape(shape)
    # header.set_data_dtype(omz["0"].dtype)
    # header.set_qform(affine)
    # header.set_sform(affine)
    # header.set_xyzt_units(nib.nifti1.unit_codes.code[to_nifti_unit(unit)])
    # header.structarr["magic"] = b"nz2\0"
    # header = np.frombuffer(header.structarr.tobytes(), dtype="u1")
    # opt = {
    #     "chunks": [len(header)],
    #     "dimension_separator": r"/",
    #     "order": "F",
    #     "dtype": "|u1",
    #     "fill_value": None,
    #     "compressor": None,
    # }
    # omz.create_dataset("nifti", data=header, shape=len(header), **opt)
    # print("done.")


@contextmanager
def mapmat(fnames, key=None):
    """Load or memory-map an array stored in a .mat file"""
    loaded_data = []

    for fname in fnames:
        try:
            # "New" .mat file
            f = h5py.File(fname, "r")
        except Exception:
            # "Old" .mat file
            f = loadmat(fname)

        if key is None:
            if not len(f.keys()):
                raise Exception(f"{fname} is empty")
            key = list(f.keys())[0]
            if len(f.keys()) > 1:
                warn(
                    f'More than one key in .mat file {fname}, arbitrarily loading "{key}"'
                )

        if key not in f.keys():
            raise Exception(f"Key {key} not found in file {fname}")

        if len(fnames) == 1:
            yield f.get(key)
            if hasattr(f, "close"):
                f.close()
            break
        loaded_data.append(f.get(key))

    yield np.stack(loaded_data, axis=-1)


def make_json(oct_meta):
    """
    Expected input:
    ---------------
    Image medium: 60% TDE
    Center Wavelength: 1294.84nm
    Axial resolution: 4.9um
    Lateral resolution: 4.92um
    FOV: 3x3mm
    Voxel size: 3x3x3um
    Depth focus range: 225um
    Number of focuses: 2
    Focus #: 2
    Slice thickness: 450um.
    Number of slices: 75
    Slice #:23
    Modality: dBI
    """

    def parse_value_unit(string, n=None):
        number = r"-?(\d+\.?\d*|\d*\.?\d+)(E-?\d+)?"
        value = "x".join([number] * (n or 1))
        match = re.fullmatch(r"(?P<value>" + value + r")(?P<unit>\w*)", string)
        value, unit = match.group("value"), match.group("unit")
        value = list(map(float, value.split("x")))
        if n is None:
            value = value[0]
        return value, unit

    meta = {
        "BodyPart": "BRAIN",
        "Environment": "exvivo",
        "SampleStaining": "none",
    }

    for line in oct_meta.split("\n"):
        if ":" not in line:
            continue

        key, value = line.split(":")
        key, value = key.strip(), value.strip()

        if key == "Image medium":
            parts = value.split()
            if "TDE" in parts:
                parts[parts.index("TDE")] = "2,2' Thiodiethanol (TDE)"
            meta["SampleMedium"] = " ".join(parts)

        elif key == "Center Wavelength":
            value, unit = parse_value_unit(value)
            meta["Wavelength"] = value
            meta["WavelengthUnit"] = unit

        elif key == "Axial resolution":
            value, unit = parse_value_unit(value)
            meta["ResolutionAxial"] = value
            meta["ResolutionAxialUnit"] = unit

        elif key == "Lateral resolution":
            value, unit = parse_value_unit(value)
            meta["ResolutionLateral"] = value
            meta["ResolutionLateralUnit"] = unit

        elif key == "Voxel size":
            value, unit = parse_value_unit(value, n=3)
            meta["PixelSize"] = value
            meta["PixelSizeUnits"] = unit

        elif key == "Depth focus range":
            value, unit = parse_value_unit(value)
            meta["DepthFocusRange"] = value
            meta["DepthFocusRangeUnit"] = unit

        elif key == "Number of focuses":
            value, unit = parse_value_unit(value)
            meta["FocusCount"] = int(value)

        elif key == "Slice thickness":
            value, unit = parse_value_unit(value)
            unit = convert_unit(value, unit[:-1], "u")
            meta["SliceThickness"] = value

        elif key == "Number of slices":
            value, unit = parse_value_unit(value)
            meta["SliceCount"] = int(value)

        elif key == "Modality":
            meta["OCTModality"] = value

        else:
            continue

    return meta




def generate_pyramid(
        omz,
        levels: int | None = None,
        ndim: int = 3,
        max_load: int = 512,
        mode: Literal["mean", "median"] = "median",
) -> list[list[int]]:
    """
    Generate the levels of a pyramid in an existing Zarr.

    Parameters
    ----------
    path : PathLike | str
        Path to parent Zarr
    levels : int
        Number of additional levels to generate.
        By default, stop when all dimensions are smaller than their
        corresponding chunk size.
    shard : list[int] | bool | {"auto"} | None
        Shard size.
        * If `None`, use same shard size as the input array;
        * If `False`, no dot use sharding;
        * If `True` or `"auto"`, automatically find shard size;
        * Otherwise, use provided shard size.
    ndim : int
        Number of spatial dimensions.
    max_load : int
        Maximum number of voxels to load along each dimension.
    mode : {"mean", "median"}
        Whether to use a mean or median moving window.

    Returns
    -------
    shapes : list[list[int]]
        Shapes of all levels, from finest to coarsest, including the
        existing top level.
    """

    shape = list(omz["0"].shape)
    chunk_size = omz["0"].chunks

    # Select windowing function
    if mode == "median":
        func = np.median
    else:
        assert mode == "mean"
        func = np.mean

    level = 0
    batch, shape = shape[:-ndim], shape[-ndim:]
    allshapes = [shape]

    opt = {
        "dimension_separator": omz["0"]._dimension_separator,
        "order": omz["0"]._order,
        "dtype": omz["0"]._dtype,
        "fill_value": omz["0"]._fill_value,
        "compressor": omz["0"]._compressor,
    }

    while True:
        level += 1

        # Compute downsampled shape
        prev_shape, shape = shape, [max(1, x // 2) for x in shape]

        # Stop if seen enough levels or level shape smaller than chunk size
        if levels is None:
            if all(x <= c for x, c in zip(shape, chunk_size[-ndim:])):
                break
        elif level > levels:
            break

        print("Compute level", level, "with shape", shape)

        allshapes.append(shape)
        omz.create_dataset(str(level), shape=shape, **opt)

        # Iterate across `max_load` chunks
        # (note that these are unrelared to underlying zarr chunks)
        grid_shape = [ceildiv(n, max_load) for n in prev_shape]
        for chunk_index in itertools.product(*[range(x) for x in grid_shape]):
            print(f"chunk {chunk_index} / {tuple(grid_shape)})", end="\r")

            # Read one chunk of data at the previous resolution
            slicer = [Ellipsis] + [
                slice(i * max_load, min((i + 1) * max_load, n))
                for i, n in zip(chunk_index, prev_shape)
            ]
            dat = omz[str(level - 1)][tuple(slicer)]

            # Discard the last voxel along odd dimensions
            crop = [0 if x == 1 else x % 2 for x in dat.shape[-3:]]
            slcr = [slice(-1) if x else slice(None) for x in crop]
            dat = dat[tuple([Ellipsis, *slcr])]

            patch_shape = dat.shape[-3:]

            # Reshape into patches of shape 2x2x2
            windowed_shape = [
                x for n in patch_shape for x in (max(n // 2, 1), min(n, 2))
            ]
            dat = dat.reshape(batch + windowed_shape)
            # -> last `ndim`` dimensions have shape 2x2x2
            dat = dat.transpose(
                list(range(len(batch)))
                + list(range(len(batch), len(batch) + 2 * ndim, 2))
                + list(range(len(batch) + 1, len(batch) + 2 * ndim, 2))
            )
            # -> flatten patches
            smaller_shape = [max(n // 2, 1) for n in patch_shape]
            dat = dat.reshape(batch + smaller_shape + [-1])

            # Compute the median/mean of each patch
            dtype = dat.dtype
            dat = func(dat, axis=-1)
            dat = dat.astype(dtype)

            # Write output
            slicer = [Ellipsis] + [
                slice(i * max_load // 2, min((i + 1) * max_load // 2, n))
                for i, n in zip(chunk_index, shape)
            ]

            omz[str(level)][tuple(slicer)] = dat

    print("")

    return allshapes

    pass

def write_ome_metadata(
    path: str | os.PathLike,
    axes: list[str],
    space_scale: float | list[float] = 1,
    time_scale: float = 1,
    space_unit: str = "micrometer",
    time_unit: str = "second",
    name: str = "",
    pyramid_aligns: str | int | list[str | int] = 2,
    levels: int | None = None,
) -> None:
    """
    Write OME metadata into Zarr.

    Parameters
    ----------
    path : str | PathLike
        Path to parent Zarr.
    axes : list[str]
        Name of each dimension, in Zarr order (t, c, z, y, x)
    space_scale : float | list[float]
        Finest-level voxel size, in Zarr order (z, y, x)
    time_scale : float
        Time scale
    space_unit : str
        Unit of spatial scale (assumed identical across dimensions)
    space_time : str
        Unit of time scale
    name : str
        Name attribute
    pyramid_aligns : float | list[float] | {"center", "edge"}
        Whether the pyramid construction aligns the edges or the centers
        of the corner voxels. If a (list of) number, assume that a moving
        window of that size was used.
    levels : int
        Number of existing levels. Default: find out automatically.
    zarr_version : {2, 3} | None
        Zarr version. If `None`, guess from existing zarr array.

    """

    # Detect zarr version

    # Read shape at each pyramid level
    zname = ".zarray"
    shapes = []
    level = 0
    while True:
        if levels is not None and level > levels:
            break

        zpath = path / str(level) / zname
        if not zpath.exists():
            levels = level
            break

        level += 1
        with zpath.open("rb") as f:
            zarray = json.load(f)
            shapes += [zarray["shape"]]

    axis_to_type = {
        "x": "space",
        "y": "space",
        "z": "space",
        "t": "time",
        "c": "channel",
    }

    # Number of spatial (s), batch (b) and total (n) dimensions
    ndim = len(axes)
    sdim = sum(axis_to_type[axis] == "space" for axis in axes)
    bdim = ndim - sdim

    if isinstance(pyramid_aligns, (int, str)):
        pyramid_aligns = [pyramid_aligns]
    pyramid_aligns = list(pyramid_aligns)
    if len(pyramid_aligns) < sdim:
        repeat = pyramid_aligns[:1] * (sdim - len(pyramid_aligns))
        pyramid_aligns = repeat + pyramid_aligns
    pyramid_aligns = pyramid_aligns[-sdim:]

    if isinstance(space_scale, (int, float)):
        space_scale = [space_scale]
    space_scale = list(space_scale)
    if len(space_scale) < sdim:
        repeat = space_scale[:1] * (sdim - len(space_scale))
        space_scale = repeat + space_scale
    space_scale = space_scale[-sdim:]

    multiscales = [
        {
            "version": "0.4",
            "axes": [
                {
                    "name": axis,
                    "type": axis_to_type[axis],
                }
                if axis_to_type[axis] == "channel"
                else {
                    "name": axis,
                    "type": axis_to_type[axis],
                    "unit": (
                        space_unit
                        if axis_to_type[axis] == "space"
                        else time_unit
                        if axis_to_type[axis] == "time"
                        else None
                    ),
                }
                for axis in axes
            ],
            "datasets": [],
            "type": "median window " + "x".join(["2"] * sdim),
            "name": name,
        }
    ]

    shape = shape0 = shapes[0]
    for n in range(len(shapes)):
        shape = shapes[n]
        multiscales[0]["datasets"].append({})
        level = multiscales[0]["datasets"][-1]
        level["path"] = str(n)

        scale = [1] * bdim + [
            (
                pyramid_aligns[i] ** n
                if not isinstance(pyramid_aligns[i], str)
                else (shape0[bdim + i] / shape[bdim + i])
                if pyramid_aligns[i][0].lower() == "e"
                else ((shape0[bdim + i] - 1) / (shape[bdim + i] - 1))
            )
            * space_scale[i]
            for i in range(sdim)
        ]
        translation = [0] * bdim + [
            (
                pyramid_aligns[i] ** n - 1
                if not isinstance(pyramid_aligns[i], str)
                else (shape0[bdim + i] / shape[bdim + i]) - 1
                if pyramid_aligns[i][0].lower() == "e"
                else 0
            )
            * 0.5
            * space_scale[i]
            for i in range(sdim)
        ]

        level["coordinateTransformations"] = [
            {
                "type": "scale",
                "scale": scale,
            },
            {
                "type": "translation",
                "translation": translation,
            },
        ]

    scale = [1] * ndim
    if "t" in axes:
        scale[axes.index("t")] = time_scale
    multiscales[0]["coordinateTransformations"] = [{"scale": scale, "type": "scale"}]


    multiscales[0]["version"] = "0.4"
    with (path / ".zgroup").open("wt") as f:
        json.dump({"zarr_format": 2}, f, indent=4)
    with (path / ".zattrs").open("wt") as f:
        json.dump({"multiscales": multiscales}, f, indent=4)



def niftizarr_write_header(
    omz,
    shape: list[int],
    affine: np.ndarray,
    dtype: np.dtype | str,
    unit: Literal["micron", "mm"] | None = None,
    header: nib.Nifti1Header | nib.Nifti2Header | None = None,
    nifti_version: Literal[1,2] = 1
) -> None:
    """
    Write NIfTI header in a NIfTI-Zarr file.

    Parameters
    ----------
    path : PathLike | str
        Path to parent Zarr.
    affine : (4, 4) matrix
        Orientation matrix.
    shape : list[int]
        Array shape, in NIfTI order (x, y, z, t, c).
    dtype : np.dtype | str
        Data type.
    unit : {"micron", "mm"}, optional
        World unit.
    header : nib.Nifti1Header | nib.Nifti2Header, optional
        Pre-instantiated header.
    zarr_version : int, default=3
        Zarr version.
    """
    # TODO: we do not write the json zattrs, but it should be added in
    #       once the nifti-zarr package is released

    # If dimensions do not fit in a short (which is often the case), we
    # use NIfTI 2.
    if all(x < 32768 for x in shape) or nifti_version == 1:
        NiftiHeader = nib.Nifti1Header
    else:
        NiftiHeader = nib.Nifti2Header

    header = header or NiftiHeader()
    header.set_data_shape(shape)
    header.set_data_dtype(dtype)
    header.set_qform(affine)
    header.set_sform(affine)
    if unit:
        header.set_xyzt_units(nib.nifti1.unit_codes.code[unit])
    header = np.frombuffer(header.structarr.tobytes(), dtype="u1")


    metadata = {
        "chunks": [len(header)],
        "order": "F",
        "dtype": "|u1",
        "fill_value": None,
        "compressor": None, # TODO: Subject to change compression
    }

    omz.create_dataset("nifti", data=header, shape=len(header), **metadata)

    print("done.")




#
# def generate_pyramid(inp, inp_chunk, nblevels, ni, nj, nk, no_pool, omz):
#     for i, j, k in product(range(ni), range(nj), range(nk)):
#         level_chunk = inp_chunk
#         loaded_chunk = inp[
#                        k * level_chunk[0]: (k + 1) * level_chunk[0],
#                        j * level_chunk[1]: (j + 1) * level_chunk[1],
#                        i * level_chunk[2]: (i + 1) * level_chunk[2],
#                        ]
#
#         out_level = omz["0"]
#
#         print(
#             f"[{i + 1:03d}, {j + 1:03d}, {k + 1:03d}]",
#             "/",
#             f"[{ni:03d}, {nj:03d}, {nk:03d}]",
#             # f"({1 + level}/{nblevels})",
#             end="\r",
#         )
#
#         # save current chunk
#         out_level[
#         k * level_chunk[0]: k * level_chunk[0] + loaded_chunk.shape[0],
#         j * level_chunk[1]: j * level_chunk[1] + loaded_chunk.shape[1],
#         i * level_chunk[2]: i * level_chunk[2] + loaded_chunk.shape[2],
#         ] = loaded_chunk
#
#         for level in range(nblevels):
#             out_level = omz[str(level)]
#
#             print(
#                 f"[{i + 1:03d}, {j + 1:03d}, {k + 1:03d}]",
#                 "/",
#                 f"[{ni:03d}, {nj:03d}, {nk:03d}]",
#                 f"({1 + level}/{nblevels})",
#                 end="\r",
#             )
#
#             # save current chunk
#             out_level[
#             k * level_chunk[0]: k * level_chunk[0] + loaded_chunk.shape[0],
#             j * level_chunk[1]: j * level_chunk[1] + loaded_chunk.shape[1],
#             i * level_chunk[2]: i * level_chunk[2] + loaded_chunk.shape[2],
#             ] = loaded_chunk
#             # ensure divisible by 2
#             loaded_chunk = loaded_chunk[
#                 slice(2 * (loaded_chunk.shape[0] // 2) if 0 != no_pool else None),
#                 slice(2 * (loaded_chunk.shape[1] // 2) if 1 != no_pool else None),
#                 slice(2 * (loaded_chunk.shape[2] // 2) if 2 != no_pool else None),
#             ]
#             # mean pyramid (average each 2x2x2 patch)
#             if no_pool == 0:
#                 loaded_chunk = (
#                                        loaded_chunk[:, 0::2, 0::2]
#                                        + loaded_chunk[:, 0::2, 1::2]
#                                        + loaded_chunk[:, 1::2, 0::2]
#                                        + loaded_chunk[:, 1::2, 1::2]
#                                ) / 4
#             elif no_pool == 1:
#                 loaded_chunk = (
#                                        loaded_chunk[0::2, :, 0::2]
#                                        + loaded_chunk[0::2, :, 1::2]
#                                        + loaded_chunk[1::2, :, 0::2]
#                                        + loaded_chunk[1::2, :, 1::2]
#                                ) / 4
#             elif no_pool == 2:
#                 loaded_chunk = (
#                                        loaded_chunk[0::2, 0::2, :]
#                                        + loaded_chunk[0::2, 1::2, :]
#                                        + loaded_chunk[1::2, 0::2, :]
#                                        + loaded_chunk[1::2, 1::2, :]
#                                ) / 4
#             else:
#                 loaded_chunk = (
#                                        loaded_chunk[0::2, 0::2, 0::2]
#                                        + loaded_chunk[0::2, 0::2, 1::2]
#                                        + loaded_chunk[0::2, 1::2, 0::2]
#                                        + loaded_chunk[0::2, 1::2, 1::2]
#                                        + loaded_chunk[1::2, 0::2, 0::2]
#                                        + loaded_chunk[1::2, 0::2, 1::2]
#                                        + loaded_chunk[1::2, 1::2, 0::2]
#                                        + loaded_chunk[1::2, 1::2, 1::2]
#                                ) / 8
#             level_chunk = [
#                 x if i == no_pool else x // 2 for i, x in enumerate(level_chunk)
#             ]
#

# def generate_pyramid(i, j, k, level_chunk, loaded_chunk, nblevels, ni, nj, nk, no_pool,
#                      omz):
#     for level in range(nblevels):
#         out_level = omz[str(level)]
#
#         print(
#             f"[{i + 1:03d}, {j + 1:03d}, {k + 1:03d}]",
#             "/",
#             f"[{ni:03d}, {nj:03d}, {nk:03d}]",
#             f"({1 + level}/{nblevels})",
#             end="\r",
#         )
#
#         # save current chunk
#         out_level[
#         k * level_chunk[0]: k * level_chunk[0] + loaded_chunk.shape[0],
#         j * level_chunk[1]: j * level_chunk[1] + loaded_chunk.shape[1],
#         i * level_chunk[2]: i * level_chunk[2] + loaded_chunk.shape[2],
#         ] = loaded_chunk
#         # ensure divisible by 2
#         loaded_chunk = loaded_chunk[
#             slice(2 * (loaded_chunk.shape[0] // 2) if 0 != no_pool else None),
#             slice(2 * (loaded_chunk.shape[1] // 2) if 1 != no_pool else None),
#             slice(2 * (loaded_chunk.shape[2] // 2) if 2 != no_pool else None),
#         ]
#         # mean pyramid (average each 2x2x2 patch)
#         if no_pool == 0:
#             loaded_chunk = (
#                                    loaded_chunk[:, 0::2, 0::2]
#                                    + loaded_chunk[:, 0::2, 1::2]
#                                    + loaded_chunk[:, 1::2, 0::2]
#                                    + loaded_chunk[:, 1::2, 1::2]
#                            ) / 4
#         elif no_pool == 1:
#             loaded_chunk = (
#                                    loaded_chunk[0::2, :, 0::2]
#                                    + loaded_chunk[0::2, :, 1::2]
#                                    + loaded_chunk[1::2, :, 0::2]
#                                    + loaded_chunk[1::2, :, 1::2]
#                            ) / 4
#         elif no_pool == 2:
#             loaded_chunk = (
#                                    loaded_chunk[0::2, 0::2, :]
#                                    + loaded_chunk[0::2, 1::2, :]
#                                    + loaded_chunk[1::2, 0::2, :]
#                                    + loaded_chunk[1::2, 1::2, :]
#                            ) / 4
#         else:
#             loaded_chunk = (
#                                    loaded_chunk[0::2, 0::2, 0::2]
#                                    + loaded_chunk[0::2, 0::2, 1::2]
#                                    + loaded_chunk[0::2, 1::2, 0::2]
#                                    + loaded_chunk[0::2, 1::2, 1::2]
#                                    + loaded_chunk[1::2, 0::2, 0::2]
#                                    + loaded_chunk[1::2, 0::2, 1::2]
#                                    + loaded_chunk[1::2, 1::2, 0::2]
#                                    + loaded_chunk[1::2, 1::2, 1::2]
#                            ) / 8
#         level_chunk = [
#             x if i == no_pool else x // 2 for i, x in enumerate(level_chunk)
#         ]

#
# def create_level(chunk, inp, nblevels, no_pool, omz, opt):
#     shape_level = inp.shape
#     for level in range(1,nblevels,1):
#         opt["chunks"] = [min(x, chunk) for x in shape_level]
#         omz.create_dataset(str(level), shape=shape_level, **opt)
#         shape_level = [x if i == no_pool else x // 2 for i, x in enumerate(shape_level)]
#


