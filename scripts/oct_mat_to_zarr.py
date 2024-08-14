"""
(OCT) Matlab to OME-ZARR
========================

This script converts Matlab files generated by the MGH in-house OCT pipeline
into a pyramidal OME-ZARR hierarchy.

dependencies:
    numpy
    scipy
    zarr
    nibabel
    cyclopts
"""
import cyclopts
import zarr
import ast
import re
import os
import math
import json
import h5py
import numpy as np
import nibabel as nib
from typing import Optional
from itertools import product
from functools import wraps
from scipy.io import loadmat
from warnings import warn
from contextlib import contextmanager

from utils import (
    ceildiv, make_compressor, convert_unit, to_ome_unit, to_nifti_unit,
    orientation_to_affine, center_affine
)

app = cyclopts.App(help_format="markdown")


def automap(func):
    """Decorator to automatically map the array in the mat file"""

    @wraps(func)
    def wrapper(inp, out, **kwargs):
        if out is None:
            out = os.path.splitext(inp)
            out += '.nii.zarr' if kwargs.get('nii', False) else '.ome.zarr'
        kwargs['nii'] = kwargs.get('nii', False) or out.endswith('.nii.zarr')
        with mapmat(inp) as dat:
            return func(dat, out, **kwargs)

    return wrapper


@app.default
@automap
def convert(
    inp: str,
    out: str = None,
    *,
    meta: str = None,
    chunk: int = 128,
    compressor: str = 'blosc',
    compressor_opt: str = "{}",
    max_load: int = 128,
    max_levels: int = 5,
    no_pool: Optional[int] = None,
    nii: bool = False,
    orientation: str = 'RAS',
    center: bool = True,
):
    """
    This command converts OCT volumes stored in raw matlab files
    into a pyramidal OME-ZARR (or NIfTI-Zarr) hierarchy.

    Parameters
    ----------
    inp
        Path to the input mat file
    out
        Path to the output Zarr directory [<INP>.ome.zarr]
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
        print('Write JSON')
        with open(meta, 'r') as f:
            meta_txt = f.read()
            meta_json = make_json(meta_txt)
        path_json = '.'.join(out.split('.')[:-2]) + '.json'
        with open(path_json, 'w') as f:
            json.dump(meta_json, f, indent=4)
        vx = meta_json['PixelSize']
        unit = meta_json['PixelSizeUnits']
    else:
        vx = [1] * 3
        unit = 'um'

    # Prepare Zarr group
    omz = zarr.storage.DirectoryStore(out)
    omz = zarr.group(store=omz, overwrite=True)

    # Prepare chunking options
    opt = {
        'dimension_separator': r'/',
        'order': 'F',
        'dtype': np.dtype(inp.dtype).str,
        'fill_value': None,
        'compressor': make_compressor(compressor, **compressor_opt),
    }

    inp_chunk = [min(x, max_load) for x in inp.shape]
    nk = ceildiv(inp.shape[0], inp_chunk[0])
    nj = ceildiv(inp.shape[1], inp_chunk[1])
    ni = ceildiv(inp.shape[2], inp_chunk[2])

    nblevels = min([
        int(math.ceil(math.log2(x)))
        for i, x in enumerate(inp.shape)
        if i != no_pool
    ])
    nblevels = min(nblevels, int(math.ceil(math.log2(max_load))))
    nblevels = min(nblevels, max_levels)

    # create all arrays in the group
    shape_level = inp.shape
    for level in range(nblevels):
        opt['chunks'] = [min(x, chunk) for x in shape_level]
        omz.create_dataset(str(level), shape=shape_level, **opt)
        shape_level = [
            x if i == no_pool else x//2
            for i, x in enumerate(shape_level)
        ]

    # iterate across input chunks
    for i, j, k in product(range(ni), range(nj), range(nk)):

        level_chunk = inp_chunk
        loaded_chunk = inp[
            k*level_chunk[0]:(k+1)*level_chunk[0],
            j*level_chunk[1]:(j+1)*level_chunk[1],
            i*level_chunk[2]:(i+1)*level_chunk[2],
        ]

        for level in range(nblevels):
            out_level = omz[str(level)]

            print(f'[{i+1:03d}, {j+1:03d}, {k+1:03d}]', '/',
                  f'[{ni:03d}, {nj:03d}, {nk:03d}]',
                  f'({1+level}/{nblevels})', end='\r')

            # save current chunk
            out_level[
                k*level_chunk[0]:k*level_chunk[0]+loaded_chunk.shape[0],
                j*level_chunk[1]:j*level_chunk[1]+loaded_chunk.shape[1],
                i*level_chunk[2]:i*level_chunk[2]+loaded_chunk.shape[2],
            ] = loaded_chunk
            # ensure divisible by 2
            loaded_chunk = loaded_chunk[
                slice(2*(level_chunk.shape[0]//2) if 0 != no_pool else None),
                slice(2*(level_chunk.shape[1]//2) if 1 != no_pool else None),
                slice(2*(level_chunk.shape[2]//2) if 2 != no_pool else None),
            ]
            # mean pyramid (average each 2x2x2 patch)
            if no_pool == 0:
                loaded_chunk = (
                    loaded_chunk[:, 0::2, 0::2] +
                    loaded_chunk[:, 0::2, 1::2] +
                    loaded_chunk[:, 1::2, 0::2] +
                    loaded_chunk[:, 1::2, 1::2]
                ) / 4
            elif no_pool == 1:
                loaded_chunk = (
                    loaded_chunk[0::2, :, 0::2] +
                    loaded_chunk[0::2, :, 1::2] +
                    loaded_chunk[1::2, :, 0::2] +
                    loaded_chunk[1::2, :, 1::2]
                ) / 4
            elif no_pool == 2:
                loaded_chunk = (
                    loaded_chunk[0::2, 0::2, :] +
                    loaded_chunk[0::2, 1::2, :] +
                    loaded_chunk[1::2, 0::2, :] +
                    loaded_chunk[1::2, 1::2, :]
                ) / 4
            else:
                inp_chunk = (
                    inp_chunk[0::2, 0::2, 0::2] +
                    inp_chunk[0::2, 0::2, 1::2] +
                    inp_chunk[0::2, 1::2, 0::2] +
                    inp_chunk[0::2, 1::2, 1::2] +
                    inp_chunk[1::2, 0::2, 0::2] +
                    inp_chunk[1::2, 0::2, 1::2] +
                    inp_chunk[1::2, 1::2, 0::2] +
                    inp_chunk[1::2, 1::2, 1::2]
                ) / 8
            level_chunk = [
                x if i == no_pool else x // 2
                for i, x in enumerate(level_chunk)
            ]
    print('')

    # Write OME-Zarr multiscale metadata
    print('Write metadata')
    print(unit)
    ome_unit = to_ome_unit(unit)
    multiscales = [{
        'version': '0.4',
        'axes': [
            {"name": "z", "type": "space", "unit": ome_unit},
            {"name": "y", "type": "space", "unit": ome_unit},
            {"name": "x", "type": "space", "unit": ome_unit}
        ],
        'datasets': [],
        'type': ('2x2x2' if no_pool is None else '2x2') + 'mean window',
        'name': '',
    }]

    for n in range(nblevels):
        multiscales[0]['datasets'].append({})
        level = multiscales[0]['datasets'][-1]
        level["path"] = str(n)

        # With a moving window, the scaling factor is exactly 2, and
        # the edges of the top-left voxel are aligned
        level["coordinateTransformations"] = [
            {
                "type": "scale",
                "scale": [
                    (1 if no_pool == 0 else 2**n)*vx[0],
                    (1 if no_pool == 1 else 2**n)*vx[1],
                    (1 if no_pool == 2 else 2**n)*vx[2],
                ]
            },
            {
                "type": "translation",
                "translation": [
                    (0 if no_pool == 0 else (2**n - 1))*vx[0]*0.5,
                    (0 if no_pool == 1 else (2**n - 1))*vx[1]*0.5,
                    (0 if no_pool == 2 else (2**n - 1))*vx[2]*0.5,
                ]
            }
        ]
    multiscales[0]["coordinateTransformations"] = [
        {
            "scale": [1.0] * 3,
            "type": "scale"
        }
    ]
    omz.attrs["multiscales"] = multiscales

    if not nii:
        print('done.')
        return

    # Write NIfTI-Zarr header
    # NOTE: we use nifti2 because dimensions typically do not fit in a short
    # TODO: we do not write the json zattrs, but it should be added in
    #       once the nifti-zarr package is released
    shape = list(reversed(omz['0'].shape))
    affine = orientation_to_affine(orientation, *vx[::-1])
    if center:
        affine = center_affine(affine, shape[:3])
    header = nib.Nifti2Header()
    header.set_data_shape(shape)
    header.set_data_dtype(omz['0'].dtype)
    header.set_qform(affine)
    header.set_sform(affine)
    header.set_xyzt_units(nib.nifti1.unit_codes.code[to_nifti_unit(unit)])
    header.structarr['magic'] = b'nz2\0'
    header = np.frombuffer(header.structarr.tobytes(), dtype='u1')
    opt = {
        'chunks': [len(header)],
        'dimension_separator': r'/',
        'order': 'F',
        'dtype': '|u1',
        'fill_value': None,
        'compressor': None,
    }
    omz.create_dataset('nifti', data=header, shape=shape, **opt)
    print('done.')


@contextmanager
def mapmat(fname):
    """Load or memory-map an array stored in a .mat file"""
    try:
        # "New" .mat file
        f = h5py.File(fname, 'r')
    except Exception:
        # "Old" .mat file
        f = loadmat(fname)
    keys = list(f.keys())
    if len(keys) > 1:
        warn(f'More than one key in .mat, arbitrarily loading "{keys[0]}"')
    yield f.get(keys[0])
    if hasattr(f, 'close'):
        f.close()


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
        number = r'-?(\d+\.?\d*|\d*\.?\d+)(E-?\d+)?'
        value = 'x'.join([number]*(n or 1))
        match = re.fullmatch(r'(?P<value>' + value + r')(?P<unit>\w*)', string)
        value, unit = match.group('value'), match.group('unit')
        value = list(map(float, value.split('x')))
        if n is None:
            value = value[0]
        return value, unit

    meta = {
        'BodyPart': 'BRAIN',
        'Environment': 'exvivo',
        'SampleStaining': 'none',
    }

    for line in oct_meta.split('\n'):
        if ':' not in line:
            continue

        key, value = line.split(':')
        key, value = key.strip(), value.strip()

        if key == 'Image medium':
            parts = value.split()
            if 'TDE' in parts:
                parts[parts.index('TDE')] = "2,2' Thiodiethanol (TDE)"
            meta['SampleMedium'] = ' '.join(parts)

        elif key == 'Center Wavelength':
            value, unit = parse_value_unit(value)
            meta['Wavelength'] = value
            meta['WavelengthUnit'] = unit

        elif key == 'Axial resolution':
            value, unit = parse_value_unit(value)
            meta['ResolutionAxial'] = value
            meta['ResolutionAxialUnit'] = unit

        elif key == 'Lateral resolution':
            value, unit = parse_value_unit(value)
            meta['ResolutionLateral'] = value
            meta['ResolutionLateralUnit'] = unit

        elif key == 'Voxel size':
            value, unit = parse_value_unit(value, n=3)
            meta['PixelSize'] = value
            meta['PixelSizeUnits'] = unit

        elif key == 'Depth focus range':
            value, unit = parse_value_unit(value)
            meta['DepthFocusRange'] = value
            meta['DepthFocusRangeUnit'] = unit

        elif key == 'Number of focuses':
            value, unit = parse_value_unit(value)
            meta['FocusCount'] = int(value)

        elif key == 'Slice thickness':
            value, unit = parse_value_unit(value)
            unit = convert_unit(value, unit[:-1], 'u')
            meta['SliceThickness'] = value

        elif key == 'Number of slices':
            value, unit = parse_value_unit(value)
            meta['SliceCount'] = int(value)

        elif key == 'Modality':
            meta['OCTModality'] = value

        else:
            continue

    return meta


if __name__ == "__main__":
    app()
