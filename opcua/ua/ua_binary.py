"""
Binary protocol specific functions and constants
"""

import sys
import struct
import logging
import uuid
from enum import IntEnum, Enum

from opcua.ua.uaerrors import UaError
from opcua.common.utils import Buffer
from opcua import ua

if sys.version_info.major > 2:
    unicode = str

logger = logging.getLogger('__name__')


def test_bit(data, offset):
    mask = 1 << offset
    return data & mask


def set_bit(data, offset):
    mask = 1 << offset
    return data | mask


def unset_bit(data, offset):
    mask = 1 << offset
    return data & ~mask


def build_array_format_py2(prefix, length, fmtchar):
    return prefix + str(length) + fmtchar


def build_array_format_py3(prefix, length, fmtchar):
    return prefix + str(length) + chr(fmtchar)


if sys.version_info.major < 3:
    build_array_format = build_array_format_py2
else:
    build_array_format = build_array_format_py3


class _Primitive(object):
    def pack_array(self, array):
        if array is None:
            return b'\xff\xff\xff\xff'
        length = len(array)
        b = [self.pack(val) for val in array]
        b.insert(0, Primitives.Int32.pack(length))
        return b"".join(b)

    def unpack_array(self, data):
        length = Primitives.Int32.unpack(data)
        if length == -1:
            return None
        elif length == 0:
            return []
        else:
            return [self.unpack(data) for _ in range(length)]


class _DateTime(_Primitive):
    @staticmethod
    def pack(dt):
        epch = ua.datetime_to_win_epoch(dt)
        return Primitives.Int64.pack(epch)

    @staticmethod
    def unpack(data):
        epch = Primitives.Int64.unpack(data)
        return ua.win_epoch_to_datetime(epch)


class _String(_Primitive):
    @staticmethod
    def pack(string):
        if string is None:
            return Primitives.Int32.pack(-1)
        if isinstance(string, unicode):
            string = string.encode('utf-8')
        length = len(string)
        return Primitives.Int32.pack(length) + string

    @staticmethod
    def unpack(data):
        b = _Bytes.unpack(data)
        if sys.version_info.major < 3:
            return b
        else:
            if b is None:
                return b
            return b.decode("utf-8")


class _Bytes(_Primitive):
    @staticmethod
    def pack(data):
        return _String.pack(data)

    @staticmethod
    def unpack(data):
        length = Primitives.Int32.unpack(data)
        if length == -1:
            return None
        return data.read(length)


class _Null(_Primitive):
    @staticmethod
    def pack(data):
        return b""

    @staticmethod
    def unpack(data):
        return None


class _Guid(_Primitive):
    @staticmethod
    def pack(guid):
        # convert python UUID 6 field format to OPC UA 4 field format
        f1 = Primitives.UInt32.pack(guid.time_low)
        f2 = Primitives.UInt16.pack(guid.time_mid)
        f3 = Primitives.UInt16.pack(guid.time_hi_version)
        f4a = Primitives.Byte.pack(guid.clock_seq_hi_variant)
        f4b = Primitives.Byte.pack(guid.clock_seq_low)
        f4c = struct.pack('>Q', guid.node)[2:8]  # no primitive .pack available for 6 byte int
        f4 = f4a + f4b + f4c
        # concat byte fields
        b = f1 + f2 + f3 + f4

        return b

    @staticmethod
    def unpack(data):
        # convert OPC UA 4 field format to python UUID bytes
        f1 = struct.pack('>I', Primitives.UInt32.unpack(data))
        f2 = struct.pack('>H', Primitives.UInt16.unpack(data))
        f3 = struct.pack('>H', Primitives.UInt16.unpack(data))
        f4 = data.read(8)
        # concat byte fields
        b = f1 + f2 + f3 + f4

        return uuid.UUID(bytes=b)


class _Primitive1(_Primitive):
    def __init__(self, fmt):
        self.struct = struct.Struct(fmt)
        self.size = self.struct.size
        self.format = self.struct.format

    def pack(self, data):
        return struct.pack(self.format, data)

    def unpack(self, data):
        return struct.unpack(self.format, data.read(self.size))[0]


class Primitives1(object):
    SByte = _Primitive1("<b")
    Int16 = _Primitive1("<h")
    Int32 = _Primitive1("<i")
    Int64 = _Primitive1("<q")
    Byte = _Primitive1("<B")
    Char = Byte
    UInt16 = _Primitive1("<H")
    UInt32 = _Primitive1("<I")
    UInt64 = _Primitive1("<Q")
    Boolean = _Primitive1("<?")
    Double = _Primitive1("<d")
    Float = _Primitive1("<f")


class Primitives(Primitives1):
    Null = _Null()
    String = _String()
    Bytes = _Bytes()
    ByteString = _Bytes()
    CharArray = _String()
    DateTime = _DateTime()
    Guid = _Guid()


def pack_uatype_array(vtype, array):
    if array is None:
        return b'\xff\xff\xff\xff'
    length = len(array)
    b = [pack_uatype(vtype, val) for val in array]
    b.insert(0, Primitives.Int32.pack(length))
    return b"".join(b)


def pack_uatype(vtype, value):
    if hasattr(Primitives, vtype.name):
        return getattr(Primitives, vtype.name).pack(value)
    elif vtype.value > 25:
        return Primitives.Bytes.pack(value)
    elif vtype == ua.VariantType.ExtensionObject:
        return extensionobject_to_binary(value)
    elif vtype in (ua.VariantType.NodeId, ua.VariantType.ExpandedNodeId):
        return nodeid_to_binary(value)
    elif vtype == ua.VariantType.Variant:
        return variant_to_binary(value)
    else:
        return struct_to_binary(value)


def unpack_uatype(vtype, data):
    if hasattr(Primitives, vtype.name):
        st = getattr(Primitives, vtype.name)
        return st.unpack(data)
    elif vtype.value > 25:
        return Primitives.Bytes.unpack(data)
    elif vtype == ua.VariantType.ExtensionObject:
        return extensionobject_from_binary(data)
    elif vtype in (ua.VariantType.NodeId, ua.VariantType.ExpandedNodeId):
        return nodeid_from_binary(data)
    elif vtype == ua.VariantType.Variant:
        return variant_from_binary(data)
    else:
        if hasattr(ua, vtype.name):
            klass = getattr(ua, vtype.name)
            return struct_from_binary(klass, data)
        else:
            raise UaError("Cannot unpack unknown variant type {0!s}".format(vtype))


def unpack_uatype_array(vtype, data):
    if hasattr(Primitives, vtype.name):
        st = getattr(Primitives, vtype.name)
        return st.unpack_array(data)
    else:
        length = Primitives.Int32.unpack(data)
        if length == -1:
            return None
        else:
            return [unpack_uatype(vtype, data) for _ in range(length)]


def struct_to_binary(obj):
    packet = []
    has_switch = hasattr(obj, "ua_switches")
    if has_switch:
        for name, switch in obj.ua_switches.items():
            member = getattr(obj, name)
            container_name, idx = switch
            if member is not None:
                container_val = getattr(obj, container_name)
                container_val = container_val | 1 << idx
                setattr(obj, container_name, container_val)
    for name, uatype in obj.ua_types:
        val = getattr(obj, name)
        if uatype.startswith("ListOf"):
            packet.append(list_to_binary(uatype[6:], val))
        else:
            if has_switch and val is None and name in obj.ua_switches:
                pass
            else:
                packet.append(to_binary(uatype, val))
    return b''.join(packet)


def to_binary(uatype, val):
    """
    Pack a python object to binary given a string defining its type
    """
    if isinstance(val, (list, tuple)):
        length = len(val)
        b = [to_binary(uatype, el) for el in val]
        b.insert(0, Primitives.Int32.pack(length))
        return b"".join(b)
    elif isinstance(uatype, (str, unicode)) and hasattr(ua.VariantType, uatype):
        vtype = getattr(ua.VariantType, uatype)
        return pack_uatype(vtype, val)
    elif isinstance(val, (IntEnum, Enum)):
        return Primitives.UInt32.pack(val.value)
    elif isinstance(val, ua.NodeId):
        return nodeid_to_binary(val)
    elif isinstance(val, ua.Variant):
        return variant_to_binary(val)
    elif hasattr(val, "ua_types"):
        return struct_to_binary(val)
    else:
        raise UaError("No known way to pack {} of type {} to ua binary".format(val, uatype))


def list_to_binary(uatype, val):
    if val is None:
        return Primitives.Int32.pack(-1)
    else:
        pack = []
        pack.append(Primitives.Int32.pack(len(val)))
        for el in val:
            pack.append(to_binary(uatype, el))
        return b''.join(pack)


def nodeid_to_binary(nodeid):
    if nodeid.NodeIdType == ua.NodeIdType.TwoByte:
        return struct.pack("<BB", nodeid.NodeIdType.value, nodeid.Identifier)
    elif nodeid.NodeIdType == ua.NodeIdType.FourByte:
        return struct.pack("<BBH", nodeid.NodeIdType.value, nodeid.NamespaceIndex, nodeid.Identifier)
    elif nodeid.NodeIdType == ua.NodeIdType.Numeric:
        return struct.pack("<BHI", nodeid.NodeIdType.value, nodeid.NamespaceIndex, nodeid.Identifier)
    elif nodeid.NodeIdType == ua.NodeIdType.String:
        return struct.pack("<BH", nodeid.NodeIdType.value, nodeid.NamespaceIndex) + \
            Primitives.String.pack(nodeid.Identifier)
    elif nodeid.NodeIdType == ua.NodeIdType.ByteString:
        return struct.pack("<BH", nodeid.NodeIdType.value, nodeid.NamespaceIndex) + \
            Primitives.Bytes.pack(nodeid.Identifier)
    elif nodeid.NodeIdType == ua.NodeIdType.Guid:
        return struct.pack("<BH", nodeid.NodeIdType.value, nodeid.NamespaceIndex) + \
               Primitives.Guid.pack(nodeid.Identifier)
    else:
        return struct.pack("<BH", nodeid.NodeIdType.value, nodeid.NamespaceIndex) + \
            nodeid.Identifier.to_binary()
    # FIXME: Missing NNamespaceURI and ServerIndex


def nodeid_from_binary(data):
    nid = ua.NodeId()
    encoding = ord(data.read(1))
    nid.NodeIdType = ua.NodeIdType(encoding & 0b00111111)

    if nid.NodeIdType == ua.NodeIdType.TwoByte:
        nid.Identifier = ord(data.read(1))
    elif nid.NodeIdType == ua.NodeIdType.FourByte:
        nid.NamespaceIndex, nid.Identifier = struct.unpack("<BH", data.read(3))
    elif nid.NodeIdType == ua.NodeIdType.Numeric:
        nid.NamespaceIndex, nid.Identifier = struct.unpack("<HI", data.read(6))
    elif nid.NodeIdType == ua.NodeIdType.String:
        nid.NamespaceIndex = Primitives.UInt16.unpack(data)
        nid.Identifier = Primitives.String.unpack(data)
    elif nid.NodeIdType == ua.NodeIdType.ByteString:
        nid.NamespaceIndex = Primitives.UInt16.unpack(data)
        nid.Identifier = Primitives.Bytes.unpack(data)
    elif nid.NodeIdType == ua.NodeIdType.Guid:
        nid.NamespaceIndex = Primitives.UInt16.unpack(data)
        nid.Identifier = Primitives.Guid.unpack(data)
    else:
        raise UaError("Unknown NodeId encoding: " + str(nid.NodeIdType))

    if test_bit(encoding, 7):
        nid.NamespaceUri = Primitives.String.unpack(data)
    if test_bit(encoding, 6):
        nid.ServerIndex = Primitives.UInt32.unpack(data)

    return nid


def variant_to_binary(var):
    b = []
    encoding = var.VariantType.value & 0b111111
    if var.is_array or isinstance(var.Value, (list, tuple)):
        var.is_array = True
        encoding = set_bit(encoding, 7)
        if var.Dimensions is not None:
            encoding = set_bit(encoding, 6)
        b.append(Primitives.Byte.pack(encoding))
        b.append(pack_uatype_array(var.VariantType, ua.flatten(var.Value)))
        if var.Dimensions is not None:
            b.append(pack_uatype_array(ua.VariantType.Int32, var.Dimensions))
    else:
        b.append(Primitives.Byte.pack(encoding))
        b.append(pack_uatype(var.VariantType, var.Value))

    return b"".join(b)


def variant_from_binary(data):
    dimensions = None
    array = False
    encoding = ord(data.read(1))
    int_type = encoding & 0b00111111
    vtype = ua.datatype_to_varianttype(int_type)
    if test_bit(encoding, 7):
        value = unpack_uatype_array(vtype, data)
        array = True
    else:
        value = unpack_uatype(vtype, data)
    if test_bit(encoding, 6):
        dimensions = unpack_uatype_array(ua.VariantType.Int32, data)
        value = _reshape(value, dimensions)
    return ua.Variant(value, vtype, dimensions, is_array=array)


def _reshape(flat, dims):
    subdims = dims[1:]
    subsize = 1
    for i in subdims:
        if i == 0:
            i = 1
        subsize *= i
    while dims[0] * subsize > len(flat):
        flat.append([])
    if not subdims or subdims == [0]:
        return flat
    return [_reshape(flat[i:i + subsize], subdims) for i in range(0, len(flat), subsize)]


def extensionobject_from_binary(data):
    """
    Convert binary-coded ExtensionObject to a Python object.
    Returns an object, or None if TypeId is zero
    """
    typeid = nodeid_from_binary(data)
    Encoding = ord(data.read(1))
    body = None
    if Encoding & (1 << 0):
        length = Primitives.Int32.unpack(data)
        if length < 1:
            body = Buffer(b"")
        else:
            body = data.copy(length)
            data.skip(length)
    if typeid.Identifier == 0:
        return None
    elif typeid in ua.extension_object_classes:
        klass = ua.extension_object_classes[typeid]
        if body is None:
            raise UaError("parsing ExtensionObject {0} without data".format(klass.__name__))
        return from_binary(klass, body)
    else:
        e = ua.ExtensionObject()
        e.TypeId = typeid
        e.Encoding = Encoding
        if body is not None:
            e.Body = body.read(len(body))
        return e


def extensionobject_to_binary(obj):
    """
    Convert Python object to binary-coded ExtensionObject.
    If obj is None, convert to empty ExtensionObject (TypeId=0, no Body).
    Returns a binary string
    """
    if isinstance(obj, ua.ExtensionObject):
        return struct_to_binary(obj)
    if obj is None:
        TypeId = ua.NodeId()
        Encoding = 0
        Body = None
    else:
        TypeId = ua.extension_object_ids[obj.__class__.__name__]
        Encoding = 0x01
        Body = struct_to_binary(obj)
    packet = []
    packet.append(nodeid_to_binary(TypeId))
    packet.append(Primitives.Byte.pack(Encoding))
    if Body:
        packet.append(Primitives.Bytes.pack(Body))
    return b''.join(packet)


def from_binary(uatype, data):
    """
    unpack data given an uatype as a string or a python class having a ua_types memeber
    """
    if isinstance(uatype, (str, unicode)) and uatype.startswith("ListOf"):
        size = Primitives.Int32.unpack(data)
        if size == -1:
            return None
        res = []
        for _ in range(size):
            res.append(from_binary(uatype[6:], data))
        return res
    elif isinstance(uatype, (str, unicode)) and hasattr(ua.VariantType, uatype):
        vtype = getattr(ua.VariantType, uatype)
        return unpack_uatype(vtype, data)
    else:
        return struct_from_binary(uatype, data)


def struct_from_binary(objtype, data):
    """
    unpack an ua struct. Arguments are an objtype as Python class or string
    """
    if isinstance(objtype, (unicode, str)):
        objtype = getattr(ua, objtype)
    if issubclass(objtype, Enum):
        return objtype(Primitives.UInt32.unpack(data))
    obj = objtype()
    for name, uatype in obj.ua_types:
        # if our member has a swtich and it is not set we skip it
        if hasattr(obj, "ua_switches") and name in obj.ua_switches:
            container_name, idx = obj.ua_switches[name]
            val = getattr(obj, container_name)
            if not test_bit(val, idx):
                continue
        val = from_binary(uatype, data)
        setattr(obj, name, val)
    return obj


def header_to_binary(hdr):
    b = []
    b.append(struct.pack("<3ss", hdr.MessageType, hdr.ChunkType))
    size = hdr.body_size + 8
    if hdr.MessageType in (ua.MessageType.SecureOpen, ua.MessageType.SecureClose, ua.MessageType.SecureMessage):
        size += 4
    b.append(Primitives.UInt32.pack(size))
    if hdr.MessageType in (ua.MessageType.SecureOpen, ua.MessageType.SecureClose, ua.MessageType.SecureMessage):
        b.append(Primitives.UInt32.pack(hdr.ChannelId))
    return b"".join(b)


def header_from_binary(data):
    hdr = ua.Header()
    hdr.MessageType, hdr.ChunkType, hdr.packet_size = struct.unpack("<3scI", data.read(8))
    hdr.body_size = hdr.packet_size - 8
    if hdr.MessageType in (ua.MessageType.SecureOpen, ua.MessageType.SecureClose, ua.MessageType.SecureMessage):
        hdr.body_size -= 4
        hdr.ChannelId = Primitives.UInt32.unpack(data)
    return hdr


def uatcp_to_binary(message_type, message):
    """
    Convert OPC UA TCP message (see OPC UA specs Part 6, 7.1) to binary.
    The only supported types are Hello, Acknowledge and ErrorMessage
    """
    header = ua.Header(message_type, ua.ChunkType.Single)
    binmsg = struct_to_binary(message)
    header.body_size = len(binmsg)
    return header_to_binary(header) + binmsg


