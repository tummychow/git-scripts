from utils import *


def dict_helper_contains_all_or_none(val, *keys):
    keys_present = map(lambda key: key in val, keys)
    if all(keys_present):
        return True
    if not any(keys_present):
        return False
    raise RuntimeError('{!r} contains some but not all of {!r}'.format(val, keys))


def dict_helper_contains_at_most_one(val, *keys):
    keys_present = list(filter(lambda key: key in val, keys))
    if len(keys_present) > 1:
        raise RuntimeError('{!r} contains more than one of of {!r}'.format(val, keys))
    if len(keys_present) == 1:
        return keys_present[0]
    return None


def parse_helper_mode_header(mode):
    # git does not preserve all file permission bits, it only knows of four
    # possible modes
    mode = int(mode)
    if mode == 100644:
        return 'regular'
    if mode == 100755:
        return 'executable'
    if mode == 120000:
        return 'symlink'
    if mode == 160000:
        return 'gitlink'
    raise RuntimeError('{!r} is not a recognized mode'.format(mode))


def parse_helper_quoted_filename(filename):
    # git performs c-style quoting using a table (sq_lookup) in quote.c
    # it supports the standard backslash escapes (a, b, f, n, r, t, v, ", \)
    # as well as octal
    # characters in the range [0x20, 0x80) are unescaped (besides backslash),
    # characters below that range are always escaped (using c-style character
    # if applicable, otherwise octal), and characters above that range are
    # octal escaped (unless core.quotePath is false)
    # if a filename does not contain any of those, then git will print it
    # completely unquoted
    quotestart = filename.startswith(b'"')
    quoteend = filename.endswith(b'"')
    if not quotestart and not quoteend:
        return filename
    if (quotestart and not quoteend) or (not quotestart and quoteend):
        raise RuntimeError('{!r} is missing a quote'.format(filename))
    # this file is quoted, we should start by discarding those
    filename = filename[1:-1]
    # iterate over backslashes and build up the unescaped filename
    ret = bytearray()
    idx = 0
    backslash = filename.find(b'\\', idx)
    while backslash != -1:
        ret.extend(filename[idx:backslash])
        idx = backslash + 1
        escape_seq = bytearray(filename[idx:idx+1])
        if escape_seq[0] in b'0123':
            # consume up to two additional chars for octal escapes
            while idx - backslash <= 3 and idx+1 < len(filename) and filename[idx+1] in b'01234567':
                idx += 1
                escape_seq.append(filename[idx])
        # now that we've determined the escape sequence, unescape it
        if escape_seq == b'\\' or escape_seq == b'"':
            # these are already unescaped as is
            pass
        elif escape_seq in b'abfnrtv':
            # TODO: there must be a way to do this without going bytearray->string->bytes
            escape_seq[0:0] = b'\\'
            escape_seq = escape_seq.decode('unicode_escape').encode('ascii')
        elif escape_seq.isdigit():
            # convert octal string to integer and make a byte of the result
            # this will fail automatically for integers outside [0,256)
            escape_seq = bytes([int(escape_seq, base=8)])
        else:
            raise RuntimeError('{!r} contains unrecognized escape {!r}'.format(filename, escape_seq))
        ret.extend(escape_seq)
        idx += 1
        backslash = filename.find(b'\\', idx)
    return ret


def parse_helper_similarity(similarity_percent):
    return int(desuffix(similarity_percent.decode('ascii'), '%', check=True))


def parse_helper_index_header(index):
    # the index extended header consists of the sha1 hashes of the blobs before
    # and after the diff, separated by a ".."
    # the index header will then have the mode, if it was unchanged
    # if the mode was changed, then the index header will omit it, and other
    # headers will indicate the changes that applied there
    index_split = index.decode('ascii').split(' ')
    [blob_old, blob_new] = index_split[0].split('..')
    if len(index_split) == 1:
        mode = ''
    elif len(index_split) == 2:
        mode = parse_helper_mode_header(index_split[1])
    else:
        raise RuntimeError('index contains multiple splits {!r}'.format(index_split))
    return {
        'old': blob_old,
        'new': blob_new,
        'mode': mode,
    }


def parse_helper_hunk_count(hunk_count):
    # a hunk header's line count consists of a "+" or "-", then a number, then
    # a comma, then another number
    # the first number is the start line of that side of the hunk, and the
    # second number is the number of lines that side spans
    # the comma and the count may be omitted if the hunk spans exactly one line
    numbers = hunk_count[1:].decode('ascii').split(',')
    if len(numbers) == 1:
        count = 1
    elif len(numbers) == 2:
        count = int(numbers[1])
    else:
        raise RuntimeError('hunk line count {!r} contains too many commas'.format(hunk_count))
    start = int(numbers[0])
    return {
        'start': start,
        'count': count,
        'end': None if count == 0 else start + count - 1,
    }


EXTENDED_HEADER_MAP = {
    'old mode': parse_helper_mode_header,
    'new mode': parse_helper_mode_header,
    'deleted file mode': parse_helper_mode_header,
    'new file mode': parse_helper_mode_header,
    'copy from': parse_helper_quoted_filename,
    'copy to': parse_helper_quoted_filename,
    'rename from': parse_helper_quoted_filename,
    'rename to': parse_helper_quoted_filename,
    'similarity index': parse_helper_similarity,
    'dissimilarity index': parse_helper_similarity,
    'index': parse_helper_index_header,
}


# a diff contains one or more patches, one for each file
# a patch contains one or more hunks, one for each contiguous region of change
# a hunk contains one or more blocks, one for each contiguous set of added,
# removed or unchanged lines
class DiffList(list):
    def __init__(self, stream):
        # discard the trailing newline on each input line
        self.stream = map(lambda line: desuffix(line, b'\n'), stream)
        next_state = self.parse_git_headers
        next_line = next(self.stream, None)
        while next_state is not None and next_line is not None:
            next_state, next_line = next_state(next_line)

    def parse_git_headers(self, line):
        # a git patch starts with "diff --git <file1> <file2>"
        # <file1> and <file2> start with "a/" and "b/" prefixes, respectively
        # (note: the prefixes may be affected by diff.mnemonicPrefix or
        # diff.noprefix, but if you use a plumbing command, those variables
        # will be ignored)
        # ignoring those prefixes, the two names will always be the same unless
        # the file was renamed/copied
        # (note: they will still be the same if the file was created or
        # deleted, /dev/null is not used here)
        # if the filenames contain newlines, quotes, tabs or backslashes, those
        # will be backslash-escaped, and the entire name will be quoted
        # however, spaces will NOT be backslash escaped, so you can't reliably
        # parse the filenames in this line unless you know they're the same (in
        # which case you would also know they had the same length)
        if not line.startswith(b'diff --git '):
            raise RuntimeError('{!r} is not a git patch header'.format(line))
        ext_headers = {}
        self.append({
            'init_header': line,
            'extended_headers': ext_headers,
        })

        # after that come one or more extended headers, used to indicate file
        # modes and copies/renames
        # the extended headers can theoretically appear in any order, but there
        # should be at most one of each type
        for line in self.stream:
            for prefix, line_parser in EXTENDED_HEADER_MAP.items():
                try:
                    rest_of_line = deprefix(line, prefix.encode('ascii') + b' ', check=True)
                except RuntimeError:
                    continue
                if callable(line_parser):
                    rest_of_line = line_parser(rest_of_line)
                if prefix in ext_headers:
                    raise RuntimeError('already parsed extended header {!r} to {!r}, cannot set to {!r}'.format(prefix, ext_headers[prefix], rest_of_line))
                ext_headers[prefix] = rest_of_line
                break
            else:
                self.parse_helper_cleanup_headers()
                # none of the prefixes matched, so this line is not an extended
                # header, and there are three possibilities:
                # it could lead to a binary patch
                if line == b'GIT binary patch':
                    return self.parse_binary_patch, line
                # it could lead to an elided binary patch (if you omit --binary)
                if line.startswith(b'Binary files ') and line.endswith(b' differ'):
                    self[-1]['binary_hunks'] = {'elided': True}
                    return self.parse_git_headers, next(self.stream, None)
                # it could lead to a text patch
                if line.startswith(b'--- '):
                    return self.parse_text_headers, line
                # or the patch could just end here
                return self.parse_git_headers, line
        # we have exhausted the input, and every line was an extended header
        # this is a valid point to terminate
        # TODO: avoid repeating this on both exit paths
        self.parse_helper_cleanup_headers()
        return None, None

    def parse_helper_cleanup_headers(self):
        ext_headers = self[-1]['extended_headers']
        # first we want to identify the paths affected
        path_header_from = dict_helper_contains_at_most_one(ext_headers, 'copy from', 'rename from')
        # either the file's path changed (rename/copy)
        if path_header_from is not None:
            # assert presence of a (dis)similarity header
            if dict_helper_contains_at_most_one(ext_headers, 'similarity index', 'dissimilarity index') is None:
                raise RuntimeError('{!r} was a rename/copy, but did not contain (dis)similarity index')
            path_header_to = desuffix(path_header_from, ' from', check=True) + ' to'
            dict_helper_contains_all_or_none(ext_headers, path_header_from, path_header_to)
            self[-1]['before_path'] = ext_headers[path_header_from]
            self[-1]['after_path'] = ext_headers[path_header_to]
        # or it did not change
        else:
            # in this case, we know that the init header's before and after
            # filenames are the same, so we can split the header in half by
            # length to find that filename
            init_header_files = deprefix(self[-1]['init_header'], b'diff --git', check=True)
            midpoint = len(init_header_files) // 2
            # offset by 1 to discard the leading space
            self[-1]['before_path'] = init_header_files[1:midpoint]
            self[-1]['after_path'] = init_header_files[midpoint+1:]
        # unquote the paths, wherever we got them from
        self[-1]['before_path'] = parse_helper_quoted_filename(self[-1]['before_path'])
        self[-1]['after_path'] = parse_helper_quoted_filename(self[-1]['after_path'])
        # next we want to identify the mode
        mode_header = dict_helper_contains_at_most_one(ext_headers, 'old mode', 'deleted file mode', 'new file mode')
        # there are five possible ways the mode could be denoted:
        # the file could have been deleted
        if mode_header == 'deleted file mode':
            self[-1]['before_mode'] = ext_headers[mode_header]
            self[-1]['after_mode'] = None
            self[-1]['after_path'] = None
        # or the file could have been added
        elif mode_header == 'new file mode':
            self[-1]['before_mode'] = None
            self[-1]['before_path'] = None
            self[-1]['after_mode'] = ext_headers[mode_header]
        # or the file's mode could have been modified, with old/new headers
        elif mode_header == 'old mode':
            # we assert that both old and new mode headers are present
            dict_helper_contains_all_or_none(ext_headers, 'old mode', 'new mode')
            self[-1]['before_mode'] = ext_headers['old mode']
            self[-1]['after_mode'] = ext_headers['new mode']
        # if none of those headers are present, the file's mode was not changed
        else:
            mode_source = None
            # normally when the file's mode is not changed, it will be included
            # in the index header
            # however, the index header can be omitted if the blob was not
            # changed (exact rename or copy), in which case the mode cannot be
            # determined from the patch content
            if 'index' in ext_headers:
                mode_source = ext_headers['index']['mode']
            self[-1]['before_mode'] = mode_source
            self[-1]['after_mode'] = mode_source

    def parse_text_headers(self, line):
        # a text patch always has a "---" line and then a "+++" line, which can
        # also be quoted
        # one very important difference between these paths and the ones used in
        # extended headers is that /dev/null will be used here for creations and
        # deletions
        # therefore, we prefer the lines in the extended headers for paths, but
        # we still attempt to parse these for validation purposes
        parse_helper_quoted_filename(deprefix(line, b'--- ', check=True))
        parse_helper_quoted_filename(deprefix(next(self.stream), b'+++ ', check=True))
        self[-1]['text_hunks'] = []
        # there must be at least one hunk following this header
        return self.parse_text_hunk, next(self.stream)

    def parse_binary_patch(self, line):
        # git starts a binary patch using this special header line
        if line != b'GIT binary patch':
            raise RuntimeError('{!r} is not the header of a git binary patch'.format(line))
        self[-1]['binary_hunks'] = {}
        # a binary patch is always followed by one binary hunk, which we skip
        # over
        self[-1]['binary_hunks']['forward'] = self.parse_helper_binary_hunk(next(self.stream))
        # after the first binary hunk, there could be a second, which is the
        # reverse of the first one
        line = next(self.stream, None)
        if line is not None and (line.startswith(b'literal ') or line.startswith(b'delta ')):
            self[-1]['binary_hunks']['backward'] = self.parse_helper_binary_hunk(line)
            line = next(self.stream, None)
        # there might have been one hunk, or two hunks, but there can't be any
        # more, so this must be the end of the patch
        return self.parse_git_headers, line

    def parse_helper_binary_hunk(self, line):
        # a binary hunk starts with the word "literal" or "delta", then the
        # number of uncompressed bytes in the patch
        # literal and delta refer to the way the patch has been encoded, either
        # a binary dump of the entire file, or a diff from one version to the
        # next
        # in either case, the data has been deflated for storage, so the length
        # reported here will not be the same as the length of binary data that
        # follows
        line = line.decode('ascii')
        if not (line.startswith('literal ') or line.startswith('delta ')):
            raise RuntimeError('{!r} is not the start of a git binary hunk'.format(line))
        [hunk_type, hunk_length] = line.split(' ')
        # after the length comes a series of binary data lines
        # each line starts with a single character indicating its length,
        # mapping 1-52 bytes to the letters A-Za-z (so A means the line has one
        # byte of data, z means 52 bytes, etc)
        # after the length indicator are the indicated number of bytes, in
        # a custom base85 encoding (similar to zeromq base85)
        # note that the length indicator is for the length of the deflated
        # binary data, not the base85-encoded length
        # like zeromq's version, the input binary is interpreted in big-endian
        # 4-byte chunks, and each chunk gets encoded to 5 chars
        # however, unlike zeromq, git uses 0-9A-Za-z!#$%&()*+-;<=>?@^_`{|}~ as
        # its mapping
        # padding is done with zero bytes, but since we already know the length
        # from the indicator, we know exactly how much padding there is, and
        # can discard it automatically
        for line in self.stream:
            # the base85 data is terminated by an empty line
            # since we don't care about the actual data, we just skip until we
            # find that line
            if len(line) == 0:
                break
        else:
            raise RuntimeError('could not find empty line terminator in binary hunk')
        return {
            'type': hunk_type,
            'len': int(hunk_length),
        }

    def parse_text_hunk(self, line):
        # a text hunk always starts with a header of the form
        # @@ <before_lines> <after_lines> @@ <optional context>
        [start, before, after, end] = line.split(b' ', maxsplit=3)
        if not (start == b'@@' and end.startswith(b'@@')):
            raise RuntimeError('{!r} is not a hunk header, missing @@ delimiters'.format(line))
        # the before count starts with a "-", and the after count starts with a
        # "+"
        if not (before.startswith(b'-') and after.startswith(b'+')):
            raise RuntimeError('{!r} is not a hunk header, line counts should start with -+'.format(line))
        before = parse_helper_hunk_count(before)
        after = parse_helper_hunk_count(after)
        blocks = []
        self[-1]['text_hunks'].append({
            'before': before,
            'after': after,
            'blocks': blocks,
        })
        # after a hunk header, we have the actual hunk lines
        # the lines fall into three categories:
        # context (starting with a space)
        # before (starting with a "-")
        # after (starting with a "+")
        # the lines must come in contiguous blocks of before, then after,
        # with blocks of context lines appearing between, and optionally also
        # at the start/end
        # in addition, git exposes a special line starting with a backslash,
        # "\ No newline at end of file", which indicates that the preceding
        # line did not have a newline terminator
        # we refer to this as an "NNEOF" (no-newline-end-of-file)
        # by its very nature, an NNEOF must come at the end of its block, and
        # if the NNEOF is attached to a context/after block, then that block
        # must itself be the end of the entire patch
        # if the NNEOF is attached to a before block, then it must be either
        # the end of the entire patch, or followed by exactly one after block
        # we use this set of line types to enforce what transitions are valid
        # (note that we do not include '@' and 'd', because a hunk must contain
        # at least one line)
        permitted_line_types = {' ', '-', '+', '\\'}
        # and for the special case of before-with-NNEOF-followed-by-after, we
        # have this boolean flag
        trailing_before_nneof = False
        # we will also enforce that the hunk has the number of lines it claimed
        # to have, using these counters
        before_seen = 0
        after_seen = 0
        for line in self.stream:
            if not line:
                raise RuntimeError('empty line found in text hunk')
            line_type = line[:1].decode('ascii')
            rest_of_line = line[1:]
            if line_type not in permitted_line_types:
                raise RuntimeError('line {!r} has unexpected line type (expected one of {!r})'.format(line, permitted_line_types))
            # no-newline indicators do not count towards the hunk's line count,
            # so we have to check this before we do the line count check
            if line_type == '\\':
                if rest_of_line != b' No newline at end of file':
                    raise RuntimeError('got NNEOF {!r} with unexpected line content after backslash'.format(line))
                blocks[-1]['ending_newline'] = False
                # if a before block has an NNEOF, then it may be followed by
                # one after block, or nothing
                if blocks[-1]['type'] == '-':
                    permitted_line_types = {'+', 'd'}
                    trailing_before_nneof = True
                # if a context or after block has an NNEOF, then nothing is
                # permitted to follow it, it terminates the entire patch
                else:
                    permitted_line_types = {'d'}
                continue
            # we validate the counters immediately after checking for NNEOF,
            # and before updating the line counters
            # this covers the case where NNEOF is the last line in the hunk and
            # is then followed by a new diff - if we moved this condition
            # anywhere else, then we would break out of the loop before
            # consuming that NNEOF
            if before_seen > before['count'] or after_seen > after['count']:
                raise RuntimeError('found more before/after lines than expected ({}>{} || {}>{})'.format(before_seen, before['count'], after_seen, after['count']))
            if before_seen == before['count'] and after_seen == after['count']:
                # note that this is the only break in the entire loop, it has
                # to stay that way
                break
            # if we're still in the loop, then we can update counters
            # if the counters are now maxed out, we'll still consume one more
            # line (in case that line is an NNEOF)
            if line_type == ' ':
                before_seen += 1
                after_seen += 1
            elif line_type == '-':
                before_seen += 1
            elif line_type == '+':
                after_seen += 1
            # now we can update the block list
            # if this line continues a previous block, then add it to the block
            if blocks and blocks[-1]['type'] == line_type:
                blocks[-1]['lines'].append(rest_of_line)
            # otherwise, create a new block with just this line, and set the
            # appropriate continuing line types
            else:
                blocks.append({
                    'type': line_type,
                    'lines': [rest_of_line],
                    'ending_newline': True,
                })
                # after blocks cannot transition to before blocks
                # this prevents us from patches of before->after->before, which
                # are invalid because the two before sections should be
                # combined together
                if line_type == '+':
                    # if the preceding block was a before block with an NNEOF,
                    # then this after block must be the last one in the patch
                    if trailing_before_nneof:
                        permitted_line_types = {'+', '\\', 'd'}
                    else:
                        permitted_line_types = {' ', '+', '\\', '@', 'd'}
                # for other block types, anything is allowed
                else:
                    permitted_line_types = {' ', '-', '+', '\\', '@', 'd'}
        else:
            # we exhausted the input stream
            # we have to make sure we did parse this entire hunk before leaving
            if not (before_seen == before['count'] and after_seen == after['count']):
                raise RuntimeError('input was exhausted before hunk {!r} was finished'.format(blocks))
            return None, None
        # if we got to here, then we must have broken out of hunk processing
        # because we counted all the lines in the hunk
        # before we can move on, we have to make sure there is at least one
        # non-context block in the hunk
        if all(map(lambda block: block['type'] == ' ', blocks)):
            raise RuntimeError('hunk consists entirely of context blocks {!r}'.format(blocks))
        # there could be another hunk here (assuming the last block wasn't
        # stopped by an NNEOF, but we already enforced that by excluding '@'
        # from the set of permitted types)
        if line.startswith(b'@@'):
            if blocks[-1]['type'] == ' ' and not blocks[-1]['ending_newline']:
                raise RuntimeError('a no-newline context block must terminate the patch, but found new hunk header {!r}'.format(line))
            return self.parse_text_hunk, line
        # otherwise the whole patch is over
        return self.parse_git_headers, line

    def patch_by_after_path(self, target_path):
        # TODO: post-parse validation that patches are consistent with each
        # other, eg you can't have two patches with the same after_path
        for idx, patch in enumerate(self):
            if patch['after_path'] == target_path:
                return idx
        return None

    def patch_by_before_path(self, target_path):
        # TODO: combine the patch_by_*_path methods into one
        for idx, patch in enumerate(self):
            if patch['before_path'] == target_path:
                return idx
        return None

    # attempt to commute our own diff with another hunk that comes after us
    # chronologically
    # TODO: also implement this for a hunk coming before this patch
    def commute_with_hunk_after(self, input_hunk, after_path):
        # first, let's see if we even touch the input hunk's path
        before_patch = self.patch_by_after_path(after_path)
        if before_patch is None:
            # if not, then we trivially commute with that hunk
            return (True, self, input_hunk)
        if self[before_patch]['before_path'] is None:
            # this patch was the one that added that file, so commutation is
            # impossible
            return (False, self, input_hunk)
        if 'binary_hunks' in self[before_patch]:
            # if either side of a diff is binary, git will always show the
            # entire diff as binary, and we consider binary hunks of any kind
            # to be noncommutative with text
            return (False, self, input_hunk)
        # we will store the commuted version of the input hunk
        # a hunk will never be changed by commuting with a hunk below it, so we
        # only have to try with the topmost hunk in the patch - if that one is
        # below the input hunk, then so are all the other hunks in the patch,
        # and we know that the input hunk won't be changed by commutation with
        # this patch at all
        commuted_input_hunk = None
        # we will attempt to commute every hunk in the patch with the input
        commuted_before_hunks = []
        # this list could be empty if the patch was binary, or if this was a
        # new file, and we've ruled both those cases out
        for before_hunk in self[before_patch]['text_hunks']:
            does_commute, commuted_input, commuted_before = commute_two_hunks(before_hunk, input_hunk)
            if not does_commute:
                # this patch does not commute with the input hunk, so we bail
                return (False, self, input_hunk)
            if commuted_input_hunk is None:
                # as mentioned above, we should only do this assignment once
                commuted_input_hunk = commuted_input
            commuted_before_hunks.append(commuted_before)
        # TODO: what do we do with extended headers? especially index? should
        # we just remove that header to indicate that the blob hashes are
        # invalid?
        ret = self.copy()
        ret[before_patch] = ret[before_patch].copy()
        ret[before_patch]['text_hunks'] = commuted_before_hunks
        return (True, ret, commuted_input_hunk)

def commute_two_hunks(first, second):
    # first we have to determine which hunk is above the other
    before_first_above_second = first['before']['start'] <= second['before']['start']
    after_first_above_second = first['after']['start'] <= second['after']['start']
    # we expect the above/below relationship to be the same on both sides, if
    # not then we've got some strangely formed hunks and error out
    if before_first_above_second and after_first_above_second:
        above = first
        below = second
    elif not before_first_above_second and not after_first_above_second:
        above = second
        below = first
    else:
        raise RuntimeError('first is {} second on before side, but {} second on after side (fb={} fa={} sb={} sa={})'.format('above' if before_first_above_second else 'below', 'above' if after_first_above_second else 'below', first['before'], first['after'], second['before'], second['after']))
    # now we compute the ranges of affected lines and confirm that the hunks
    # are separated by at least one unchanged line on each side
    # if either hunk is empty, then they're already separated, so we have to
    # check that first
    if above['before']['count'] != 0 and below['before']['count'] != 0:
        # we have confirmed that neither hunk is empty, now we need to check
        # for an empty line between the end of the above hunk and the start of
        # the below hunk
        if below['before']['start'] - above['before']['end'] < 2:
            return False, first, second
    # TODO: avoid repeating these three lines
    if above['after']['count'] != 0 and below['after']['count'] != 0:
        if below['after']['start'] - above['after']['end'] < 2:
            return False, first, second
    # at this point, we know the hunks commute
    # we need to know how the net number of lines added/removed by the above
    # hunk
    above_change_offset = above['after']['count'] - above['before']['count']
    # now, the below hunk has to move by that many lines
    # if the below hunk was first, then it has to move down, now that the above
    # hunk is being commuted to come before it
    if below is second:
        # but if the below hunk was second, then it has to move up instead of
        # down
        above_change_offset = -above_change_offset
    ret_below = below.copy()
    ret_below['before'] = ret_below['before'].copy()
    ret_below['before']['start'] += above_change_offset
    if ret_below['before']['count'] != 0:
        ret_below['before']['end'] += above_change_offset
    # TODO: avoid repeating these four lines
    ret_below['after'] = ret_below['after'].copy()
    ret_below['after']['start'] += above_change_offset
    if ret_below['after']['count'] != 0:
        ret_below['after']['end'] += above_change_offset
    # make sure to return the commuted hunks in the right order
    if below is second:
        return True, ret_below, above
    return True, above, ret_below
