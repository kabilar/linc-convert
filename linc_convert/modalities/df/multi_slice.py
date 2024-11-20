"""
Convert JPEG2000 files generated by MBF-Neurolucida into a OME-ZARR pyramid.

We do not recompute the image pyramid but instead reuse the JPEG2000
levels (obtained by wavelet transform).
"""

# stdlib
import ast
import json
import os

# externals
import glymur
import nibabel as nib
import numpy as np
import zarr
from cyclopts import App

# internals
from linc_convert.modalities.df.cli import df
from linc_convert.utils.j2k import WrappedJ2K, get_pixelsize
from linc_convert.utils.math import ceildiv, floordiv
from linc_convert.utils.orientation import center_affine, orientation_to_affine
from linc_convert.utils.zarr import make_compressor

HOME = "/space/aspasia/2/users/linc/000003"

# Path to LincBrain dataset
LINCSET = os.path.join(HOME, "sourcedata")
LINCOUT = os.path.join(HOME, "rawdata")


ms = App(name="multislice", help_format="markdown")
df.command(ms)


@ms.default
def convert(
    inp: list[str],
    out: str | None = None,
    *,
    chunk: int = 1024,
    compressor: str = "blosc",
    compressor_opt: str = "{}",
    max_load: int = 16384,
    nii: bool = False,
    orientation: str = "coronal",
    center: bool = True,
    thickness: float | None = None,
) -> None:
    """
    Convert JPEG2000 files generated by MBF-Neurolucida into a Zarr pyramid.

    It does not recompute the image pyramid but instead reuse the
    JPEG2000 levels (obtained by wavelet transform).

    This command converts a batch of slices and stacks them together
    into a single 3D Zarr.

    Orientation
    -----------
    The anatomical orientation of the slice is given in terms of RAS axes.

    It is a combination of two letters from the set
    `{"L", "R", "A", "P", "I", "S"}`, where

    * the first letter corresponds to the horizontal dimension and
      indicates the anatomical meaning of the _right_ of the jp2 image,
    * the second letter corresponds to the vertical dimension and
      indicates the anatomical meaning of the _bottom_ of the jp2 image,
    * the third letter corresponds to the slice dimension and
      indicates the anatomical meaninff of the _end_ of the stack.

    We also provide the aliases

    * `"coronal"` == `"LI"`
    * `"axial"` == `"LP"`
    * `"sagittal"` == `"PI"`

    The orientation flag is only useful when converting to nifti-zarr.

    Parameters
    ----------
    inp
        Path to the input slices
    out
        Path to the output Zarr directory [<dirname(INP)>.ome.zarr]
    chunk
        Output chunk size
    compressor : {blosc, zlib, raw}
        Compression method
    compressor_opt
        Compression options
    max_load
        Maximum input chunk size
    nii
        Convert to nifti-zarr. True if path ends in ".nii.zarr"
    orientation
        Orientation of the slice
    center
        Set RAS[0, 0, 0] at FOV center
    thickness
        Slice thickness
    """
    # Default output path
    if not out:
        out = os.path.splitext(inp[0])[0]
        out += ".nii.zarr" if nii else ".ome.zarr"
    nii = nii or out.endswith(".nii.zarr")

    if isinstance(compressor_opt, str):
        compressor_opt = ast.literal_eval(compressor_opt)

    # Prepare Zarr group
    omz = zarr.storage.DirectoryStore(out)
    omz = zarr.group(store=omz, overwrite=True)

    nblevel, has_channel, dtype_jp2 = float("inf"), float("inf"), ""

    # Compute output shape
    new_height, new_width = 0, 0
    for inp1 in inp:
        jp2 = glymur.Jp2k(inp1)
        nblevel = min(nblevel, jp2.codestream.segment[2].num_res)
        has_channel = min(has_channel, jp2.ndim - 2)
        dtype_jp2 = np.dtype(jp2.dtype).str
        if jp2.shape[0] > new_height:
            new_height = jp2.shape[0]
        if jp2.shape[1] > new_width:
            new_width = jp2.shape[1]
    new_size = (new_height, new_width)
    if has_channel:
        new_size += (3,)
    print(len(inp), new_size, nblevel, has_channel)

    # Prepare chunking options
    opt = {
        "chunks": list(new_size[2:]) + [1] + [chunk, chunk],
        "dimension_separator": r"/",
        "order": "F",
        "dtype": dtype_jp2,
        "fill_value": 0,
        "compressor": make_compressor(compressor, **compressor_opt),
    }
    print(opt)
    print(new_size)
    # Write each level
    for level in range(nblevel):
        shape = [ceildiv(s, 2**level) for s in new_size[:2]]
        shape = [new_size[2]] + [len(inp)] + shape

        omz.create_dataset(f"{level}", shape=shape, **opt)
        array = omz[f"{level}"]

        # Write each slice
        for idx, inp1 in enumerate(inp):
            j2k = glymur.Jp2k(inp1)
            vxw, vxh = get_pixelsize(j2k)
            subdat = WrappedJ2K(j2k, level=level)
            subdat_size = subdat.shape
            print(
                "Convert level",
                level,
                "with shape",
                shape,
                "for slice",
                idx,
                "with size",
                subdat_size,
            )

            # offset while attaching
            x = floordiv(shape[-2] - subdat_size[-2], 2)
            y = floordiv(shape[-1] - subdat_size[-1], 2)

            for channel in range(3):
                if max_load is None or (
                    subdat_size[-2] < max_load and subdat_size[-1] < max_load
                ):
                    array[
                        channel, idx, x : x + subdat_size[-2], y : y + subdat_size[-1]
                    ] = subdat[channel : channel + 1, ...][0]
                else:
                    ni = ceildiv(subdat_size[-2], max_load)
                    nj = ceildiv(subdat_size[-1], max_load)

                    for i in range(ni):
                        for j in range(nj):
                            print(f"\r{i+1}/{ni}, {j+1}/{nj}", end=" ")
                            start_x, end_x = (
                                i * max_load,
                                min((i + 1) * max_load, subdat_size[-2]),
                            )
                            start_y, end_y = (
                                j * max_load,
                                min((j + 1) * max_load, subdat_size[-1]),
                            )

                            array[
                                channel,
                                idx,
                                x + start_x : x + end_x,
                                y + start_y : y + end_y,
                            ] = subdat[
                                channel : channel + 1,
                                start_x:end_x,
                                start_y:end_y,
                            ][0]

                    print("")

    # Write OME-Zarr multiscale metadata
    print("Write metadata")
    multiscales = [
        {
            "version": "0.4",
            "axes": [
                {"name": "z", "type": "space", "unit": "micrometer"},
                {"name": "y", "type": "distance", "unit": "micrometer"},
                {"name": "x", "type": "space", "unit": "micrometer"},
            ],
            "datasets": [],
            "type": "jpeg2000",
            "name": "",
        }
    ]
    if has_channel:
        multiscales[0]["axes"].insert(0, {"name": "c", "type": "channel"})

    for n in range(nblevel):
        shape0 = omz["0"].shape[2:]
        shape = omz[str(n)].shape[2:]
        multiscales[0]["datasets"].append({})
        level = multiscales[0]["datasets"][-1]
        level["path"] = str(n)

        # I assume that wavelet transforms end up aligning voxel edges
        # across levels, so the effective scaling is the shape ratio,
        # and there is a half voxel shift wrt to the "center of first voxel"
        # frame
        level["coordinateTransformations"] = [
            {
                "type": "scale",
                "scale": [1.0] * has_channel
                + [
                    1.0,
                    (shape0[0] / shape[0]) * vxh,
                    (shape0[0] / shape[0]) * vxw,
                ],
            },
            {
                "type": "translation",
                "translation": [0.0] * has_channel
                + [
                    0.0,
                    (shape0[0] / shape[0] - 1) * vxh * 0.5,
                    (shape0[0] / shape[0] - 1) * vxw * 0.5,
                ],
            },
        ]
    multiscales[0]["coordinateTransformations"] = [
        {"scale": [1.0] * (3 + has_channel), "type": "scale"}
    ]
    omz.attrs["multiscales"] = multiscales

    # Write NIfTI-Zarr header
    # NOTE: we use nifti2 because dimensions typically do not fit in a short
    # TODO: we do not write the json zattrs, but it should be added in
    #       once the nifti-zarr package is released
    shape = list(reversed(omz["0"].shape))
    if has_channel:
        shape = shape[:3] + [1] + shape[3:]
    affine = orientation_to_affine(orientation, vxw, vxh, thickness or 1)
    if center:
        affine = center_affine(affine, shape[:2])
    header = nib.Nifti2Header()
    header.set_data_shape(shape)
    header.set_data_dtype(omz["0"].dtype)
    header.set_qform(affine)
    header.set_sform(affine)
    header.set_xyzt_units(nib.nifti1.unit_codes.code["micron"])
    header.structarr["magic"] = b"n+2\0"
    header = np.frombuffer(header.structarr.tobytes(), dtype="u1")
    opt = {
        "chunks": [len(header)],
        "dimension_separator": r"/",
        "order": "F",
        "dtype": "|u1",
        "fill_value": None,
        "compressor": None,
    }
    omz.create_dataset("nifti", data=header, shape=shape, **opt)

    # Write sidecar .json file
    json_name = os.path.splitext(out)[0]
    json_name += ".json"
    dic = {}
    dic["PixelSize"] = json.dumps([vxw, vxh])
    dic["PixelSizeUnits"] = "um"
    dic["SliceThickness"] = 1.2
    dic["SliceThicknessUnits"] = "mm"
    dic["SampleStaining"] = "LY"

    with open(json_name, "w") as outfile:
        json.dump(dic, outfile)
        outfile.write("\n")

    print("done.")
