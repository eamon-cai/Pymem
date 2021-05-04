import ctypes
import collections
import functools
import struct

import pymem.rctypes
import pymem.resources.structure


IMAGE_ORDINAL_FLAG32 = 0x80000000
IMAGE_ORDINAL_FLAG64 = 0x8000000000000000


def get_structure_transformer_for_target(target, targetbitness=None):
    current_bitness = 8 * struct.calcsize("P")
    if target is None:
        ctypes_structure_transformer = lambda x:x
        create_structure_at = lambda structcls, addr: structcls.from_address(addr)
        return ctypes_structure_transformer, create_structure_at

    if targetbitness is None:
        targetbitness = target.bitness

    if targetbitness == 32 and current_bitness == 64:
        ctypes_structure_transformer = pymem.rctypes.transform_type_to_remote32bits
    elif targetbitness == 64 and current_bitness == 32:
        ctypes_structure_transformer = pymem.rctypes.transform_type_to_remote64bits
    elif targetbitness == current_bitness:
        ctypes_structure_transformer = pymem.rctypes.transform_type_to_remote
    else:
        raise NotImplementedError("Parsing {0} PE from {1} Process".format(targetbitness, current_bitness))

    def create_structure_at(structcls, addr):  # Il reste une closure sur 'target' ici !!
        return ctypes_structure_transformer(structcls)(target, addr)
    return ctypes_structure_transformer, create_structure_at


def get_pe_bitness(baseaddr, target):
    # We can force bitness as the field we access are bitness-independant
    pe = GetPEFile(baseaddr, target, force_bitness=32)
    machine = pe.get_NT_HEADER().FileHeader.Machine
    if machine == 0x14c:
        return 32
    elif machine == 0x8664:
        return 64
    else:
        raise ValueError("Unknow PE target machine <0x{0:x}>".format(machine))


## == PEPARSE V2 ==


import collections
CtypesStructureTransformers = collections.namedtuple("CtypesStructureTransformers", ["ctypes_structure_transformer", "create_structure_at"])


def GetPEFile(baseaddr, handle=None, force_bitness=None):
    """Returns a :class:`PEFile` to explore a PE loaded at `baseaddr` in process `target`.
    :rtype: :class:`PEFile`
    .. note::
        If target is ``None`` it refers to the current process
    """
    proc_bitness = 8 * struct.calcsize("P")

    if force_bitness is None:
        targetedbitness = get_pe_bitness(baseaddr, handle)
    else:
        targetedbitness = force_bitness

    transformers = get_structure_transformer_for_target(handle, targetedbitness)
    #ctypes_structure_transformer, create_structure_at = transformers
    transfor_funcs = CtypesStructureTransformers(*transformers)  # TODO: rename
    return PEFile(handle, baseaddr, targetedbitness, transfor_funcs)


class THUNK_DATA(ctypes.Union):
    _fields_ = [
        ("Ordinal", ctypes.c_void_p),
        ("AddressOfData", ctypes.c_void_p)
    ]


class IMPORT_BY_NAME(ctypes.Structure):
    _fields_ = [
        ("Hint", ctypes.c_ushort),
        ("Name", ctypes.c_byte)
    ]


def get_string(handle, addr):
    if handle is None:
        return ctypes.c_char_p(addr).value.decode("latin1")
    return pymem.memory.read_string(handle, addr)


class PESection(pymem.resources.structure.IMAGE_SECTION_HEADER):

    @property
    def name(self):
        if self.handle is None:
            name = get_string(self.handle, ctypes.addressof(self.Name))[:8]
        else:
            name = get_string(self.handle, self.base_address)[:8]
        # Decode as UTF-8 as the MS doc say ?
        return name

    @property
    def start(self):
        return self.baseaddr + self.VirtualAddress

    @property
    def size(self):
        return self.VirtualSize

    def __repr__(self):
        return "<PESection \"{0}\">".format(self.name)

    @classmethod
    def create(cls, pefile, addr):
        self = pefile.transformers.create_structure_at(cls, addr)
        self.baseaddr = pefile.baseaddr
        self.handle = pefile.handle
        return self


class IATPtr(ctypes.c_void_p):
    @classmethod
    def from_iatentry(cls, iat_entry):
        self = cls.from_address(iat_entry.addr)
        self.addr = iat_entry.addr
        self.nonhookvalue = iat_entry.nonhookvalue
        return self


class IATEntry(ctypes.Structure):
    """Represent an entry in the IAT of a module
    Can be used to get resolved value and setup hook
    """
    _fields_ = [
        ("value", ctypes.c_void_p)
    ]

    @classmethod
    def create(cls, addr, ord, name, handle, transformers):
        self = transformers.create_structure_at(cls, addr)
        self.addr = addr
        self.ord = ord
        self.name = name
        self.hook = None
        self.nonhookvalue = self.value
        self.handle = handle
        return self

    def __repr__(self):
        return '<{0} "{1}" ordinal {2}>'.format(self.__class__.__name__, self.name, self.ord)

    # def set_hook(self, callback, types=None):
    #     """Setup a hook on the entry and return it.
    #     You MUST keep a reference to the hook while the hook is enabled.
    #     :param callback: the hook
    #         .. note::
    #             see :ref:`hook_protocol`
    #     :rtype: :class:`windows.hooks.IATHook`
    #     .. warning::
    #         This works only for PEFile with the current process as target.
    #     """
    #     if self.target is not None:
    #         raise NotImplementedError("Setting hook in remote process (use python code injection)")
    #
    #     hook = hooks.IATHook(self, callback, types)
    #     import weakref
    #     self.whook = weakref.ref(hook, self.on_destroy)
    #     self.hook = hook
    #     hook.enable()
    #     return hook
    #
    # def on_destroy(self, *args):
    #     # We cannot know if the hook was enabled here..
    #     print("DESTROY: {0} -> ".format(args, self.enabled))
    #     # import pdb;pdb.set_trace()
    #     # print(args[0]())
    #
    # def remove_hook(self):
    #     """Remove the hook on the entry"""
    #     if self.hook is None:
    #         return False
    #     self.hook.disable()
    #     self.hook = None
    #     return True
    #
    # # def __del__(self):
    #     # print(self.hook)
    #     # if self.hook:
    #         # print("LOL BYE {0}".format(self.hook))


class IMAGE_IMPORT_DESCRIPTOR(pymem.resources.structure.IMAGE_IMPORT_DESCRIPTOR): # TODO: use explicite name winstructs.IMAGE_IMPORT_DESCRIPTOR

    def get_INT(self):
        if not self.OriginalFirstThunk:
            return None
        int_addr = self.OriginalFirstThunk + self.baseaddr
        int_entry = self.transformers.create_structure_at(THUNK_DATA, int_addr)
        res = []
        while int_entry.Ordinal:
            if int_entry.Ordinal & self.IMAGE_ORDINAL_FLAG:
                res += [(int_entry.Ordinal & 0x7fffffff, None)]
            else:
                import_by_name = self.transformers.create_structure_at(IMPORT_BY_NAME, self.baseaddr + int_entry.AddressOfData)
                name_address = self.baseaddr + int_entry.AddressOfData + type(import_by_name).Name.offset
                name = get_string(self.handle, name_address)
                res.append((import_by_name.Hint, name))
            int_addr += ctypes.sizeof(type(int_entry))
            int_entry = self.transformers.create_structure_at(THUNK_DATA, int_addr)
        return res

    def get_IAT(self):
        iat_addr = self.FirstThunk + self.baseaddr
        iat_entry = self.transformers.create_structure_at(THUNK_DATA, iat_addr)
        res = []
        while iat_entry.Ordinal:
            res.append(IATEntry.create(iat_addr, -1, "??", self.handle, self.transformers))
            iat_addr += ctypes.sizeof(type(iat_entry))
            iat_entry = self.transformers.create_structure_at(THUNK_DATA, iat_addr)
        return res

    @classmethod
    def create(cls, pefile, addr):
        self = pefile.transformers.create_structure_at(cls, addr)
        self.baseaddr = pefile.baseaddr
        self.transformers = pefile.transformers
        self.IMAGE_ORDINAL_FLAG = pefile.IMAGE_ORDINAL_FLAG
        self.handle = pefile.handle
        return self


class IMAGE_EXPORT_DIRECTORY(pymem.resources.structure.IMAGE_EXPORT_DIRECTORY): # TODO: use explicite name winstructs._IMAGE_EXPORT_DIRECTORY
    def get_exports(self):
        NameOrdinals = self.transformers.create_structure_at((ctypes.c_ushort * self.NumberOfNames), self.AddressOfNameOrdinals + self.baseaddr)
        NameOrdinals = list(NameOrdinals)
        Functions = self.transformers.create_structure_at((ctypes.c_ulong * self.NumberOfFunctions), self.AddressOfFunctions + self.baseaddr)
        Names = self.transformers.create_structure_at((ctypes.c_ulong * self.NumberOfNames), self.AddressOfNames + self.baseaddr)
        res = []
        for nb, func in enumerate(Functions):
            func += self.baseaddr
            if nb in NameOrdinals:
                name = get_string(self.handle, Names[NameOrdinals.index(nb)] + self.baseaddr)
                # Export name should be ascii
                # Decode from ascii or return bytes ?
                # https://docs.microsoft.com/en-us/windows/win32/debug/pe-format#export-address-table
            else:
                name = None
            res.append((nb, func, name))
        return res

    @classmethod
    def create(cls, pefile, addr):
        self = pefile.transformers.create_structure_at(cls, addr)
        self.transformers = pefile.transformers
        self.handle = pefile.handle
        self.baseaddr = pefile.baseaddr
        return self


class IMAGE_DOS_HEADER(ctypes.Structure):
    _fields_ = [
        ("e_magic", ctypes.c_char * 2),
        ("e_cblp", ctypes.c_ushort),
        ("e_cp", ctypes.c_ushort),
        ("e_crlc", ctypes.c_ushort),
        ("e_cparhdr", ctypes.c_ushort),
        ("e_minalloc", ctypes.c_ushort),
        ("e_maxalloc", ctypes.c_ushort),
        ("e_ss", ctypes.c_ushort),
        ("e_sp", ctypes.c_ushort),
        ("e_csum", ctypes.c_ushort),
        ("e_ip", ctypes.c_ushort),
        ("e_cs", ctypes.c_ushort),
        ("e_lfarlc", ctypes.c_ushort),
        ("e_ovno", ctypes.c_ushort),
        ("e_res", ctypes.c_ushort * 4),
        ("e_oemid", ctypes.c_ushort),
        ("e_oeminfo", ctypes.c_ushort),
        ("e_res2", ctypes.c_ushort * 10),
        ("e_lfanew", ctypes.c_ulong),
    ]


class PEFile(object):
    """Represent a PE loaded in a process (current or remote)"""

    def __init__(self, handle, baseaddr, targetedbitness, transformers):
        self.handle = handle
        self.baseaddr = baseaddr
        self.bitness = targetedbitness
        self.transformers = transformers

        if targetedbitness == 32:
            self.IMAGE_ORDINAL_FLAG = IMAGE_ORDINAL_FLAG32
        else:
            self.IMAGE_ORDINAL_FLAG = IMAGE_ORDINAL_FLAG64

    def get_DOS_HEADER(self):
        return self.transformers.create_structure_at(IMAGE_DOS_HEADER, self.baseaddr)

    def get_NT_HEADER(self):
        offset = self.get_DOS_HEADER().e_lfanew
        if self.bitness == 32:
            return self.transformers.create_structure_at(pymem.resources.structure.IMAGE_NT_HEADERS32, self.baseaddr + offset)
        return self.transformers.create_structure_at(pymem.resources.structure.IMAGE_NT_HEADERS64, self.baseaddr + offset)

    def get_OptionalHeader(self):
        return self.get_NT_HEADER().OptionalHeader

    def get_DataDirectory(self):
        # This won't work if we load a PE32 in a 64bit process
        # PE32 .NET...
        # return self.get_OptionalHeader().DataDirectory
        DataDirectory_type = pymem.resources.structure.IMAGE_DATA_DIRECTORY * pymem.resources.structure.IMAGE_NUMBEROF_DIRECTORY_ENTRIES
        SizeOfOptionalHeader = self.get_NT_HEADER().FileHeader.SizeOfOptionalHeader
        if self.handle is None:
            opt_header_addr = ctypes.addressof(self.get_NT_HEADER().OptionalHeader)
        else:
            opt_header_addr = self.get_NT_HEADER().OptionalHeader.base_address
        DataDirectory_addr = opt_header_addr + SizeOfOptionalHeader - ctypes.sizeof(DataDirectory_type)
        return self.transformers.create_structure_at(DataDirectory_type, DataDirectory_addr)

    def get_IMPORT_DESCRIPTORS(self):
        IMAGE_DIRECTORY_ENTRY_IMPORT = 1
        import_datadir = self.get_DataDirectory()[IMAGE_DIRECTORY_ENTRY_IMPORT]
        if import_datadir.VirtualAddress == 0:
            return []
        import_descriptor_addr = self.baseaddr + import_datadir.VirtualAddress
        current_import_descriptor = IMAGE_IMPORT_DESCRIPTOR.create(self, import_descriptor_addr)
        res = []
        while current_import_descriptor.FirstThunk:
            res.append(current_import_descriptor)
            import_descriptor_addr += ctypes.sizeof(IMAGE_IMPORT_DESCRIPTOR)
            current_import_descriptor = IMAGE_IMPORT_DESCRIPTOR.create(self, import_descriptor_addr)
        return res

    def get_EXPORT_DIRECTORY(self):
        IMAGE_DIRECTORY_ENTRY_EXPORT = 0
        export_directory_rva = self.get_DataDirectory()[IMAGE_DIRECTORY_ENTRY_EXPORT].VirtualAddress
        if export_directory_rva == 0:
            return None
        export_directory_addr = self.baseaddr + export_directory_rva
        exp_dir = IMAGE_EXPORT_DIRECTORY.create(self, export_directory_addr)
        return exp_dir

    @property
    @functools.lru_cache(maxsize=1)
    def sections(self):
        nt_header = self.get_NT_HEADER()
        nb_section = nt_header.FileHeader.NumberOfSections
        SizeOfOptionalHeader = self.get_NT_HEADER().FileHeader.SizeOfOptionalHeader
        if self.handle is None:
            opt_header_addr = ctypes.addressof(self.get_NT_HEADER().OptionalHeader)
        else:
            opt_header_addr = self.get_NT_HEADER().OptionalHeader.base_address
        base_section = opt_header_addr + SizeOfOptionalHeader
        #pe_section_type = IMAGE_SECTION_HEADER
        # return [PESection.create(self, base_section + (sizeof(IMAGE_SECTION_HEADER) * i)) for i in range(nb_section)]
        return [
            PESection.create(self, base_section + (ctypes.sizeof(pymem.resources.structure.IMAGE_SECTION_HEADER) * i))
            for i in range(nb_section)
        ]
        #sections_array = self.transformers.create_structure_at((self.PESection * nb_section), base_section)
        #return list(sections_array)

    @property
    @functools.lru_cache(maxsize=1)
    def exports(self):
        """The exports of the PE in a dict. Keys are ordinal (:class:`int`) and name (:class:`str`).
         The values are the addresses of the exports.
            :type: {(:class:`int` or :class:`str`) : :class:`int`}"""
        IMAGE_DIRECTORY_ENTRY_EXPORT = 0
        res = {}
        exp_dir = self.get_EXPORT_DIRECTORY()
        export_datadir = self.get_DataDirectory()[IMAGE_DIRECTORY_ENTRY_EXPORT]
        export_start = self.baseaddr + export_datadir.VirtualAddress
        export_end = export_start + export_datadir.Size
        if exp_dir is None:
            return res
        # import pdb;pdb.set_trace()
        raw_exports = exp_dir.get_exports()
        for id, rva_addr, rva_name in raw_exports:
            if export_start <= rva_addr < export_end:
                # Export proxy...
                # Contains the string to another Dll.Function
                rva_addr = get_string(self.handle, rva_addr) # Put the string proxy instead

            res[id] = rva_addr
            if rva_name is not None:
                res[rva_name] = rva_addr
        return res

    @property
    @functools.lru_cache(maxsize=1)
    def export_name(self):
        """The Name attribute of the ``EXPORT_DIRECTORY``"""
        exp_dir = self.get_EXPORT_DIRECTORY()
        if exp_dir is None:
            return None
        if not exp_dir.Name:
            return None
        return get_string(self.handle, self.baseaddr + exp_dir.Name)

    # TODO: get imports by parsing other modules exports if no INT
    @property
    @functools.lru_cache(maxsize=1)
    def imports(self):
        """The imports of the PE in a dict.
        Keys are the names of DLL to import from and values are :class:`list`
        of :class:`IATEntry`
            :type: {:class:`str` : [:class:`IATEntry`]}"""
        res = {}
        for import_descriptor in self.get_IMPORT_DESCRIPTORS():
            INT = import_descriptor.get_INT()
            IAT = import_descriptor.get_IAT()
            if INT is not None:
                for iat_entry, (ord, name) in zip(IAT, INT):
                    # str(name.decode()) -> python2 and python3 compatible for str result
                    iat_entry.ord = ord
                    iat_entry.name = str(name) if name else ""
            name = get_string(self.handle, self.baseaddr + import_descriptor.Name)
            res.setdefault(name.lower(), []).extend(IAT)
        return res