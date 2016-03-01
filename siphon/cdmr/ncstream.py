# Copyright (c) 2013-2015 Unidata.
# Distributed under the terms of the MIT License.
# SPDX-License-Identifier: MIT

from __future__ import print_function
import logging
import zlib

import numpy as np

from . import ncStream_pb2 as stream  # noqa

MAGIC_HEADER = b'\xad\xec\xce\xda'
MAGIC_DATA = b'\xab\xec\xce\xba'
MAGIC_VDATA = b'\xab\xef\xfe\xba'
MAGIC_VEND = b'\xed\xef\xfe\xda'
MAGIC_ERR = b'\xab\xad\xba\xda'

log = logging.getLogger('siphon.ncstream')
log.addHandler(logging.StreamHandler())  # Python 2.7 needs a handler set
log.setLevel(logging.WARNING)


def read_ncstream_messages(fobj):
    messages = []

    while True:
        magic = read_magic(fobj)
        if not magic:
            break

        if magic == MAGIC_HEADER:
            log.debug('Header chunk')
            messages.append(stream.Header())
            messages[0].ParseFromString(read_block(fobj))
            log.debug('Header: %s', str(messages[0]))
        elif magic == MAGIC_DATA:
            log.debug('Data chunk')
            data = stream.Data()
            data.ParseFromString(read_block(fobj))
            log.debug('Data: %s', str(data))
            if data.dataType in (stream.STRING, stream.OPAQUE) or data.vdata:
                log.debug('Reading string/opaque')
                num_obj = read_var_int(fobj)
                blocks = [read_block(fobj) for _ in range(num_obj)]
                if data.dataType == stream.STRING:
                    blocks = [b.decode('utf-8', errors='ignore') for b in blocks]

                # Again endian isn't coded properly
                dt = data_type_to_numpy(data.dataType).newbyteorder('>')
                if data.vdata:
                    arr = np.array([np.frombuffer(b, dtype=dt) for b in blocks])
                    messages.append(arr)
                else:
                    messages.append(np.array(blocks, dtype=dt))
            elif data.dataType in _dtypeLookup:
                log.debug('Reading array data')
                bin_data = read_block(fobj)
                log.debug('Binary data: %s', bin_data)
                data_block = make_array(data, bin_data)
                messages.append(data_block)
            elif data.dataType == stream.STRUCTURE:
                log.debug('Reading structure')
                sd = stream.StructureData()
                sd.ParseFromString(read_block(fobj))
                log.debug('StructureData: %s', str(sd))
                data_block = make_array(data, sd)
                messages.append(data_block)
            elif data.dataType == stream.SEQUENCE:
                log.debug('Reading sequence')
                blocks = []
                magic = read_magic(fobj)
                while magic != MAGIC_VEND:
                    if magic == MAGIC_VDATA:
                        log.error('Bad magic for struct/seq data!')
                    blocks.append(stream.StructureData())
                    blocks[0].ParseFromString(read_block(fobj))
                    magic = read_magic(fobj)
                messages.append((data, blocks))
            else:
                raise NotImplementedError("Don't know how to handle data type: {0}".format(
                    data.dataType))
        elif magic == MAGIC_ERR:
            err = stream.Error()
            err.ParseFromString(read_block(fobj))
            raise RuntimeError(err.message)
        else:
            log.error('Unknown magic: ' + str(' '.join('%02x' % b for b in magic)))

    return messages


def read_magic(fobj):
    return fobj.read(4)


def read_block(fobj):
    num = read_var_int(fobj)
    return fobj.read(num)


def make_vlen(data_header, blocks):
    return [s]


def make_array(data_header, buf):
    """Handles returning an numpy array from serialized ncstream data.

    Can handle taking a data header and either bytes containing data or a StructureData
    instance, which will have binary data as well as some additional information.

    Parameters
    ----------
    data_header : Data
    buf : bytes or StructureData
    """
    # Structures properly encode endian, but regular data is big endian
    if data_header.dataType == stream.STRUCTURE:
        struct_header = buf
        buf = struct_header.data
        endian = '>' if data_header.bigend else '<'
        dt = np.dtype([(endian, np.void, struct_header.rowLength)])
    else:
        endian = '>'
        dt = data_type_to_numpy(data_header.dataType)

    dt = dt.newbyteorder(endian)

    # Figure out the shape of the resulting array
    if data_header.vdata:
        shape = None
    else:
        shape = tuple(r.size for r in data_header.section.range)

    # Handle decompressing the bytes
    if data_header.compress == stream.DEFLATE:
        # Structure data not currently compressed by TDS
        if data_header.dataType != stream.STRUCTURE:
            buf = zlib.decompress(buf)
            assert len(buf) == data_header.uncompressedSize
    elif data_header.compress != stream.NONE:
        raise NotImplementedError('Compression type {0} not implemented!'.format(
            data_header.compress))

    ret = np.frombuffer(bytearray(buf), dtype=dt)

    # Only reshape if non-scalar. This is necessary because we handle compound types.
    if shape:
        ret = ret.reshape(*shape)

    return ret

# STRUCTURE = 8;
# SEQUENCE = 9;
_dtypeLookup = {stream.CHAR: 'S1', stream.BYTE: 'b', stream.SHORT: 'i2',
                stream.INT: 'i4', stream.LONG: 'i8', stream.FLOAT: 'f4',
                stream.DOUBLE: 'f8', stream.STRING: 'O',
                stream.ENUM1: 'B', stream.ENUM2: 'u2', stream.ENUM4: 'u4',
                stream.OPAQUE: 'O'}


def data_type_to_numpy(datatype, unsigned=False):
    basic_type = _dtypeLookup[datatype]

    if datatype in (stream.STRING, stream.OPAQUE):
        return np.dtype(basic_type)

    if unsigned:
        basic_type = basic_type.replace('i', 'u')
    return np.dtype('=' + basic_type)


def struct_to_dtype(struct):
    """Convert a Structure specification to a numpy structured dtype."""
    # str() around name necessary because protobuf gives unicode names, but dtype doesn't
    # support them on Python 2
    fields = [(str(var.name), data_type_to_numpy(var.dataType, var.unsigned))
              for var in struct.vars]
    for s in struct.structs:
        fields.append((s.name, struct_to_dtype(s)))

    log.debug('Structure fields: %s', fields)
    dt = np.dtype(fields)
    return dt


def unpack_variable(var):
    # If we actually get a structure instance, handle turning that into a variable
    if var.dataType == stream.STRUCTURE:
        return None, struct_to_dtype(var), 'Structure'
    elif var.dataType == stream.SEQUENCE:
        log.warning('Sequence support not implemented!')

    dt = data_type_to_numpy(var.dataType, var.unsigned)
    if var.dataType == stream.OPAQUE:
        type_name = 'opaque'
    elif var.dataType == stream.STRING:
        type_name = 'string'
    else:
        type_name = dt.name

    if var.data:
        log.debug('Storing variable data: %s %s', dt, var.data)
        if var.dataType is str:
            data = var.data
        else:
            # Always sent big endian
            data = np.fromstring(var.data, dtype=dt.newbyteorder('>'))
    else:
        data = None

    return data, dt, type_name

_attrConverters = {stream.Attribute.BYTE: np.dtype('>b'),
                   stream.Attribute.SHORT: np.dtype('>i2'),
                   stream.Attribute.INT: np.dtype('>i4'),
                   stream.Attribute.LONG: np.dtype('>i8'),
                   stream.Attribute.FLOAT: np.dtype('>f4'),
                   stream.Attribute.DOUBLE: np.dtype('>f8')}


def unpack_attribute(att):
    if att.unsigned:
        log.warning('Unsupported unsigned attribute!')

    if att.len == 0:
        val = None
    elif att.type == stream.Attribute.STRING:
        val = att.sdata
    else:
        val = np.fromstring(att.data,
                            dtype=_attrConverters[att.type], count=att.len)

    if att.len == 1:
        val = val[0]

    return att.name, val


def read_var_int(file_obj):
    'Read a variable-length integer'
    # Read all bytes from here, stopping with the first one that does not have
    # the MSB set. Save the lower 7 bits, and keep stacking to the *left*.
    val = 0
    shift = 0
    while True:
        # Read next byte
        next_val = ord(file_obj.read(1))
        val = ((next_val & 0x7F) << shift) | val
        shift += 7
        if not next_val & 0x80:
            break

    return val
