import copy
import struct

import pytest

from amitools.fs.ADFSVolume import ADFSVolume
from amitools.fs.FSError import (
    FSError,
    INTERNAL_ERROR,
    INVALID_DIR_CACHE_BLOCK,
    INVALID_ENTRY_BLOCK,
)
from amitools.fs.FSString import FSString
from amitools.fs.blkdev.ADFBlockDevice import ADFBlockDevice
from amitools.fs.block.Block import Block
from amitools.fs.block.CommentBlock import CommentBlock
from amitools.fs.block.DirCacheBlock import DirCacheBlock, DirCacheRecord
from amitools.fs.block.UserDirBlock import UserDirBlock
from amitools.fs.validate.Log import Log
from amitools.fs.validate.Validator import Validator
import amitools.fs.DosType as DosType
from amitools.tools.xdftool import main as xdftool_main

DOS_TYPES = [DosType.DOS0 + n for n in range(8)]
FFS_DOS_TYPES = [DosType.DOS1, DosType.DOS3, DosType.DOS5, DosType.DOS7]
DIRCACHE_DOS_TYPES = [DosType.DOS4, DosType.DOS5]
LONGNAME_DOS_TYPES = [DosType.DOS6, DosType.DOS7]


def dos_id(dos_type):
    return "DOS%d" % (dos_type - DosType.DOS0)


def create_volume(path, dos_type):
    blkdev = ADFBlockDevice(str(path))
    blkdev.create()
    volume = ADFSVolume(blkdev)
    volume.create(FSString("Test"), dos_type=dos_type)
    return blkdev, volume


def close_volume(blkdev, volume):
    volume.close()
    blkdev.close()


def validate(path):
    blkdev = ADFBlockDevice(str(path), read_only=True)
    blkdev.open()
    validator = Validator(blkdev, Log.WARN)
    assert validator.scan_boot()[0]
    assert validator.scan_root()
    validator.scan_dir_tree()
    validator.scan_files()
    validator.scan_bitmap()
    blkdev.close()
    return [str(e) for e in validator.log.entries]


def make_dircache_volume(path, dos_type):
    """a volume whose 'dir' needs several dircache blocks; returns the
    numbers of the dircache blocks of 'dir'"""
    blkdev, volume = create_volume(path, dos_type)
    volume.create_dir(FSString("dir"))
    volume.create_dir(FSString("dir/sub"))
    for n in range(40):
        volume.write_file(b"x" * (n * 50), FSString("dir/file%02d" % n))
    volume.write_file(b"data", FSString("top"))
    node = volume.get_path_name(FSString("dir"))
    dcb_nums = [dcb.blk_num for dcb in node.dcache_blks or []]
    if volume.is_dircache:
        assert len(dcb_nums) > 1
    close_volume(blkdev, volume)
    return dcb_nums


@pytest.mark.parametrize("dos_type", DOS_TYPES, ids=dos_id)
def fs_validate_clean_volume_test(tmp_path, dos_type):
    image = tmp_path / "test.adf"
    make_dircache_volume(image, dos_type)
    assert validate(image) == []


def rewrite_dircache_block(path, blk_num, change):
    blkdev = ADFBlockDevice(str(path))
    blkdev.open()
    dcb = DirCacheBlock(blkdev, blk_num)
    dcb.read()
    assert dcb.valid
    change(dcb)
    blkdev.close()


@pytest.mark.parametrize("dos_type", DIRCACHE_DOS_TYPES, ids=dos_id)
def fs_validate_dircache_checksum_test(tmp_path, dos_type):
    image = tmp_path / "test.adf"
    # the last block, so that no block depends on its link
    dcb_num = make_dircache_volume(image, dos_type)[-1]

    def corrupt(dcb):
        data = bytearray(dcb.blkdev.read_block(dcb.blk_num))
        data[24] ^= 0xFF  # first record, checksum now stale
        dcb.blkdev.write_block(dcb.blk_num, data)

    rewrite_dircache_block(image, dcb_num, corrupt)
    errors = validate(image)
    assert len(errors) == 1
    assert "@%06d:ERROR" % dcb_num in errors[0]
    assert "dircache" in errors[0]


@pytest.mark.parametrize("dos_type", DIRCACHE_DOS_TYPES, ids=dos_id)
def fs_validate_dircache_parent_test(tmp_path, dos_type):
    image = tmp_path / "test.adf"
    dcb_num = make_dircache_volume(image, dos_type)[0]

    def reparent(dcb):
        dcb.parent += 1
        dcb.write()

    rewrite_dircache_block(image, dcb_num, reparent)
    errors = validate(image)
    assert len(errors) == 1
    assert "@%06d:ERROR" % dcb_num in errors[0]
    assert "parent" in errors[0]


@pytest.mark.parametrize("dos_type", DIRCACHE_DOS_TYPES, ids=dos_id)
def fs_validate_dircache_own_key_test(tmp_path, dos_type):
    image = tmp_path / "test.adf"
    dcb_num = make_dircache_volume(image, dos_type)[0]

    def rekey(dcb):
        dcb.own_key += 1
        dcb.write()

    rewrite_dircache_block(image, dcb_num, rekey)
    errors = validate(image)
    assert len(errors) == 1
    assert "@%06d:ERROR" % dcb_num in errors[0]
    assert "own key" in errors[0]


@pytest.mark.parametrize(
    "change", ["name", "size", "comment", "entry", "missing", "duplicate"]
)
@pytest.mark.parametrize("dos_type", DIRCACHE_DOS_TYPES, ids=dos_id)
def fs_validate_dircache_record_test(tmp_path, dos_type, change):
    # Each entry of a directory has exactly one dircache record, and that
    # record matches the entry's name, block, size and comment.
    image = tmp_path / "test.adf"
    # the last dircache block of 'dir' has room for another record
    dcb_num = make_dircache_volume(image, dos_type)[-1]
    entry = []

    def alter(dcb):
        r = dcb.records[0]
        entry.append(r.entry)
        if change == "name":
            r.name = FSString("x" + r.name.get_unicode()[1:])
        elif change == "size":
            r.size += 1
        elif change == "comment":
            r.comment = FSString("x")
        elif change == "entry":
            r.entry = dcb.parent
        elif change == "missing":
            dcb.remove_record(r)
        else:
            dcb.add_record(
                DirCacheRecord(
                    r.entry, r.size, r.protect, r.mod_ts, r.sub_type, r.name, r.comment
                )
            )
        dcb.write()

    rewrite_dircache_block(image, dcb_num, alter)
    want = {
        "name": [(dcb_num, "name")],
        "size": [(dcb_num, "size")],
        "comment": [(dcb_num, "comment")],
        "entry": [(dcb_num, "no entry"), (entry[0], "no dircache record")],
        "missing": [(entry[0], "no dircache record")],
        "duplicate": [(dcb_num, "duplicate")],
    }[change]
    errors = validate(image)
    assert len(errors) == len(want)
    for error, (blk_num, words) in zip(errors, want):
        assert "@%06d:ERROR" % blk_num in error
        assert words in error


def overrun(dcb):
    dcb.num_records = 0xFFFFFFFF
    dcb.write()


@pytest.mark.parametrize("dos_type", DIRCACHE_DOS_TYPES, ids=dos_id)
def fs_validate_dircache_record_overrun_test(tmp_path, dos_type, capsys):
    # A record count that runs past the end of the block makes the block
    # invalid. The validator reports it; the directory can still be listed
    # and its files read, to rescue them.
    image = tmp_path / "test.adf"
    dcb_num = make_dircache_volume(image, dos_type)[-1]
    rewrite_dircache_block(image, dcb_num, overrun)
    errors = validate(image)
    assert len(errors) == 1
    assert "@%06d:ERROR" % dcb_num in errors[0]
    assert "dircache" in errors[0]

    blkdev = ADFBlockDevice(str(image))
    blkdev.open()
    volume = ADFSVolume(blkdev)
    volume.open()
    node = volume.get_path_name(FSString("dir"))
    assert len(node.get_entries()) == 41
    assert volume.read_file(FSString("dir/file01")) == b"x" * 50
    close_volume(blkdev, volume)

    capsys.readouterr()
    assert xdftool_main([str(image), "list"]) == 0
    assert "file39" in capsys.readouterr().out


@pytest.mark.parametrize(
    "op", ["write", "delete", "protect", "write_sub", "delete_sub", "delete_dir"]
)
@pytest.mark.parametrize("dos_type", DIRCACHE_DOS_TYPES, ids=dos_id)
def fs_validate_dircache_overrun_change_test(tmp_path, dos_type, op):
    # Nothing changes a directory whose dircache cannot be read: a write
    # would drop the records it could not read, and leave the block
    # allocated with nothing pointing to it. Trying again fails again.
    image = tmp_path / "test.adf"
    dcb_num = make_dircache_volume(image, dos_type)[-1]
    blkdev, volume = open_volume(image)
    volume.write_file(b"x", FSString("dir/sub/old"))
    close_volume(blkdev, volume)
    rewrite_dircache_block(image, dcb_num, overrun)
    before = image.read_bytes()
    blkdev = ADFBlockDevice(str(image))
    blkdev.open()
    volume = ADFSVolume(blkdev)
    volume.open()
    free = volume.get_free_blocks()
    for attempt in range(2):
        with pytest.raises(FSError) as error:
            if op == "write":
                volume.write_file(b"x", FSString("dir/new"))
            elif op == "delete":
                volume.delete(FSString("dir/file00"))
            elif op == "protect":
                volume.get_path_name(FSString("dir/file00")).change_protect(0x0F)
            elif op == "write_sub":
                # its new time stamp would change the record of dir/sub
                volume.write_file(b"x", FSString("dir/sub/new"))
            elif op == "delete_sub":
                volume.delete(FSString("dir/sub/old"))
            else:
                volume.delete(FSString("dir"), all=True)
        assert error.value.code == INVALID_DIR_CACHE_BLOCK
    assert volume.get_free_blocks() == free
    close_volume(blkdev, volume)
    assert image.read_bytes() == before


def open_volume(path):
    blkdev = ADFBlockDevice(str(path))
    blkdev.open()
    volume = ADFSVolume(blkdev)
    volume.open()
    return blkdev, volume


@pytest.mark.parametrize("op", ["write", "delete", "protect"])
@pytest.mark.parametrize("dos_type", DIRCACHE_DOS_TYPES, ids=dos_id)
def fs_validate_root_dircache_overrun_test(tmp_path, dos_type, op, capsys):
    # A volume whose root has an invalid dircache block opens, and its
    # files list and read; a change to the root is refused.
    image = tmp_path / "test.adf"
    make_dircache_volume(image, dos_type)
    blkdev, volume = open_volume(image)
    dcb_num = volume.get_root_dir().block.extension
    close_volume(blkdev, volume)
    rewrite_dircache_block(image, dcb_num, overrun)
    before = image.read_bytes()

    blkdev, volume = open_volume(image)
    names = [e.name.get_unicode_name() for e in volume.get_root_dir().get_entries()]
    assert sorted(names) == ["dir", "top"]
    assert volume.read_file(FSString("top")) == b"data"
    with pytest.raises(FSError) as error:
        if op == "write":
            volume.write_file(b"x", FSString("new"))
        elif op == "delete":
            volume.delete(FSString("top"))
        else:
            volume.get_path_name(FSString("top")).change_protect(0x0F)
    assert error.value.code == INVALID_DIR_CACHE_BLOCK
    close_volume(blkdev, volume)
    assert image.read_bytes() == before

    capsys.readouterr()
    assert xdftool_main([str(image), "list"]) == 0
    out = capsys.readouterr().out
    assert "top" in out and "file39" in out


@pytest.mark.parametrize("dos_type", DIRCACHE_DOS_TYPES, ids=dos_id)
def fs_validate_dircache_overrun_delete_dir_test(tmp_path, dos_type):
    # An empty directory whose dircache block is invalid is not deleted:
    # the dircache blocks after it could not be found and released.
    image = tmp_path / "test.adf"
    blkdev, volume = create_volume(image, dos_type)
    volume.create_dir(FSString("empty"))
    dcb_num = volume.get_path_name(FSString("empty")).block.extension
    close_volume(blkdev, volume)
    rewrite_dircache_block(image, dcb_num, overrun)
    before = image.read_bytes()

    blkdev, volume = open_volume(image)
    with pytest.raises(FSError) as error:
        volume.delete(FSString("empty"))
    assert error.value.code == INVALID_DIR_CACHE_BLOCK
    close_volume(blkdev, volume)
    assert image.read_bytes() == before


@pytest.mark.parametrize("kind", ["loop", "other_dir", "out_of_range", "own_key"])
@pytest.mark.parametrize("dos_type", DIRCACHE_DOS_TYPES, ids=dos_id)
def fs_validate_dircache_chain_test(tmp_path, dos_type, kind, capsys, monkeypatch):
    # A dircache chain that links a block twice, a block of another
    # directory or a block off the disk, or holds a block with a wrong own
    # key, is an invalid dircache: the directory lists, but a change that
    # would move a record into such a block is refused. A loop is not
    # followed for ever: past a few hundred reads, reading fails.
    image = tmp_path / "test.adf"
    blkdev, volume = create_volume(image, dos_type)
    volume.create_dir(FSString("P"))
    volume.create_dir(FSString("Q"))
    volume.write_file(b"q", FSString("Q/qfile"))
    # 17 records fill a dircache block
    for n in range(17):
        volume.write_file(b"x", FSString("P/f%02d" % n))
    p_node = volume.get_path_name(FSString("P"))
    p_num = p_node.block.blk_num
    p_dcb = p_node.block.extension
    q_dcb = volume.get_path_name(FSString("Q")).block.extension
    close_volume(blkdev, volume)

    def relink(dcb):
        if kind == "loop":
            dcb.next_cache = p_dcb
        elif kind == "other_dir":
            dcb.next_cache = q_dcb
        else:
            dcb.own_key += 1
        dcb.write()

    if kind == "out_of_range":
        blkdev = ADFBlockDevice(str(image))
        blkdev.open()
        ud = UserDirBlock(blkdev, p_num)
        ud.read()
        ud.extension = 5000
        ud.write()
        blkdev.close()
    else:
        rewrite_dircache_block(image, p_dcb, relink)
    before = image.read_bytes()
    read = DirCacheBlock.read
    reads = []

    def bounded_read(self):
        reads.append(self.blk_num)
        if len(reads) > 300:
            raise RuntimeError("dircache chain followed for ever")
        read(self)

    monkeypatch.setattr(DirCacheBlock, "read", bounded_read)

    capsys.readouterr()
    assert xdftool_main([str(image), "list"]) == 0
    assert "f16" in capsys.readouterr().out
    blkdev, volume = open_volume(image)
    with pytest.raises(FSError) as error:
        volume.get_path_name(FSString("P/f00")).change_comment(FSString("c" * 79))
    assert error.value.code == INVALID_DIR_CACHE_BLOCK
    close_volume(blkdev, volume)
    assert image.read_bytes() == before


@pytest.mark.parametrize("dos_type", DIRCACHE_DOS_TYPES, ids=dos_id)
def fs_validate_dircache_overrun_backstop_test(tmp_path, dos_type):
    # Every change checks the dircache before it writes anything. Should
    # one get past that, the dircache code refuses, too.
    image = tmp_path / "test.adf"
    dcb_num = make_dircache_volume(image, dos_type)[-1]
    rewrite_dircache_block(image, dcb_num, overrun)
    before = image.read_bytes()
    blkdev, volume = open_volume(image)
    node = volume.get_path_name(FSString("dir"))
    node.get_entries()
    dcb = node.dcache_blks[0]
    record = dcb.records[0]
    calls = [
        lambda: node._dircache_add_entry_int(copy.copy(record)),
        lambda: node._dircache_remove_record(dcb, record),
        lambda: node.update_dircache_record(record, copy.copy(record)),
    ]
    for call in calls:
        with pytest.raises(FSError) as error:
            call()
        assert error.value.code == INTERNAL_ERROR
    assert dcb.records[0] is record
    close_volume(blkdev, volume)
    assert image.read_bytes() == before


def entry_read_order(node):
    """the header block numbers of the entries of node, in the order that
    reading node reads them: the hash table, then the hash chains"""
    entries = {e.block.blk_num: e for e in node.get_entries()}
    order = [blk_num for blk_num in node.block.hash_table if blk_num != 0]
    for blk_num in order:
        if entries[blk_num].block.hash_chain != 0:
            order.append(entries[blk_num].block.hash_chain)
    return order


def flip_checksum(path, blk_num):
    """invert a byte of the checksum of block blk_num: twice restores it"""
    blkdev = ADFBlockDevice(str(path))
    blkdev.open()
    data = bytearray(blkdev.read_block(blk_num))
    data[20] ^= 0xFF
    blkdev.write_block(blk_num, data)
    blkdev.close()


def make_invalid_entry(path, dos_type, bad):
    """a volume whose 'dir' holds a0 to a5, of which the one read first or
    last has a bad checksum: reading 'dir' stops there"""
    blkdev, volume = create_volume(path, dos_type)
    volume.create_dir(FSString("dir"))
    for n in range(6):
        volume.write_file(b"x" * 10, FSString("dir/a%d" % n))
    order = entry_read_order(volume.get_path_name(FSString("dir")))
    close_volume(blkdev, volume)
    flip_checksum(path, order[0] if bad == "first" else order[-1])


@pytest.mark.parametrize(
    "bad,op",
    [
        (bad, op)
        for bad in ("first", "last")
        for op in ("write", "makedir", "delete", "delete_dir")
        # reading stops at the first entry: there is none to delete
        if not (bad == "first" and op == "delete")
    ],
)
@pytest.mark.parametrize("dos_type", DOS_TYPES, ids=dos_id)
def fs_validate_entry_invalid_change_test(tmp_path, dos_type, bad, op):
    # Reading a directory stops at an entry whose header is invalid. No
    # entry is then added or removed: a new one could take the name or the
    # hash chain of one that was not read, and deleting the directory would
    # lose those. Trying again fails again.
    image = tmp_path / "test.adf"
    make_invalid_entry(image, dos_type, bad)
    before = image.read_bytes()
    blkdev, volume = open_volume(image)
    names = sorted(
        e.name.get_unicode_name()
        for e in volume.get_path_name(FSString("dir")).get_entries()
    )
    assert len(names) == (0 if bad == "first" else 5)
    free = volume.get_free_blocks()
    for attempt in range(2):
        with pytest.raises(FSError) as error:
            if op == "write":
                volume.write_file(b"x", FSString("dir/new"))
            elif op == "makedir":
                volume.create_dir(FSString("dir/new"))
            elif op == "delete":
                volume.delete(FSString("dir/" + names[0]))
            else:
                volume.delete(FSString("dir"), all=True)
        assert error.value.code == INVALID_ENTRY_BLOCK
    assert volume.get_free_blocks() == free
    close_volume(blkdev, volume)
    assert image.read_bytes() == before


@pytest.mark.parametrize("dos_type", DOS_TYPES, ids=dos_id)
def fs_validate_entry_invalid_read_test(tmp_path, dos_type, capsys):
    # A volume whose root stops reading at an invalid header still lists
    # the entries read, and their files read. Their meta info changes, and
    # entries are created in and deleted from a directory read in full, as
    # these do not add or remove entries of the root. Once the header is
    # repaired, the image is valid.
    image = tmp_path / "test.adf"
    blkdev, volume = create_volume(image, dos_type)
    volume.create_dir(FSString("good"))
    volume.write_file(b"old", FSString("good/old"))
    for n in range(6):
        volume.write_file(b"r%d" % n, FSString("r%d" % n))
    # the header read last is not that of "good"
    bad = entry_read_order(volume.get_root_dir())[-1]
    assert bad != volume.get_path_name(FSString("good")).block.blk_num
    close_volume(blkdev, volume)
    flip_checksum(image, bad)

    blkdev, volume = open_volume(image)
    root = volume.get_root_dir()
    names = sorted(e.name.get_unicode_name() for e in root.get_entries())
    assert len(names) == 6 and "good" in names
    name = [n for n in names if n != "good"][0]
    assert volume.read_file(FSString(name)) == name.encode()
    with pytest.raises(FSError) as error:
        volume.write_file(b"x", FSString("new"))
    assert error.value.code == INVALID_ENTRY_BLOCK
    volume.get_path_name(FSString(name)).change_comment(FSString("a comment"))
    volume.get_path_name(FSString("good")).change_protect(0x0F)
    volume.write_file(b"new", FSString("good/new"))
    volume.delete(FSString("good/old"))
    close_volume(blkdev, volume)

    capsys.readouterr()
    assert xdftool_main([str(image), "list"]) == 0
    assert name in capsys.readouterr().out
    flip_checksum(image, bad)
    blkdev, volume = open_volume(image)
    assert volume.read_file(FSString("good/new")) == b"new"
    assert len(volume.get_root_dir().get_entries()) == 7
    close_volume(blkdev, volume)
    assert validate(image) == []


@pytest.mark.parametrize(
    "dos_type,fail",
    [(dos_type, "entry") for dos_type in DOS_TYPES]
    + [(dos_type, "dircache") for dos_type in DIRCACHE_DOS_TYPES],
    ids=lambda v: dos_id(v) if isinstance(v, int) else v,
)
def fs_validate_read_raises_test(tmp_path, dos_type, fail, monkeypatch):
    # A read of a directory that raises, as when a header or a dircache
    # block cannot be read, leaves it unread: the next use reads it again,
    # in full, and the entries created then keep all records.
    image = tmp_path / "test.adf"
    dcb_nums = make_dircache_volume(image, dos_type)
    blkdev, volume = open_volume(image)
    if fail == "dircache":
        blk_num = dcb_nums[1]
    else:
        blk_num = entry_read_order(volume.get_path_name(FSString("dir")))[1]
    close_volume(blkdev, volume)
    read_block = ADFBlockDevice.read_block
    failed = []

    def failing_read_block(self, num):
        if num == blk_num and not failed:
            failed.append(num)
            raise IOError("read failed")
        return read_block(self, num)

    blkdev, volume = open_volume(image)
    monkeypatch.setattr(ADFBlockDevice, "read_block", failing_read_block)
    node = volume.get_path_name(FSString("dir"))
    with pytest.raises(IOError):
        node.get_entries()
    assert node.entries is None
    for n in range(20):
        volume.write_file(b"y", FSString("dir/new%02d" % n))
    close_volume(blkdev, volume)

    blkdev, volume = open_volume(image)
    assert len(volume.get_path_name(FSString("dir")).get_entries()) == 61
    close_volume(blkdev, volume)
    assert validate(image) == []


def dircache_like_data(blkdev):
    """file data that passes for a dircache block whose record count runs
    past its end"""
    data = bytearray(blkdev.block_bytes)
    struct.pack_into(">I", data, 0, Block.T_DIR_CACHE)
    struct.pack_into(">I", data, 12, 0xFFFFFFFF)
    longs = struct.unpack(">%dI" % blkdev.block_longs, data)
    struct.pack_into(">I", data, 20, -sum(longs) & 0xFFFFFFFF)
    return bytes(data)


@pytest.mark.parametrize("dos_type", FFS_DOS_TYPES, ids=dos_id)
def fs_validate_file_data_like_dircache_test(tmp_path, dos_type):
    # FFS data blocks hold raw file data, which may look like any block.
    image = tmp_path / "test.adf"
    blkdev, volume = create_volume(image, dos_type)
    volume.write_file(dircache_like_data(blkdev), FSString("file"))
    close_volume(blkdev, volume)
    assert validate(image) == []


@pytest.mark.parametrize("dos_type", LONGNAME_DOS_TYPES, ids=dos_id)
def fs_validate_comment_block_test(tmp_path, dos_type):
    image = tmp_path / "test.adf"
    name = FSString("n" * 60)
    blkdev, volume = create_volume(image, dos_type)
    volume.write_file(b"data", name)
    volume.create_dir(FSString("d" * 60))
    volume.get_path_name(name).change_comment(FSString("c" * 79))
    volume.get_path_name(FSString("d" * 60)).change_comment(FSString("c" * 79))
    close_volume(blkdev, volume)
    assert validate(image) == []


@pytest.mark.parametrize("dos_type", LONGNAME_DOS_TYPES, ids=dos_id)
def fs_validate_comment_block_own_key_test(tmp_path, dos_type):
    image = tmp_path / "test.adf"
    name = FSString("n" * 60)
    blkdev, volume = create_volume(image, dos_type)
    volume.write_file(b"data", name)
    node = volume.get_path_name(name)
    node.change_comment(FSString("c" * 79))
    cblk_num = node.block.comment_block_id
    close_volume(blkdev, volume)

    blkdev = ADFBlockDevice(str(image))
    blkdev.open()
    cblk = CommentBlock(blkdev, cblk_num)
    cblk.read()
    cblk.own_key += 1
    cblk.write()
    blkdev.close()
    errors = validate(image)
    assert len(errors) == 1
    assert "@%06d:ERROR" % cblk_num in errors[0]
    assert "own key" in errors[0]


def fs_validate_read_error_test(tmp_path, monkeypatch):
    # A block that cannot be read is reported; the validator goes on.
    image = tmp_path / "test.adf"
    blkdev, volume = create_volume(image, DosType.DOS0)
    volume.write_file(b"data", FSString("file"))
    blk_num = volume.get_path_name(FSString("file")).block.blk_num
    close_volume(blkdev, volume)
    read_block = ADFBlockDevice.read_block

    def failing_read_block(self, num):
        if num == blk_num:
            raise IOError("read failed")
        return read_block(self, num)

    monkeypatch.setattr(ADFBlockDevice, "read_block", failing_read_block)
    errors = validate(image)
    assert "@%06d:ERROR:Can't read block" % blk_num in errors
