import copy

from .block.CommentBlock import CommentBlock
from .block.EntryBlock import EntryBlock
from .FileName import FileName
from .MetaInfo import MetaInfo
from .ProtectFlags import ProtectFlags
from .TimeStamp import TimeStamp
from .FSError import *
from .FSString import FSString
import amitools.util.ByteSize as ByteSize

# AmigaDOS keeps at most 79 bytes of comment, whatever the DOS type
MAX_COMMENT_BYTES = 79


def check_comment(comment, node=None, file_name=None):
    """refuse a comment the disk cannot hold: FastFileSystem rejects more
    than 79 bytes, and the disk stores Latin-1 only"""
    try:
        size = len(comment.get_ami_str())
    except UnicodeEncodeError:
        raise FSError(
            INVALID_COMMENT, node=node, file_name=file_name, extra="not Latin-1"
        ) from None
    if size > MAX_COMMENT_BYTES:
        raise FSError(
            INVALID_COMMENT,
            node=node,
            file_name=file_name,
            extra="%d bytes, max %d" % (size, MAX_COMMENT_BYTES),
        )


class ADFSNode:
    def __init__(self, volume, parent):
        self.volume = volume
        self.blkdev = volume.blkdev
        self.parent = parent
        self.block_bytes = self.blkdev.block_bytes
        self.block = None
        self.name = None
        self.valid = False
        self.meta_info = None
        # the comment block that belongs to this entry, if any
        self.comment_blk_num = 0

    def __str__(self):
        return "%s:'%s'(@%d)" % (
            self.__class__.__name__,
            self.get_node_path_name(),
            self.block.blk_num,
        )

    def set_block(self, block):
        self.block = block
        self.name = FileName(
            self.block.name,
            is_intl=self.volume.is_intl,
            is_longname=self.volume.is_longname,
        )
        self.valid = True
        self.create_meta_info()

    def create_meta_info(self):
        comment = self.block.comment
        # a comment block that is not ours is never freed or rewritten, but
        # the header keeps pointing to it until a new comment replaces it
        self.comment_blk_num = 0
        if self.block.comment_block_id != 0:
            comment_block = self._read_comment_block()
            if comment_block:
                comment = comment_block.comment
                self.comment_blk_num = comment_block.blk_num
        self.meta_info = MetaInfo(self.block.protect, self.block.mod_ts, comment)

    def _read_comment_block(self):
        """return the comment block the header points to, or None unless it
        is a valid comment block that belongs to this entry"""
        blk_num = self.block.comment_block_id
        if blk_num < self.blkdev.reserved or blk_num >= self.blkdev.num_blocks:
            return None
        comment_block = CommentBlock(self.blkdev, blk_num)
        comment_block.read()
        if not comment_block.valid or comment_block.comment is None:
            return None
        if comment_block.header_key != self.block.blk_num:
            return None
        return comment_block

    def get_file_name(self):
        return self.name

    def delete(self, wipe=False, all=False, update_ts=True):
        if all:
            self.delete_children(wipe, all, update_ts)
        self.parent._delete(self, wipe, update_ts)

    def delete_children(self, wipe, all, update_ts):
        pass

    def get_meta_info(self):
        return self.meta_info

    def change_meta_info(self, meta_info):
        # check the comment before anything changes
        comment = meta_info.get_comment()
        if comment and hasattr(self.block, "comment"):
            check_comment(comment, node=self)
        else:
            comment = None
        protect = meta_info.get_protect()
        if not hasattr(self.block, "protect"):
            protect = None
        mod_ts = meta_info.get_mod_ts()
        if not (protect or mod_ts or comment):
            return

        # dircache: change a copy of the record, swapped in once written
        record = None
        if self.volume.is_dircache and self.parent:
            record = self.parent.get_dircache_record(self.name.get_name())
            if not record:
                raise FSError(INTERNAL_ERROR, node=self, extra="dc not found!")
            new_record = copy.copy(record)
            if protect:
                new_record.protect = protect
            if mod_ts:
                new_record.mod_ts = mod_ts
            if comment:
                new_record.comment = comment
            # a grown record may need a new dircache block: make sure that
            # there is one before anything changes
            self.parent.check_dircache_room(record, new_record)

        # a long name comment that does not fit beside the name goes into
        # the entry's own comment block, or else into a new one
        comment_blk_num = 0
        new_blk_num = 0
        if (
            comment
            and self.volume.is_longname
            and EntryBlock.needs_extra_comment_block(self.name.get_name(), comment)
        ):
            comment_blk_num = self.comment_blk_num
            if comment_blk_num == 0:
                blks = self.volume.bitmap.alloc_n(1)
                if blks is None:
                    raise FSError(NO_FREE_BLOCKS, node=self)
                comment_blk_num = new_blk_num = blks[0]

        # change the header, and put it back as it was if a write fails
        saved = vars(self.block).copy()
        if protect:
            self.block.protect = protect
        if mod_ts:
            self.block.mod_ts = mod_ts
        if comment:
            self.block.comment_block_id = comment_blk_num
            if comment_blk_num == 0:
                self.block.comment = comment
        header_written = False
        try:
            # a new comment block is written before the header points to it,
            # the entry's own one after the header, so that a header write
            # that fails leaves the old comment
            if new_blk_num != 0:
                self._write_comment_block(new_blk_num, comment)
            self.block.write()
            header_written = True
            if comment_blk_num != 0 and new_blk_num == 0:
                self._write_comment_block(comment_blk_num, comment)
            if record:
                self.parent.update_dircache_record(record, new_record)
        except Exception:
            vars(self.block).update(saved)
            if header_written:
                self.block.write()
            if new_blk_num != 0:
                self.volume.bitmap.dealloc_n([new_blk_num])
            raise

        # all is written: release a comment block the entry no longer uses
        if comment:
            if self.comment_blk_num not in (0, comment_blk_num):
                self.volume.bitmap.dealloc_n([self.comment_blk_num])
            self.comment_blk_num = comment_blk_num
            self.meta_info.set_comment(comment)
        if protect:
            self.meta_info.set_protect(protect)
        if mod_ts:
            self.meta_info.set_mod_ts(mod_ts)

    def _write_comment_block(self, blk_num, comment):
        """write comment into comment block blk_num of this entry"""
        comment_block = CommentBlock(self.blkdev, blk_num)
        comment_block.create(self.block.blk_num, comment)
        comment_block.write()

    def change_comment(self, comment):
        self.change_meta_info(MetaInfo(comment=comment))

    def change_protect(self, protect):
        self.change_meta_info(MetaInfo(protect=protect))

    def change_protect_by_string(self, pr_str):
        p = ProtectFlags()
        p.parse(pr_str)
        self.change_protect(p.mask)

    def change_mod_ts(self, mod_ts):
        self.change_meta_info(MetaInfo(mod_ts=mod_ts))

    def change_mod_ts_by_string(self, tm_str):
        t = TimeStamp()
        if not t.parse(tm_str):
            return False
        self.change_meta_info(MetaInfo(mod_ts=t))
        return True

    def get_list_str(self, indent=0, all=False, detail=False):
        istr = "  " * indent
        if detail:
            extra = self.get_detail_str()
        else:
            extra = self.meta_info.get_str_line()
        return "%-40s       %8s  %s" % (
            istr + self.name.get_unicode_name(),
            self.get_size_str(),
            extra,
        )

    def list(self, indent=0, all=False, detail=False, encoding="UTF-8"):
        print(self.get_list_str(indent=indent, all=all, detail=detail))

    def get_size_str(self):
        # re-implemented in derived classes!
        return ""

    def get_blocks(self, with_data=False):
        # re-implemented in derived classes!
        return 0

    def get_file_data(self):
        return None

    def dump_blocks(self, with_data=False):
        blks = self.get_blocks(with_data)
        for b in blks:
            b.dump()

    def get_node_path(self, with_vol=False):
        if self.parent != None:
            if not with_vol and self.parent.parent == None:
                r = []
            else:
                r = self.parent.get_node_path()
        else:
            if not with_vol:
                return []
            r = []
        r.append(self.name.get_unicode_name())
        return r

    def get_node_path_name(self, with_vol=False):
        r = self.get_node_path()
        return FSString("/".join(r))

    def get_detail_str(self):
        return ""

    def get_block_usage(self, all=False, first=True):
        return (0, 0)

    def get_file_bytes(self, all=False, first=True):
        return (0, 0)

    def is_file(self):
        return False

    def is_dir(self):
        return False

    def get_info(self, all=False):
        # block usage: data + fs blocks
        (data, fs) = self.get_block_usage(all=all)
        total = data + fs
        bb = self.blkdev.block_bytes
        btotal = total * bb
        bdata = data * bb
        bfs = fs * bb
        prc_data = 10000 * data / total
        prc_fs = 10000 - prc_data
        res = []
        res.append(
            "sum:    %10d  %s  %12d"
            % (total, ByteSize.to_byte_size_str(btotal), btotal)
        )
        res.append(
            "data:   %10d  %s  %12d  %5.2f%%"
            % (data, ByteSize.to_byte_size_str(bdata), bdata, prc_data / 100.0)
        )
        res.append(
            "fs:     %10d  %s  %12d  %5.2f%%"
            % (fs, ByteSize.to_byte_size_str(bfs), bfs, prc_fs / 100.0)
        )
        return res
